"""Cycle construction and combo discovery over the interaction algebra.

* :func:`build_graph` turns ability signatures into a card-level graph whose
  edges are ability :class:`~combo_discovery.ontology.links.Link` instances;
* :func:`find_combos` enumerates re-entrant cycles, validates resource closure
  and gates, names them with the cycle queries, and scores them with the
  ground-truth motif enrichment.

**Why card-level cycles.**  A combo is a set of cards, but the interactions are
ability-scoped.  Nodes are cards; edges are ability links.  The graph search is
anchored on ``re_trigger`` links (rare) and is bounded by ``max_len`` cards, so
the (large) ``enables`` fan-out only has to be explored around those anchors.

**Validity rules** (documented, conservative):

1. *re-entrant* — the cycle contains at least one ``re_trigger`` link (a fresh
   token/phase/reanimation fires an ability again);
2. *resource-closed* — every ``consumes`` port of every ability in the cycle is
   covered by some ``produces`` port in the cycle (type-compatibly);
3. *gates conservatively satisfiable* — a gate is discharged by a **different
   card's** ability in the cycle (a card cannot bootstrap its own gate).  A gate
   that is not discharged is carried as a precondition, never a rejection.
4. *net resources non-decreasing* — the cycle contains a repeatable producer
   (mana/token/damage/extra_phase/counters/life) and no uncovered external
   requirement, so it is flagged ``infinite``.  This is deliberately a simple
   model: it does **not** track quantities, timing, stack order, summoning
   sickness or interaction between the produced resources.

**Scoring.**  ``score = base(query) × Π enrichment_weight(link motif)`` where the
motifs are the same cross-card tokens the enrichment analysis stores
(``COPIES_CREATURE~ETB_TRIGGER``, ...).  Weights come from the latest
``motif_enrichment`` run when available (lift clipped to ``[0.1, 2.0]``), else
1.0.  Any link whose motif weight is below :data:`HOSTILE_LIFT` suppresses the
cycle.  The product is clamped to 1.0.  The whole scheme is transparent and
exposed via :func:`load_motif_weights`.
"""

from __future__ import annotations

import math
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping, Sequence

from .budget import (
    DEFAULT_MAX_SECONDS,
    DEFAULT_MAX_STEPS,
    MAX_SAFE_POOL,
    SearchBudget,
    estimate_cost,
    preflight,
)
from .links import (
    HOSTILE_LIFT,
    Link,
    LinkOptions,
    _port_satisfies_gate,
    build_links,
    link_hostile,
    link_weight,
    ports_enable,
)
from .ports import AbilitySig
from .queries import ComboContext, Query, matching_queries


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------


@dataclass
class Graph:
    """A card-level interaction graph over ability signatures.

    ``truncated`` / ``truncation_reason`` report that a hard budget stopped the
    build early, so ``links``/``re_triggers`` are a partial (deterministic)
    prefix rather than the full graph.
    """

    sigs: tuple[AbilitySig, ...]
    links: tuple[Link, ...]
    by_card: dict[int, tuple[AbilitySig, ...]]
    names: dict[int, str]
    adjacency: dict[int, tuple[Link, ...]]
    re_triggers: tuple[Link, ...]
    truncated: bool = False
    truncation_reason: str | None = None
    build_seconds: float = 0.0
    budget_report: dict[str, Any] | None = None
    cost_estimate: dict[str, Any] | None = None
    warnings: tuple[str, ...] = ()

    @property
    def nodes(self) -> tuple[int, ...]:
        return tuple(sorted(self.by_card))


def build_graph(
    sigs: Sequence[AbilitySig],
    *,
    weights: dict[str, float] | None = None,
    pool: Iterable[int] | None = None,
    options: LinkOptions | None = None,
    budget: SearchBudget | None = None,
    max_seconds: float | None = DEFAULT_MAX_SECONDS,
    max_steps: int | None = DEFAULT_MAX_STEPS,
    allow_over_budget: bool = False,
) -> Graph:
    """Build the card-level graph under a hard budget.

    A cost pre-flight (:func:`~.budget.preflight`) refuses a pool above
    :data:`~.budget.MAX_SAFE_POOL` and clamps ``closure_depth`` above
    ``MAX_SAFE_CLOSURE_DEPTH`` unless ``allow_over_budget`` is set.  The build
    itself is bounded by ``max_seconds`` / ``max_steps`` (never unbounded) and
    returns a partial graph with ``truncated=True`` + a reason when the budget is
    exhausted.
    """
    selected = list(sigs)
    if pool is not None:
        allowed = set(pool)
        selected = [s for s in selected if s.card_id in allowed]
    options = options or LinkOptions()
    pool_cards = len({s.card_id for s in selected})
    # ``closure_depth`` only shapes a scoped (retrigger) build; the full build
    # ignores it, so don't warn/clamp for a scope=None call.
    check_depth = options.closure_depth if options.scope == "retrigger" else 0
    closure_depth, preflight_warnings = preflight(
        pool_cards, check_depth, allow_over_budget=allow_over_budget
    )
    if options.scope == "retrigger" and closure_depth != options.closure_depth:
        options = replace(options, closure_depth=closure_depth)
    cost = estimate_cost(pool_cards, options.closure_depth)
    budget = budget or SearchBudget(max_seconds, max_steps)

    started = time.monotonic()
    links = build_links(selected, weights=weights, options=options, budget=budget)
    build_seconds = time.monotonic() - started

    by_card: dict[int, list[AbilitySig]] = defaultdict(list)
    names: dict[int, str] = {}
    for sig in selected:
        by_card[sig.card_id].append(sig)
        names.setdefault(sig.card_id, sig.card_name)
    adjacency: dict[int, list[Link]] = defaultdict(list)
    for link in links:
        adjacency[link.src.card_id].append(link)
    re_triggers = tuple(link for link in links if link.kind == "re_trigger")
    return Graph(
        sigs=tuple(selected),
        links=tuple(links),
        by_card={cid: tuple(sorted(v, key=lambda s: s.ability_ref))
                 for cid, v in by_card.items()},
        names=names,
        adjacency={cid: tuple(sorted(v, key=lambda l: (
            l.dst.card_id, l.kind, l.subkind, l.motif)))
            for cid, v in adjacency.items()},
        re_triggers=re_triggers,
        truncated=budget.truncated,
        truncation_reason=budget.reason,
        build_seconds=round(build_seconds, 3),
        budget_report=budget.report(),
        cost_estimate=cost,
        warnings=preflight_warnings,
    )


# ---------------------------------------------------------------------------
# Combo
# ---------------------------------------------------------------------------


@dataclass
class Combo:
    """A validated interaction cycle."""

    card_ids: tuple[int, ...]
    cards: tuple[str, ...]
    abilities: tuple[AbilitySig, ...]
    links: tuple[Link, ...]
    score: float
    mechanism: str
    preconditions: tuple[str, ...] = ()
    patterns: tuple[str, ...] = ()
    infinite: bool = False
    motifs: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "cards": list(self.cards),
            "card_ids": list(self.card_ids),
            "score": self.score,
            "patterns": list(self.patterns),
            "preconditions": list(self.preconditions),
            "infinite": self.infinite,
            "motifs": list(self.motifs),
            "mechanism": self.mechanism,
            "abilities": [a.ability_ref for a in self.abilities],
            "links": [
                {"kind": l.kind, "subkind": l.subkind, "motif": l.motif,
                 "src": l.src.card_name, "dst": l.dst.card_name}
                for l in self.links
            ],
        }

    def format(self) -> str:
        tags = ", ".join(self.patterns)
        pre = f"  preconditions: {', '.join(self.preconditions)}" if self.preconditions else ""
        return (
            f"[{tags}] score={self.score:.3f} infinite={self.infinite}  "
            f"{' + '.join(self.cards)}\n    {self.mechanism}" + pre
        )


class ComboList(list):
    """A ``list`` of :class:`Combo` that also reports whether the search was cut
    short by a hard budget.

    Subclassing ``list`` keeps the historical ``find_combos`` return value
    (iteration, indexing, ``== []``) while carrying the truncation metadata.
    """

    truncated: bool = False
    truncation_reason: str | None = None
    budget_report: dict[str, Any] | None = None
    graph_truncated: bool = False
    graph_truncation_reason: str | None = None


# ---------------------------------------------------------------------------
# Cycle enumeration
# ---------------------------------------------------------------------------


def _enumerate_cycles(
    graph: Graph,
    max_len: int,
    max_cycles: int = 20_000,
    budget: SearchBudget | None = None,
) -> list[tuple[tuple[int, ...], tuple[Link, ...]]]:
    """Re-entrant cycles as ``(ordered card ids, ordered links)``.

    Anchored on every ``re_trigger`` link ``u -> v``; a cycle is a path from
    ``v`` back to ``u`` with at most ``max_len`` distinct cards.  For the
    supported default (``max_len <= 3``) the path is short enough to close with
    a deterministic first-link lookup (``reach`` / ``incoming``) instead of a
    DFS, which avoids exploring the enormous enable fan-out of high-degree
    nodes.  Longer cycles fall back to a bounded DFS.  Deterministic; bounded by
    ``max_cycles`` and by ``budget`` (never unbounded).

    The budget is stepped once per anchor (and once per DFS pop in the
    fallback); a per-edge count would exhaust a small step budget before doing
    useful search.
    """
    budget = budget or SearchBudget()
    adjacency = graph.adjacency

    cycles: list[tuple[tuple[int, ...], tuple[Link, ...]]] = []
    seen: set[frozenset[int]] = set()

    def record(ordered_cards: Sequence[int], ordered_links: Sequence[Link]) -> bool:
        cards = tuple(dict.fromkeys(ordered_cards))
        key = frozenset(cards)
        if len(key) < 2 or key in seen:
            return True
        seen.add(key)
        cycles.append((cards, tuple(ordered_links)))
        if len(cycles) >= max_cycles:
            budget.trip(f"max_cycles={max_cycles} reached")
            return False
        return True

    if max_len <= 3:
        # Deterministic first link for each oriented card pair, and the links
        # entering each card (used to close a 3-card path).
        reach: dict[tuple[int, int], Link] = {}
        incoming: dict[int, list[Link]] = {}
        for cid in sorted(adjacency):
            for link in adjacency[cid]:
                reach.setdefault((link.src.card_id, link.dst.card_id), link)
                incoming.setdefault(link.dst.card_id, []).append(link)
        for anchor in graph.re_triggers:
            if not budget.tick():
                return cycles
            u, v = anchor.pair
            if u == v:
                continue
            # 2-card cycle: v -> u directly.
            closing = reach.get((v, u))
            if closing is not None and not record([u, v], [anchor, closing]):
                return cycles
            if max_len < 3:
                continue
            # 3-card cycle: u -> v -> w -> u.  Iterate the smaller side.
            # Work per anchor is bounded by the adjacency/indegree size, so a
            # single tick per anchor keeps the step budget meaningful.
            outs = adjacency.get(v, ())
            ins = incoming.get(u, ())
            if len(ins) <= len(outs):
                for back in ins:
                    w = back.src.card_id
                    if w in (u, v):
                        continue
                    forward = reach.get((v, w))
                    if forward is not None and not record([u, v, w], [anchor, forward, back]):
                        return cycles
            else:
                for forward in outs:
                    w = forward.dst.card_id
                    if w in (u, v):
                        continue
                    back = reach.get((w, u))
                    if back is not None and not record([u, v, w], [anchor, forward, back]):
                        return cycles
        return cycles

    # Fallback for longer cycles: bounded, budgeted DFS.
    for anchor in graph.re_triggers:
        if not budget.tick():
            return cycles
        u, v = anchor.pair
        if u == v:
            continue
        stack: list[tuple[int, list[Link], frozenset[int]]] = [
            (v, [anchor], frozenset({u, v}))
        ]
        while stack:
            if not budget.tick():
                return cycles
            node, path, visited = stack.pop()
            for link in adjacency.get(node, ()):
                nxt = link.dst.card_id
                if nxt == u:
                    if not record(
                        [path[0].src.card_id] + [p.dst.card_id for p in path],
                        tuple(path + [link]),
                    ):
                        return cycles
                elif nxt not in visited and len(visited) < max_len:
                    stack.append((nxt, path + [link], visited | {nxt}))
    return cycles


# ---------------------------------------------------------------------------
# Validation + scoring
# ---------------------------------------------------------------------------


def _resource_closed(abilities: Sequence[AbilitySig]) -> bool:
    for consumer in abilities:
        for consumed in consumer.consumes:
            if not any(
                ports_enable(produced, consumed, consumer)
                for producer in abilities
                for produced in producer.produces
            ):
                return False
    return True


def _gate_preconditions(abilities: Sequence[AbilitySig]) -> tuple[str, ...]:
    """Gates not discharged by a *different* card in the cycle."""
    preconditions: list[str] = []
    for holder in abilities:
        for gate in holder.gates:
            discharged = any(
                other.card_id != holder.card_id
                and any(_port_satisfies_gate(produced, gate, holder)
                        for produced in other.produces)
                for other in abilities
            )
            if not discharged:
                preconditions.append(f"{holder.card_name}:{gate.kind}")
    return tuple(dict.fromkeys(preconditions))


def _motif_score(links: Sequence[Link], weights: Mapping[str, float] | None) -> float:
    score = 1.0
    for link in links:
        score *= link_weight(link, dict(weights) if weights else None)
    return score


def _mechanism(cards: Sequence[int], links: Sequence[Link], graph: Graph) -> str:
    names = graph.names
    parts: list[str] = []
    for link in links:
        src = names.get(link.src.card_id, link.src.card_name)
        dst = names.get(link.dst.card_id, link.dst.card_name)
        if link.kind == "re_trigger":
            verb = {
                "copy": "copies",
                "combat": "adds an extra combat for",
                "reanimate": "reanimates",
                "flicker": "flickers",
            }.get(link.subkind, "re-triggers")
            parts.append(f"{src} {verb} {dst} ({link.motif})")
        elif link.kind == "enables":
            parts.append(f"{src} feeds {dst} ({link.motif})")
        elif link.kind == "satisfies":
            parts.append(f"{src} satisfies {dst} ({link.motif})")
        else:
            parts.append(f"{src} -> {dst} ({link.motif})")
    return "; ".join(parts) + "."


def _make_combo(
    ordered_cards: Sequence[int],
    ordered_links: Sequence[Link],
    graph: Graph,
    weights: dict[str, float] | None,
) -> Combo | None:
    if len(set(ordered_cards)) < 2:
        return None
    abilities: list[AbilitySig] = []
    seen_abilities: set[tuple[int, str]] = set()
    for link in ordered_links:
        for sig in (link.src, link.dst):
            if sig.key not in seen_abilities:
                seen_abilities.add(sig.key)
                abilities.append(sig)
    if not any(link.kind == "re_trigger" for link in ordered_links):
        return None
    if not _resource_closed(abilities):
        return None

    # Hostile motifs suppress the cycle.
    for left in abilities:
        for right in abilities:
            if left.key == right.key:
                continue
            if link_hostile(left, right, weights) is not None:
                return None

    context = ComboContext(abilities=tuple(abilities), links=tuple(ordered_links))
    matched: tuple[Query, ...] = matching_queries(context)
    if not matched:
        return None
    base = max(query.base_score for query in matched)
    weight_product = _motif_score(ordered_links, weights)
    score = min(1.0, base * weight_product)

    preconditions = _gate_preconditions(abilities)
    infinite = any(
        p.kind in ("mana", "token", "damage", "extra_phase", "counters",
                   "life_gain", "copy_permanent")
        for ability in abilities for p in ability.produces
    )
    motifs = tuple(dict.fromkeys(
        link.motif for link in ordered_links if link.motif
    ))
    return Combo(
        card_ids=tuple(ordered_cards),
        cards=tuple(graph.names.get(cid, str(cid)) for cid in ordered_cards),
        abilities=tuple(abilities),
        links=tuple(ordered_links),
        score=round(score, 6),
        mechanism=_mechanism(ordered_cards, ordered_links, graph),
        preconditions=preconditions,
        patterns=tuple(query.name for query in matched),
        infinite=infinite,
        motifs=motifs,
    )


def find_combos(
    sigs: Sequence[AbilitySig],
    *,
    weights: dict[str, float] | None = None,
    max_len: int = 3,
    min_score: float = 0.0,
    pool: Iterable[int] | None = None,
    options: LinkOptions | None = None,
    graph: Graph | None = None,
    max_cycles: int = 1_000_000,
    max_seconds: float | None = DEFAULT_MAX_SECONDS,
    max_steps: int | None = DEFAULT_MAX_STEPS,
    allow_over_budget: bool = False,
    budget: SearchBudget | None = None,
) -> ComboList:
    """Enumerate, validate and score re-entrant cycles under a hard budget.

    ``max_len`` is the maximum number of distinct cards in a cycle (default 3).
    Results are deduplicated by card set (best score wins) and sorted by
    descending score.  ``max_seconds`` / ``max_steps`` bound the whole call
    (graph build + enumeration); the returned :class:`ComboList` carries
    ``truncated`` / ``truncation_reason`` and never runs unbounded.  A pool over
    the safe threshold is refused unless ``allow_over_budget=True``.
    """
    budget = budget or SearchBudget(max_seconds, max_steps)
    graph_truncated = False
    graph_reason: str | None = None
    if graph is None:
        graph = build_graph(
            sigs, weights=weights, pool=pool, options=options, budget=budget,
            max_seconds=max_seconds, max_steps=max_steps,
            allow_over_budget=allow_over_budget,
        )
        graph_truncated = graph.truncated
        graph_reason = graph.truncation_reason
    else:
        graph_truncated = bool(getattr(graph, "truncated", False))
        graph_reason = getattr(graph, "truncation_reason", None)

    best: dict[frozenset[int], Combo] = {}
    for cards, links in _enumerate_cycles(graph, max_len, max_cycles=max_cycles,
                                          budget=budget):
        if not budget.tick():
            break
        combo = _make_combo(cards, links, graph, weights)
        if combo is None or combo.score < min_score:
            continue
        key = frozenset(combo.card_ids)
        current = best.get(key)
        if current is None or combo.score > current.score:
            best[key] = combo
    combos = ComboList(sorted(
        best.values(), key=lambda c: (-c.score, c.cards)
    ))
    combo_truncated = budget.truncated
    combos.truncated = bool(graph_truncated or combo_truncated)
    if combo_truncated:
        combos.truncation_reason = budget.reason
    elif graph_truncated:
        combos.truncation_reason = graph_reason
    combos.graph_truncated = graph_truncated
    combos.graph_truncation_reason = graph_reason
    combos.budget_report = budget.report()
    return combos


# ---------------------------------------------------------------------------
# Pool helpers + high-level discovery
# ---------------------------------------------------------------------------


def _loop_machinery(sig: AbilitySig) -> bool:
    """A card carrying a loop-relevant typed port (the tight default pool)."""
    if any(p.kind in ("copy_permanent", "extra_phase", "untap")
           for p in sig.produces):
        return True
    if any(p.kind == "zone_move"
           and str(p.params.get("to") or "").lower() == "battlefield"
           for p in sig.produces):
        return True
    if any(p.kind == "sacrifice" for p in sig.consumes):
        return True
    if any(p.kind == "zone_move"
           and "graveyard" in str(p.params.get("from") or "").lower()
           for p in sig.produces):
        return True
    return False


def loop_machinery_cards(sigs: Iterable[AbilitySig]) -> tuple[int, ...]:
    """Card ids carrying loop machinery: copy/extra-phase/untap/reanimation/
    sacrifice.  This is the tight (``<= ~7k``) default search pool."""
    return tuple(sorted({s.card_id for s in sigs if _loop_machinery(s)}))


def tight_pool(
    sigs: Iterable[AbilitySig],
    names: Mapping[int, str] | None = None,
    legality=None,
) -> tuple[int, ...]:
    """Loop-machinery cards, optionally filtered to the Vintage-legal subset."""
    return vintage_pool(loop_machinery_cards(sigs), names or {}, legality)


def discover_combos(
    sigs: Sequence[AbilitySig],
    *,
    names: Mapping[int, str] | None = None,
    legality=None,
    weights: dict[str, float] | None = None,
    broad: bool = False,
    pool: Iterable[int] | None = None,
    max_len: int = 3,
    max_cycles: int = 1_000_000,
    options: LinkOptions | None = None,
    max_seconds: float | None = DEFAULT_MAX_SECONDS,
    max_steps: int | None = DEFAULT_MAX_STEPS,
    allow_over_budget: bool = False,
) -> tuple[Graph, ComboList]:
    """Tight-by-default discovery: build the scoped graph and find combos.

    Broad corpora are opt-in (``broad=True`` / explicit ``pool``) and, above the
    safe threshold, require ``allow_over_budget=True``.  The two phases share
    one wall-clock/step budget so the whole call stays bounded.
    """
    if pool is None and not broad:
        pool = tight_pool(sigs, names, legality)
    opts = options or LinkOptions.safe()
    budget = SearchBudget(max_seconds, max_steps)
    graph = build_graph(
        sigs, weights=weights, pool=pool, options=opts, budget=budget,
        max_seconds=max_seconds, max_steps=max_steps,
        allow_over_budget=allow_over_budget,
    )
    combos = find_combos(
        sigs, weights=weights, max_len=max_len, pool=pool, options=opts,
        graph=graph, max_cycles=max_cycles, max_seconds=max_seconds,
        max_steps=max_steps, allow_over_budget=allow_over_budget, budget=budget,
    )
    return graph, combos


# ---------------------------------------------------------------------------
# Enrichment weights
# ---------------------------------------------------------------------------


def lift_to_weight(lift: float | None) -> float:
    """Clip a motif lift into a bounded multiplicative weight."""
    if lift is None:
        return 1.0
    try:
        value = float(lift)
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(value):
        return 2.0
    return min(2.0, max(0.1, value))


def load_motif_weights(
    conn: sqlite3.Connection, *, run_id: int | None = None
) -> dict[str, float]:
    """Load ``motif -> weight`` from the latest (or given) enrichment run.

    Lifts are clipped by :func:`lift_to_weight`; a missing table/run returns an
    empty dict so callers fall back to neutral weights.
    """
    try:
        if run_id is None:
            row = conn.execute("SELECT MAX(id) AS run_id FROM motif_runs").fetchone()
            run_id = row["run_id"] if row is not None else None
        if run_id is None:
            return {}
        rows = conn.execute(
            "SELECT motif, lift FROM motif_enrichment WHERE run_id = ?", (run_id,)
        ).fetchall()
    except sqlite3.Error:
        return {}
    weights: dict[str, float] = {}
    for row in rows:
        try:
            motif = row["motif"]
            lift = row["lift"]
        except (KeyError, TypeError):
            motif, lift = row[0], row[1]
        if motif:
            weights[str(motif)] = lift_to_weight(lift)
    return weights


def vintage_pool(
    card_ids: Iterable[int],
    names: Mapping[int, str],
    legality,
) -> tuple[int, ...]:
    """Vintage-legal subset of ``card_ids`` (uses the forged-format helper)."""
    return tuple(
        sorted(cid for cid in card_ids
               if legality is None or legality.is_legal(names.get(cid)))
    )


__all__ = [
    "Combo",
    "ComboList",
    "Graph",
    "build_graph",
    "discover_combos",
    "find_combos",
    "lift_to_weight",
    "load_motif_weights",
    "loop_machinery_cards",
    "tight_pool",
    "vintage_pool",
]
