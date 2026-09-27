"""Static cycle gate over the interaction graph (an *additive* signal).

The gate answers one question for a candidate pairing (or a small card set):
**does the interaction graph contain a closed cycle whose resources close, with
no once-per-turn/once-per-game cap?**  It does not reorder, remove or re-score
any existing candidate; it is a separate, measurable signal that callers may
apply after generation (see ``scripts/cycle_gate.py``).

Why a separate module
---------------------

:mod:`~combo_discovery.ontology.cycles` already enumerates re-entrant cycles and
validates resource closure, but it is anchored on ``re_trigger`` links and its
``infinite`` flag is only "the cycle contains *some* repeatable producer".  The
gate here is deliberately narrower and stronger in one dimension and broader in
another:

* it enumerates *any* closed cycle over flow links (``enables`` /
  ``re_trigger`` / ``satisfies``), not only ``re_trigger``-anchored ones;
* it adds one documented closure rule the graph itself lacks: a
  ``copy_permanent`` output covers the ``tap`` activation cost of an ability on
  the copied card, because each fresh token arrives untapped (this is the
  Kiki-Jiki / Splinter Twin loop shape, whose untap is an *activated* ability of
  the copy, not a trigger);
* it rejects a cycle that carries a *cap gate* (``turn_restriction``,
  ``first_attack``, ``activation_limit``, ...), i.e. "once each turn" limits.

Honest limits
-------------

The graph vocabulary is coarse.  It can express "untaps" but not always "untaps
only once each turn" (some such limits are emitted as ``turn_restriction`` /
``first_attack`` gates and *are* caught; others are hidden inside trigger text
and are not).  It cannot model Aura-granted abilities, quantity/mana accounting,
stack order, summoning sickness, or whether a producer is genuinely repeatable.
The signal is therefore a **recall-oriented ranker, not a proof**: a high signal
is evidence of cycle structure, not a guarantee the loop is unbounded, and a low
signal does not prove the absence of a loop.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .cycles import _gate_preconditions
from .edges import build_edges
from .extractor import CardContext, CardEffect, extract_card_predicates
from .links import Link, LinkOptions, build_links, copy_accepts, ports_enable
from .patterns.base import CardView
from .ports import AbilitySig, Port, build_signatures

#: Gate kinds that cap repetition (once per turn / combat / game).  Their mere
#: presence on a cycle ability makes the cycle bounded, regardless of whether a
#: producer could "discharge" the trigger condition.
CAP_GATES: frozenset[str] = frozenset({
    "activation_limit",
    "first_attack",
    "first_time",
    "only_first",
    "phase_out",
    "rolled_die",
    "turn_restriction",
})

#: Link kinds that carry a resource / re-entry flow between abilities.
FLOW_KINDS: tuple[str, ...] = ("enables", "re_trigger", "satisfies")

#: Synthetic link kind for the additive fresh-token closure rule.
COPY_REFRESH = "copy_refresh"

#: Motif recorded for the additive ``copy_refresh`` edge.
COPY_REFRESH_MOTIF = "COPIES_CREATURE~TAPS_COST"

#: Signal values: proven unbounded cycle / closed-but-capped / no cycle.
SIGNAL_UNBOUNDED = 1.0
SIGNAL_CAPPED = 0.5
SIGNAL_NONE = 0.0


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CycleStep:
    """One oriented edge of the reported cycle."""

    src: str
    kind: str
    subkind: str
    dst: str
    motif: str

    def as_dict(self) -> dict[str, str]:
        return {
            "src": self.src,
            "kind": self.kind,
            "subkind": self.subkind,
            "dst": self.dst,
            "motif": self.motif,
        }

    def format(self) -> str:
        motif = f" ({self.motif})" if self.motif else ""
        return f"{self.src} -[{self.kind}/{self.subkind}]- {self.dst}{motif}"


@dataclass
class CycleEvidence:
    """The gate's verdict for one pairing / card set."""

    cards: tuple[str, ...]
    closed: bool = False
    unbounded: bool = False
    signal: float = SIGNAL_NONE
    caps: tuple[str, ...] = ()
    leaks: tuple[str, ...] = ()
    preconditions: tuple[str, ...] = ()
    motifs: tuple[str, ...] = ()
    path: tuple[CycleStep, ...] = ()
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "cards": list(self.cards),
            "closed": self.closed,
            "unbounded": self.unbounded,
            "signal": self.signal,
            "caps": list(self.caps),
            "leaks": list(self.leaks),
            "preconditions": list(self.preconditions),
            "motifs": list(self.motifs),
            "path": [step.as_dict() for step in self.path],
            "notes": list(self.notes),
        }

    def format(self) -> str:
        if self.unbounded:
            verdict = "UNBOUNDED CYCLE"
        elif self.closed:
            verdict = "CLOSED (capped)"
        elif self.path:
            verdict = "NOT CLOSED (resources leak)"
        else:
            verdict = "no cycle"
        lines = [f"{' + '.join(self.cards)}: {verdict}  signal={self.signal:.2f}"]
        if self.caps:
            lines.append(f"    caps: {', '.join(self.caps)}")
        if self.leaks:
            lines.append(f"    uncovered consumes: {', '.join(self.leaks)}")
        if self.preconditions:
            lines.append(f"    preconditions: {', '.join(self.preconditions)}")
        for step in self.path:
            lines.append(f"    {step.format()}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Closure + refresh
# ---------------------------------------------------------------------------


def covers(produced: Port, consumed: Port, consumer: AbilitySig) -> bool:
    """Does ``produced`` cover ``consumed`` for ``consumer``?

    The graph's :func:`~.links.ports_enable` plus one additive rule: a
    ``copy_permanent`` producer covers a ``tap`` activation cost on the copied
    card, because each fresh token arrives untapped (the Kiki-Jiki / Splinter
    Twin loop shape).
    """
    if produced.kind == "copy_permanent" and consumed.kind == "tap":
        return copy_accepts(produced, consumer)
    return ports_enable(produced, consumed, consumer)


def refresh_links(
    sigs: Sequence[AbilitySig], *, include_triggered: bool = False
) -> list[Link]:
    """Synthetic ``copy_refresh`` edges: a copy engine -> the copied card.

    Emitted when an (activated/static) copy engine produces ``copy_permanent``
    and another card has a ``tap``-cost ability the engine can legally copy, so
    the fresh token can activate it again.  Triggered copy engines are skipped
    by default (matching :func:`~.links.link_re_trigger`); pass
    ``include_triggered=True`` to include them.
    """
    out: list[Link] = []
    for engine in sigs:
        for produced in engine.produces:
            if produced.kind != "copy_permanent":
                continue
            if engine.triggers_on is not None and not include_triggered:
                continue
            for other in sigs:
                if other.card_id == engine.card_id:
                    continue
                for consumed in other.consumes:
                    if consumed.kind == "tap" and copy_accepts(produced, other):
                        out.append(Link(
                            COPY_REFRESH, "copy_to_tap", engine, other,
                            matched=[(produced, consumed)],
                            motif=COPY_REFRESH_MOTIF,
                        ))
    return out


def augment_links(
    sigs: Sequence[AbilitySig],
    links: Sequence[Link],
    *,
    include_refresh: bool = True,
    include_triggered: bool = False,
) -> list[Link]:
    """Flow links (``enables``/``re_trigger``/``satisfies``) + refresh edges."""
    flow = [link for link in links if link.kind in FLOW_KINDS]
    if include_refresh:
        flow = flow + refresh_links(sigs, include_triggered=include_triggered)
    return flow


# ---------------------------------------------------------------------------
# Cycle enumeration + validation
# ---------------------------------------------------------------------------


def enumerate_cycles(
    links: Sequence[Link], *, max_len: int = 3
) -> list[tuple[tuple[int, ...], tuple[Link, ...]]]:
    """Closed directed cycles (card ids, ordered links) up to ``max_len`` cards.

    Deterministic; each card set is reported once (the first cycle found for it
    in sorted order).  Bounded by the (small) input graph; intended for a
    pairing or a handful of cards, not a whole corpus.
    """
    adjacency: dict[int, list[Link]] = defaultdict(list)
    for link in links:
        adjacency[link.src.card_id].append(link)

    found: dict[frozenset[int], tuple[tuple[int, ...], tuple[Link, ...]]] = {}
    for start in sorted(adjacency):
        stack: list[tuple[int, tuple[int, ...], tuple[Link, ...]]] = [
            (start, (start,), ())
        ]
        while stack:
            node, path, trail = stack.pop()
            for link in adjacency.get(node, ()):
                nxt = link.dst.card_id
                if nxt == start and len(path) >= 2:
                    key = frozenset(path)
                    found.setdefault(key, (path, trail + (link,)))
                elif nxt not in path and len(path) < max_len:
                    stack.append((nxt, path + (nxt,), trail + (link,)))
    return [found[key] for key in sorted(found, key=lambda k: tuple(sorted(k)))]


def _cycle_abilities(links: Sequence[Link]) -> list[AbilitySig]:
    abilities: list[AbilitySig] = []
    seen: set[tuple[int, str]] = set()
    for link in links:
        for sig in (link.src, link.dst):
            if sig.key not in seen:
                seen.add(sig.key)
                abilities.append(sig)
    return abilities


def resource_leaks(abilities: Sequence[AbilitySig]) -> tuple[str, ...]:
    """Uncovered ``consumes`` ports of a cycle (why closure fails)."""
    leaks: list[str] = []
    for holder in abilities:
        for consumed in holder.consumes:
            if not any(covers(produced, consumed, holder)
                       for other in abilities for produced in other.produces):
                leaks.append(f"{holder.card_name}:{consumed.kind}")
    return tuple(dict.fromkeys(leaks))


def resource_closed(abilities: Sequence[AbilitySig]) -> bool:
    """Every ``consumes`` port of every ability is covered inside the cycle."""
    return not resource_leaks(abilities)


def cap_gates(abilities: Sequence[AbilitySig]) -> tuple[str, ...]:
    """Cap-gate kinds present on the cycle (once per turn/combat/game)."""
    return tuple(dict.fromkeys(
        gate.kind for holder in abilities for gate in holder.gates
        if gate.kind in CAP_GATES
    ))


def evaluate_cycle(
    links: Sequence[Link],
) -> tuple[bool, tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """``(closed, caps, preconditions, motifs, leaks)`` for one ordered cycle."""
    abilities = _cycle_abilities(links)
    leaks = resource_leaks(abilities)
    closed = not leaks
    caps = cap_gates(abilities)
    preconditions = _gate_preconditions(abilities)
    motifs = tuple(dict.fromkeys(link.motif for link in links if link.motif))
    return closed, caps, preconditions, motifs, leaks


def _cycle_steps(links: Sequence[Link]) -> tuple[CycleStep, ...]:
    return tuple(
        CycleStep(link.src.card_name, link.kind, link.subkind,
                  link.dst.card_name, link.motif)
        for link in links
    )


def check_signatures(
    sigs: Sequence[AbilitySig],
    *,
    links: Sequence[Link] | None = None,
    max_len: int = 3,
    options: LinkOptions | None = None,
    weights: dict[str, float] | None = None,
    include_refresh: bool = True,
    include_triggered: bool = False,
) -> CycleEvidence:
    """Check a small set of ability signatures for an unbounded closed cycle."""
    sigs = list(sigs)
    cards = tuple(dict.fromkeys(sig.card_name for sig in sigs))
    if len(sigs) < 2:
        return CycleEvidence(cards=cards, notes=("fewer than two abilities",))
    if links is None:
        links = build_links(
            sigs, weights=weights, options=options or LinkOptions(),
        )
    flow = augment_links(
        sigs, links,
        include_refresh=include_refresh, include_triggered=include_triggered,
    )
    best: CycleEvidence | None = None
    for _cards, cycle in enumerate_cycles(flow, max_len=max_len):
        closed, caps, preconditions, motifs, leaks = evaluate_cycle(cycle)
        unbounded = closed and not caps
        evidence = CycleEvidence(
            cards=tuple(dict.fromkeys(
                link.src.card_name for link in cycle
            )) or cards,
            closed=closed,
            unbounded=unbounded,
            signal=(SIGNAL_UNBOUNDED if unbounded
                    else SIGNAL_CAPPED if closed else SIGNAL_NONE),
            caps=caps,
            leaks=leaks,
            preconditions=preconditions,
            motifs=motifs,
            path=_cycle_steps(cycle),
        )
        if best is None or _evidence_rank(evidence) < _evidence_rank(best):
            best = evidence
    if best is None:
        return CycleEvidence(cards=cards, notes=("no closed cycle",))
    return best


def _evidence_rank(evidence: CycleEvidence) -> tuple[Any, ...]:
    """Prefer unbounded, then closed, then a shorter path (smaller is better)."""
    return (
        not evidence.unbounded,
        not evidence.closed,
        len(evidence.path),
        evidence.cards,
    )


# ---------------------------------------------------------------------------
# Corpus-backed helpers
# ---------------------------------------------------------------------------


def resolve_card_id(contexts: Mapping[int, CardContext], name: str) -> int | None:
    """Resolve a card name (or id string) to a corpus card id."""
    from ..corpus.importer import normalize_name

    text = str(name).strip()
    if text.isdigit() and int(text) in contexts:
        return int(text)
    wanted = normalize_name(text)
    for card_id, context in contexts.items():
        if context.normalized_name == wanted or context.name == text:
            return card_id
    return None


def check_card_ids(
    contexts: Mapping[int, CardContext],
    effects: Mapping[int, Sequence[CardEffect]],
    card_ids: Iterable[int],
    **kwargs: Any,
) -> CycleEvidence:
    """Build signatures for ``card_ids`` and run the gate on them."""
    wanted = [int(cid) for cid in card_ids]
    sub_contexts = {cid: contexts[cid] for cid in wanted if cid in contexts}
    sub_effects = {cid: effects.get(cid, ()) for cid in wanted if cid in contexts}
    if len(sub_contexts) < 2:
        names = tuple(contexts[cid].name for cid in wanted if cid in contexts)
        return CycleEvidence(cards=names, notes=("fewer than two known cards",))
    sigs = build_signatures(sub_contexts, sub_effects)
    return check_signatures(sigs, **kwargs)


# ---------------------------------------------------------------------------
# Engine class + candidate-finder score (for the validation comparison)
# ---------------------------------------------------------------------------

_ACTIVATED_COPY = re.compile(r"\{t\}.*(create a token|copy)", re.IGNORECASE)


def _is_copy(text: str) -> bool:
    return "create a token" in text and "copy" in text


def _is_untap(text: str) -> bool:
    return "untap" in text


def engine_class(card_ids: Iterable[int], contexts: Mapping[int, CardContext]) -> str:
    """``activated`` / ``triggered`` / ``none`` copy-engine class of a pair.

    Mirrors ``scripts/known_recall.py`` exactly so the validation split is the
    same one the witness sweep used.
    """
    for cid in card_ids:
        context = contexts.get(cid)
        text = (context.oracle_text if context else "") or ""
        text = text.lower()
        if _is_copy(text):
            return "activated" if _ACTIVATED_COPY.search(text) else "triggered"
    return "none"


def candidate_finder_score(
    contexts: Mapping[int, CardContext],
    effects: Mapping[int, Sequence[CardEffect]],
    card_ids: Iterable[int],
    *,
    cache: dict[int, list[Any]] | None = None,
) -> float:
    """Reproduce the legacy ``combo_hypotheses.score`` for a pair.

    Runs the same ``build_edges`` pattern registry the candidate finder uses, so
    a pair that is in the generated set reproduces its persisted score and a
    pair that is not gets the score it *would* have received (0.0 when no pattern
    matches).  This is the fair "existing score doing the same job" baseline.
    """
    views: dict[int, CardView] = {}
    for cid in card_ids:
        if cid not in contexts:
            continue
        if cache is not None and cid in cache:
            predicates = cache[cid]
        else:
            predicates = extract_card_predicates(
                contexts[cid], list(effects.get(cid, ()))
            )
            if cache is not None:
                cache[cid] = predicates
        views[cid] = CardView.build(
            contexts[cid], predicates, effects.get(cid, ())
        )
    if len(views) < 2:
        return 0.0
    edges = build_edges(views)
    return max((edge.score for edge in edges), default=0.0)


# ---------------------------------------------------------------------------
# Retroactive validation
# ---------------------------------------------------------------------------

#: Default witness run ranges: the 422-pair engine-class Known-Combo Check and
#: the 133 ``home`` combos.
DEFAULT_RUN_RANGES: tuple[tuple[int, int], ...] = ((1586, 2008), (169, 301))


@dataclass
class VerdictRow:
    """One persisted witness verdict joined to its static / legacy signals."""

    run_id: int
    verdict: str
    cards: tuple[str, ...]
    card_ids: tuple[int, ...]
    engine_class: str
    evidence: CycleEvidence
    score: float
    existing_score: float = 0.0

    @property
    def loop(self) -> bool:
        return self.verdict == "loops"

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "verdict": self.verdict,
            "cards": list(self.cards),
            "engine_class": self.engine_class,
            "signal": self.evidence.signal,
            "closed": self.evidence.closed,
            "unbounded": self.evidence.unbounded,
            "caps": list(self.evidence.caps),
            "path": [step.as_dict() for step in self.evidence.path],
            "score": self.score,
            "existing_score": self.existing_score,
        }


def load_persisted_scores(conn: Any) -> dict[tuple[int, int], float]:
    """Max persisted ``combo_hypotheses.score`` per 2-card pair."""
    scores: dict[tuple[int, int], float] = {}
    for record in conn.execute(
        "SELECT card_ids_json, score FROM combo_hypotheses"
    ):
        ids = json.loads(record["card_ids_json"])
        if len(ids) != 2:
            continue
        key = tuple(sorted(int(i) for i in ids))
        scores[key] = max(scores.get(key, 0.0), float(record["score"]))
    return scores


def load_verdicts(
    conn: Any,
    contexts: Mapping[int, CardContext],
    effects: Mapping[int, Sequence[CardEffect]],
    *,
    run_ranges: Sequence[tuple[int, int]] = DEFAULT_RUN_RANGES,
    include_refresh: bool = True,
    include_triggered: bool = False,
    max_len: int = 3,
) -> tuple[list[VerdictRow], int]:
    """Load witness verdicts and compute both signals for each pair.

    Returns ``(rows, unresolved)`` where ``unresolved`` counts verdict rows whose
    two card names could not both be resolved to corpus ids.

    ``score`` is the reproduced candidate-finder (legacy pattern) score;
    ``existing_score`` is ``max(score, persisted combo_hypotheses.score)``, so
    the baseline also carries the algebra ``q:`` scores the legacy registry
    cannot reproduce.  That is the fairest "existing score doing the same job".
    """
    from ..corpus.importer import normalize_name

    id_by_norm: dict[str, int] = {}
    for cid, context in contexts.items():
        id_by_norm.setdefault(context.normalized_name, cid)

    persisted = load_persisted_scores(conn)
    query = (
        "SELECT run_id, verdict, card_names_json FROM witness_results "
        "WHERE " + " OR ".join(
            "run_id BETWEEN ? AND ?" for _ in run_ranges
        ) + " ORDER BY run_id, id"
    )
    params = [value for span in run_ranges for value in span]
    cache: dict[int, list[Any]] = {}
    rows: list[VerdictRow] = []
    unresolved = 0
    for record in conn.execute(query, params):
        names = json.loads(record["card_names_json"])
        card_ids = tuple(sorted(
            cid for cid in (
                id_by_norm.get(normalize_name(name)) for name in names
            ) if cid is not None
        ))
        if len(card_ids) < 2:
            unresolved += 1
            continue
        evidence = check_card_ids(
            contexts, effects, card_ids,
            max_len=max_len,
            include_refresh=include_refresh,
            include_triggered=include_triggered,
        )
        score = candidate_finder_score(contexts, effects, card_ids, cache=cache)
        rows.append(VerdictRow(
            run_id=int(record["run_id"]),
            verdict=str(record["verdict"]),
            cards=tuple(names),
            card_ids=card_ids,
            engine_class=engine_class(card_ids, contexts),
            evidence=evidence,
            score=score,
            existing_score=max(score, persisted.get(card_ids, 0.0)),
        ))
    return rows, unresolved


def _auc(scores: Sequence[float], positive: Sequence[bool]) -> float:
    """Rank AUC (Mann-Whitney), ``0.5`` when one class is empty."""
    pos = [s for s, p in zip(scores, positive, strict=True) if p]
    neg = [s for s, p in zip(scores, positive, strict=True) if not p]
    if not pos or not neg:
        return 0.5
    wins = sum(1.0 for p in pos for n in neg if p > n)
    ties = sum(0.5 for p in pos for n in neg if p == n)
    return (wins + ties) / (len(pos) * len(neg))


def _confusion(
    rows: Sequence[VerdictRow], predict
) -> dict[str, Any]:
    tp = fp = fn = tn = 0
    for row in rows:
        hit = bool(predict(row))
        if row.loop and hit:
            tp += 1
        elif row.loop:
            fn += 1
        elif hit:
            fp += 1
        else:
            tn += 1
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return {
        "n": len(rows), "loops": tp + fn,
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": round(precision, 4), "recall": round(recall, 4),
    }


def validate(
    rows: Sequence[VerdictRow],
    *,
    threshold: float = 0.5,
) -> dict[str, Any]:
    """Precision / recall / separation of the static signal vs the existing score.

    ``inconclusive`` verdicts are excluded from the predictor evaluation (they
    are counted separately).  ``refuted`` and ``no_loop`` are both treated as
    negatives: neither is a confirmed loop.  The baseline is ``existing_score``
    (persisted ``combo_hypotheses.score`` where present, else the reproduced
    legacy pattern score).
    """
    from collections import Counter

    verdicts = Counter(row.verdict for row in rows)
    excluded = verdicts.get("inconclusive", 0)
    scored = [row for row in rows if row.verdict != "inconclusive"]

    def _block(subset: Sequence[VerdictRow]) -> dict[str, Any]:
        if not subset:
            return {}
        positive = [row.loop for row in subset]
        static_scores = [row.evidence.signal for row in subset]
        existing_scores = [row.existing_score for row in subset]
        return {
            "static_unbounded": _confusion(
                subset, lambda r: r.evidence.unbounded
            ),
            "static_closed": _confusion(subset, lambda r: r.evidence.closed),
            "static_threshold": _confusion(
                subset, lambda r: r.evidence.signal >= threshold
            ),
            "existing_score_gt0": _confusion(
                subset, lambda r: r.existing_score > 0
            ),
            "legacy_score_gt0": _confusion(subset, lambda r: r.score > 0),
            "static_auc": round(_auc(static_scores, positive), 4),
            "existing_auc": round(_auc(existing_scores, positive), 4),
            "legacy_auc": round(_auc([r.score for r in subset], positive), 4),
        }

    result: dict[str, Any] = {
        "verdicts": dict(verdicts),
        "inconclusive_excluded": excluded,
        "scored": len(scored),
        "overall": _block(scored),
        "by_engine": {},
    }
    for engine in ("activated", "triggered", "none"):
        subset = [row for row in scored if row.engine_class == engine]
        if subset:
            result["by_engine"][engine] = _block(subset)
    return result


__all__ = [
    "CAP_GATES",
    "COPY_REFRESH",
    "COPY_REFRESH_MOTIF",
    "DEFAULT_RUN_RANGES",
    "FLOW_KINDS",
    "SIGNAL_CAPPED",
    "SIGNAL_NONE",
    "SIGNAL_UNBOUNDED",
    "CycleEvidence",
    "CycleStep",
    "VerdictRow",
    "augment_links",
    "cap_gates",
    "candidate_finder_score",
    "check_card_ids",
    "check_signatures",
    "covers",
    "engine_class",
    "enumerate_cycles",
    "evaluate_cycle",
    "load_persisted_scores",
    "load_verdicts",
    "refresh_links",
    "resolve_card_id",
    "resource_closed",
    "resource_leaks",
    "validate",
]
