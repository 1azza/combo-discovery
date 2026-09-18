"""Motif-enrichment analysis: which predicate motifs actually predict known combos?

This is the statistical replacement for hand-tuned pattern intuition.  For a
card, a **signature** is a token (a predicate name, optionally qualified, e.g.
``UNTAPS``, ``UNTAPS:creature``, ``PRODUCES_MANA:B``).  A **motif** is a set of
signatures; here we score three kinds:

* ``single`` — one signature on at least one card of a pair;
* ``pair`` — 2–3 signatures co-occurring on at least one card of a pair;
* ``card_pair`` — a signature on each of the two cards (cross-card co-occurrence),
  or an explicit *probe* token set contained in the pair's combined signatures.

For each motif we compare its occurrence rate in **known-combo pairs** (Tier-A
``known_combo_pairs`` where ``is_full_variant=1``, restricted to Vintage-legal
cards present in the corpus) against a seeded **background** of random
Vintage card pairs from the corpus.  We report raw counts, ``lift`` and a
chi-square test with 1 degree of freedom (p from ``erfc(sqrt(chi2/2))``); a
minimum known-side support filters the long tail (2 hits and huge lift is
noise).  ``DEFAULT_PROBES`` records the predicate unions behind the current
evaluation false-positive clusters so their enrichment is always measurable.

**Limits (read this before trusting a number):**

* enrichment is **correlation, not causation**: a motif that predicts known
  combos does not explain the combo's mechanism;
* the ground truth (Commander Spellbook) is a *curated, selected* corpus, so
  selection bias is baked in;
* the background is a random sample of corpus cards, not a deck-construction
  model, so it does not control for card popularity or archetype;
* the predicate vocabulary is coarse; a motif can be enriched only because its
  tokens mark a broad card category (e.g. "artifact").

The signature provider is an interface (:class:`SignatureProvider`); a future
engine-derived provider (event/snapshot signatures) implements the same
``card_signatures()`` method and slots into :func:`analyze_motifs` unchanged.
"""

from __future__ import annotations

import json
import math
import random
import sqlite3
import time
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path
from typing import Any, Protocol

from .. import store as store_module
from ..cards import utc_now as _utc_now
from ..corpus.names import pair_hash
from ..corpus.spellbook import DEFAULT_VINTAGE_FORMAT, VintageLegality
from ..store import ExperimentStore

SOURCE_KNOWN = "commander_spellbook"
_KIND_SINGLE = "single"
_KIND_PAIR = "pair"
_KIND_CROSS = "card_pair"


# ---------------------------------------------------------------------------
# Signature providers
# ---------------------------------------------------------------------------


class SignatureProvider(Protocol):
    """Yields a set of signature tokens per card id.

    Implementations must be deterministic.  A future engine-derived provider
    (signatures inferred from played game events / snapshots) implements the
    same ``card_signatures()`` method and can be passed to
    :func:`analyze_motifs` with no other change.
    """

    name: str

    def card_signatures(self) -> Mapping[int, frozenset[str]]: ...


#: Predicates whose params yield cheap, interpretable qualifier tokens.
_QUALIFIED_PREDICATES = ("PRODUCES_MANA", "UNTAPS", "COPIES_CREATURE")
_UNTAP_TYPES = frozenset({"CREATURE", "PERMANENT", "LAND", "ARTIFACT", "PLANESWALKER",
                          "ENCHANTMENT", "CARD"})
_COPY_TYPES = frozenset({"CREATURE", "PERMANENT", "ARTIFACT"})


def _qualifier_tokens(predicate: str, params: dict[str, Any]) -> set[str]:
    """Cheap predicate+qualifier tokens from the stored predicate params."""
    tokens: set[str] = set()
    if predicate == "PRODUCES_MANA":
        color = params.get("color")
        if color:
            text = str(color).strip()
            tokens.add("PRODUCES_MANA:" + ("any" if text.lower() == "any" else text))
        if params.get("x"):
            tokens.add("PRODUCES_MANA:x")
    elif predicate == "UNTAPS":
        target = params.get("target") or {}
        for alt in target.get("alternatives") or []:
            for kind in alt.get("types") or []:
                if kind in _UNTAP_TYPES:
                    tokens.add("UNTAPS:" + kind.lower())
            if alt.get("controller") == "you":
                tokens.add("UNTAPS:you")
    elif predicate == "COPIES_CREATURE":
        target = params.get("copy") or {}
        for alt in target.get("alternatives") or []:
            for kind in alt.get("types") or []:
                if kind in _COPY_TYPES:
                    tokens.add("COPIES_CREATURE:" + kind.lower())
            if alt.get("legendary") is False:
                tokens.add("COPIES_CREATURE:nonlegendary")
        if str(params.get("defined") or "").lower() == "self":
            tokens.add("COPIES_CREATURE:self")
    return tokens


class PredicateSignatureProvider:
    """Signatures from the ``card_predicates`` table for one corpus import.

    Token = predicate name; with ``include_qualifiers`` also the cheap
    ``PREDICATE:qualifier`` tokens (``UNTAPS:creature``, ``PRODUCES_MANA:B``, …).
    A card's set is the union across its faces.
    """

    name = "card_predicates"

    def __init__(
        self,
        conn: sqlite3.Connection,
        import_id: str,
        *,
        include_qualifiers: bool = True,
    ) -> None:
        self._conn = conn
        self._import_id = import_id
        self.include_qualifiers = include_qualifiers

    def card_signatures(self) -> dict[int, frozenset[str]]:
        by_card: dict[int, set[str]] = defaultdict(set)
        for row in self._conn.execute(
            "SELECT card_id, predicate, params_json FROM card_predicates "
            "WHERE import_id = ? ORDER BY id",
            (self._import_id,),
        ):
            card_id = int(row["card_id"])
            predicate = str(row["predicate"])
            tokens = by_card[card_id]
            tokens.add(predicate)
            if self.include_qualifiers and predicate in _QUALIFIED_PREDICATES:
                try:
                    params = json.loads(row["params_json"] or "{}")
                except (ValueError, TypeError):
                    params = {}
                tokens.update(_qualifier_tokens(predicate, params))
        return {card_id: frozenset(tokens) for card_id, tokens in by_card.items()}


# ---------------------------------------------------------------------------
# Motif construction
# ---------------------------------------------------------------------------


def build_vocabulary(
    card_signatures: Mapping[int, frozenset[str]], size: int
) -> tuple[str, ...]:
    """Top-``size`` tokens by number of cards carrying them (stable order)."""
    counts: Counter[str] = Counter()
    for tokens in card_signatures.values():
        counts.update(tokens)
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return tuple(token for token, _ in ranked[: max(0, int(size))])


def _card_motif_strings(tokens: Sequence[str], max_size: int) -> frozenset[str]:
    motifs: set[str] = set()
    for size in range(1, max(1, int(max_size)) + 1):
        for combo in combinations(tokens, size):
            motifs.add("|".join(combo))
    return frozenset(motifs)


def _pair_motif_string(tokens_a: Sequence[str], tokens_b: Sequence[str]) -> set[str]:
    """Cross-card motifs: one token on each card, canonicalised order."""
    motifs: set[str] = set()
    for a in tokens_a:
        for b in tokens_b:
            motifs.add(f"{a}~{b}" if a <= b else f"{b}~{a}")
    return motifs


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MotifStat:
    """Enrichment of one motif in known combos vs the background."""

    motif: str
    kind: str
    tokens: tuple[str, ...]
    n_known: int
    n_known_total: int
    n_background: int
    n_background_total: int
    lift: float
    chi_square: float
    p_value: float
    low_expected: bool = False

    @property
    def p_known(self) -> float:
        return self.n_known / self.n_known_total if self.n_known_total else 0.0

    @property
    def p_background(self) -> float:
        return self.n_background / self.n_background_total if self.n_background_total else 0.0

    def as_dict(self) -> dict[str, Any]:
        lift = self.lift
        if not math.isfinite(lift):
            lift_out: Any = "inf" if lift > 0 else "-inf"
        else:
            lift_out = round(lift, 4)
        return {
            "motif": self.motif,
            "kind": self.kind,
            "tokens": list(self.tokens),
            "n_known": self.n_known,
            "n_known_total": self.n_known_total,
            "n_background": self.n_background,
            "n_background_total": self.n_background_total,
            "lift": lift_out,
            "significance": round(self.chi_square, 6),
            "p_value": round(self.p_value, 6),
            "low_expected": self.low_expected,
        }


def chi_square_2x2(a: int, b: int, c: int, d: int) -> tuple[float, float, bool]:
    """Chi-square (1 df) for ``[[a, b], [c, d]]``; p is ``erfc(sqrt(chi2/2))``.

    Returns ``(chi2, p, low_expected)`` where ``low_expected`` flags any
    expected cell below 5 (the chi-square approximation degrades there).
    """
    n = a + b + c + d
    if n == 0 or (a + b) == 0 or (c + d) == 0 or (a + c) == 0 or (b + d) == 0:
        return 0.0, 1.0, True
    denominator = (a + b) * (c + d) * (a + c) * (b + d)
    chi2 = n * (a * d - b * c) ** 2 / denominator
    p_value = math.erfc(math.sqrt(max(0.0, chi2) / 2.0))
    expected = (
        (a + b) * (a + c) / n,
        (a + b) * (b + d) / n,
        (c + d) * (a + c) / n,
        (c + d) * (b + d) / n,
    )
    return chi2, p_value, min(expected) < 5.0


def _motif_kind_tokens(motif: str) -> tuple[str, tuple[str, ...]]:
    if "~" in motif:
        return _KIND_CROSS, tuple(sorted(motif.split("~")))
    tokens = tuple(motif.split("|"))
    return (_KIND_SINGLE if len(tokens) == 1 else _KIND_PAIR), tokens


#: Default probe motifs: the predicate unions behind the current evaluation
#: false-positive clusters and the infinite-loop family.  A probe is counted as
#: ``tokens ⊆ signatures(card A) ∪ signatures(card B)`` (any split across the
#: pair); it is skipped when the same token set is already a general motif.
DEFAULT_PROBES: tuple[tuple[str, ...], ...] = (
    ("RECURS_FROM_GRAVEYARD", "SACRIFICE_OUTLET"),
    ("CASTS_FROM_GRAVEYARD", "PRODUCES_MANA"),
    ("MILLS", "SETS_COLOR"),
    ("PRODUCES_MANA", "STORM"),
    ("COPIES_CREATURE", "ETB_TRIGGER", "TAPS_COST", "UNTAPS"),
)


def _stat_for(
    motif: str,
    n_known: int,
    n_background: int,
    n_known_total: int,
    n_background_total: int,
    min_support: int,
    *,
    kind: str | None = None,
) -> MotifStat | None:
    if n_known < min_support:
        return None
    p_known = n_known / n_known_total if n_known_total else 0.0
    p_background = n_background / n_background_total if n_background_total else 0.0
    if p_background == 0.0:
        lift = float("inf") if p_known > 0 else 0.0
    else:
        lift = p_known / p_background
    chi2, p_value, low_expected = chi_square_2x2(
        n_known, n_known_total - n_known,
        n_background, n_background_total - n_background,
    )
    derived_kind, tokens = _motif_kind_tokens(motif)
    return MotifStat(
        motif=motif, kind=kind or derived_kind, tokens=tokens,
        n_known=n_known, n_known_total=n_known_total,
        n_background=n_background, n_background_total=n_background_total,
        lift=lift, chi_square=chi2, p_value=p_value, low_expected=low_expected,
    )


def _build_stats(
    card_known: Counter[str],
    card_background: Counter[str],
    cross_known: Counter[str],
    cross_background: Counter[str],
    probe_known: Counter[str],
    probe_background: Counter[str],
    n_known_total: int,
    n_background_total: int,
    min_support: int,
) -> list[MotifStat]:
    stats: list[MotifStat] = []
    general = set(card_known) | set(card_background) | set(cross_known) | set(cross_background)
    for motif in general:
        if "~" in motif:
            n_known = cross_known.get(motif, 0)
            n_background = cross_background.get(motif, 0)
        else:
            n_known = card_known.get(motif, 0)
            n_background = card_background.get(motif, 0)
        stat = _stat_for(motif, n_known, n_background, n_known_total,
                         n_background_total, min_support)
        if stat is not None:
            stats.append(stat)
    for motif in set(probe_known) | set(probe_background):
        if motif in general:
            continue
        stat = _stat_for(
            motif, probe_known.get(motif, 0), probe_background.get(motif, 0),
            n_known_total, n_background_total, min_support, kind=_KIND_CROSS,
        )
        if stat is not None:
            stats.append(stat)
    stats.sort(key=lambda s: (-(s.lift if math.isfinite(s.lift) else float("inf")),
                              -s.n_known, s.motif))
    return stats


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass
class EnrichmentReport:
    corpus_import_id: str
    known_import_id: str | None
    vocab: tuple[str, ...]
    n_known_total: int
    n_background_total: int
    seed: int
    min_support: int
    max_motif_size: int
    provider_name: str
    stats: list[MotifStat] = field(default_factory=list)
    pattern_view: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    duration_s: float = 0.0
    # Convenience index for lookups (not persisted).
    stats_by_motif: dict[str, MotifStat] = field(default_factory=dict, repr=False)

    def top_enriched(self, limit: int = 20) -> list[MotifStat]:
        return [s for s in self.stats if s.lift > 1.0][:limit]

    def top_anti_enriched(self, limit: int = 20) -> list[MotifStat]:
        anti = [s for s in self.stats if s.lift < 1.0]
        return sorted(anti, key=lambda s: (s.lift, -s.n_known, s.motif))[:limit]

    def lookup(self, motifs: Sequence[str]) -> list[MotifStat]:
        return [self.stats_by_motif[m] for m in motifs if m in self.stats_by_motif]

    def as_dict(self, *, top: int = 20) -> dict[str, Any]:
        return {
            "corpus_import_id": self.corpus_import_id,
            "known_import_id": self.known_import_id,
            "provider": self.provider_name,
            "vocab": list(self.vocab),
            "n_known_total": self.n_known_total,
            "n_background_total": self.n_background_total,
            "seed": self.seed,
            "min_support": self.min_support,
            "max_motif_size": self.max_motif_size,
            "duration_s": round(self.duration_s, 3),
            "top_enriched": [s.as_dict() for s in self.top_enriched(top)],
            "top_anti_enriched": [s.as_dict() for s in self.top_anti_enriched(top)],
            "pattern_view": self.pattern_view,
        }

    @staticmethod
    def _fmt(stat: MotifStat) -> str:
        lift = stat.lift
        lift_text = "inf" if math.isinf(lift) else f"{lift:.3f}"
        return (
            f"  lift={lift_text:>8}  {stat.n_known:>5}/{stat.n_known_total:<5} known  "
            f"vs {stat.n_background:>7}/{stat.n_background_total:<7} bg  "
            f"p={stat.p_value:.3g}  [{stat.kind}] {stat.motif}"
        )

    def format(self, top: int = 20) -> str:
        lines = [
            f"corpus import : {self.corpus_import_id}",
            f"known import  : {self.known_import_id}",
            f"provider      : {self.provider_name}",
            f"vocabulary    : {len(self.vocab)} tokens",
            f"known pairs   : {self.n_known_total}",
            f"background    : {self.n_background_total} (seed={self.seed})",
            f"min support   : {self.min_support}",
            f"duration      : {self.duration_s:.2f}s",
            "",
            f"top {top} enriched motifs (lift > 1):",
        ]
        lines.extend(self._fmt(s) for s in self.top_enriched(top))
        lines.append("")
        lines.append(f"top {top} anti-enriched motifs (lift < 1):")
        lines.extend(self._fmt(s) for s in self.top_anti_enriched(top))
        if self.pattern_view:
            lines.append("")
            lines.append("per-pattern top motifs (proposal support -> enrichment):")
            for pattern, motifs in sorted(self.pattern_view.items()):
                lines.append(f"  {pattern}:")
                for item in motifs:
                    lift = item.get("lift")
                    lift_text = "inf" if lift == "inf" else (
                        f"{lift:.3f}" if isinstance(lift, (int, float)) else "n/a")
                    lines.append(
                        f"    {item['motif']:<42} n={item['count']:<6} "
                        f"known={item.get('n_known')} bg={item.get('n_background')} "
                        f"lift={lift_text}"
                    )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Loading helpers
# ---------------------------------------------------------------------------


def _latest_corpus_import(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT r.import_id AS import_id FROM import_runs r "
        "WHERE EXISTS (SELECT 1 FROM cards c WHERE c.import_id = r.import_id) "
        "ORDER BY r.rowid DESC LIMIT 1"
    ).fetchone()
    return row["import_id"] if row is not None else None


def _latest_known_import(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT import_id FROM import_runs WHERE scryfall_source = 'spellbook' "
        "ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    return row["import_id"] if row is not None else None


def _latest_ontology_import(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT import_id FROM interactions ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row["import_id"] if row is not None else None


def _load_full_known_name_pairs(
    conn: sqlite3.Connection, known_import_id: str | None
) -> list[tuple[str, str]]:
    """The two card names of every exact 2-card known variant (Tier A).

    Reads ``known_combo_pairs`` where ``is_full_variant = 1`` and joins the
    combo's ``use`` cards to recover the concrete names behind the pair hash.
    """
    sql = (
        "SELECT kcp.combo_id AS combo_id, kcc.normalized_name AS name "
        "FROM known_combo_pairs kcp "
        "JOIN known_combo_cards kcc ON kcc.combo_id = kcp.combo_id "
        "WHERE kcp.is_full_variant = 1 AND kcc.role = 'use'"
    )
    params: tuple[Any, ...] = ()
    if known_import_id:
        sql += " AND kcp.import_id = ?"
        params = (known_import_id,)
    uses: dict[int, list[str]] = defaultdict(list)
    for row in conn.execute(sql + " ORDER BY kcp.combo_id, kcc.id", params):
        uses[int(row["combo_id"])].append(str(row["name"] or ""))
    pairs: list[tuple[str, str]] = []
    for names in uses.values():
        unique = sorted({n for n in names if n})
        if len(unique) == 2:
            pairs.append((unique[0], unique[1]))
    return pairs


def _resolve_vintage(vintage: VintageLegality | None) -> VintageLegality:
    if vintage is not None:
        return vintage
    if DEFAULT_VINTAGE_FORMAT.is_file():
        return VintageLegality.from_forge_format(DEFAULT_VINTAGE_FORMAT)
    return VintageLegality.permissive()


# ---------------------------------------------------------------------------
# Counting
# ---------------------------------------------------------------------------


def _count_pairs(
    pairs: Sequence[tuple[int, int]],
    card_motifs: Mapping[int, frozenset[str]],
    card_tokens: Mapping[int, tuple[str, ...]],
) -> tuple[Counter[str], Counter[str]]:
    card_counts: Counter[str] = Counter()
    cross_counts: Counter[str] = Counter()
    for left, right in pairs:
        left_motifs = card_motifs.get(left)
        right_motifs = card_motifs.get(right)
        if left_motifs and right_motifs:
            card_counts.update(left_motifs | right_motifs)
        elif left_motifs:
            card_counts.update(left_motifs)
        elif right_motifs:
            card_counts.update(right_motifs)
        left_tokens = card_tokens.get(left)
        right_tokens = card_tokens.get(right)
        if left_tokens and right_tokens:
            cross_counts.update(_pair_motif_string(left_tokens, right_tokens))
    return card_counts, cross_counts


def _count_probes(
    pairs: Sequence[tuple[int, int]],
    card_tokens: Mapping[int, Iterable[str]],
    probes: Sequence[Sequence[str]],
) -> Counter[str]:
    """Count pairs whose combined token union contains each probe motif."""
    prepared = [
        ("|".join(sorted(set(probe))), frozenset(probe))
        for probe in probes if probe
    ]
    counts: Counter[str] = Counter()
    for left, right in pairs:
        left_tokens = card_tokens.get(left)
        right_tokens = card_tokens.get(right)
        if not left_tokens and not right_tokens:
            continue
        union = set(left_tokens or ()) | set(right_tokens or ())
        for motif, tokens in prepared:
            if tokens <= union:
                counts[motif] += 1
    return counts


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


def analyze_motifs(
    store: ExperimentStore,
    *,
    corpus_import_id: str | None = None,
    known_import_id: str | None = None,
    provider: SignatureProvider | None = None,
    vocab_size: int = 40,
    max_motif_size: int = 3,
    background: int = 200_000,
    seed: int = 0,
    min_support: int = 3,
    vintage_only: bool = True,
    vintage: VintageLegality | None = None,
    background_pairs: Sequence[tuple[int, int]] | None = None,
    probe_motifs: Sequence[Sequence[str]] | None = None,
    pattern_top: int = 5,
) -> EnrichmentReport:
    """Rank predicate motifs by enrichment in known combos vs a random background.

    ``background_pairs`` injects a fixed background (tests); otherwise ``background``
    random Vintage card pairs are sampled with ``seed``.  ``lift`` is
    ``P(motif | known) / P(motif | background)``; ``chi_square``/``p_value`` are
    the 1-df test.  Motifs with fewer than ``min_support`` known hits are
    dropped (report counts alongside ratios).
    """
    started = time.monotonic()
    conn = store._conn
    with store_module._DB_LOCK:
        corpus_import_id = corpus_import_id or _latest_corpus_import(conn)
        if corpus_import_id is None:
            raise ValueError("no corpus import found (run combo-import-cards first)")
        known_import_id = known_import_id or _latest_known_import(conn)
        provider = provider or PredicateSignatureProvider(conn, corpus_import_id)
        card_signatures = dict(provider.card_signatures())
        card_rows = [
            (int(row["id"]), str(row["normalized_name"] or ""))
            for row in conn.execute(
                "SELECT id, normalized_name FROM cards WHERE import_id = ? ORDER BY id",
                (corpus_import_id,),
            )
        ]
        known_name_pairs = _load_full_known_name_pairs(conn, known_import_id)

    vintage = _resolve_vintage(vintage)

    # Vocabulary + per-card tokens/motifs.
    vocab = build_vocabulary(card_signatures, vocab_size)
    vocab_set = set(vocab)
    card_tokens: dict[int, tuple[str, ...]] = {}
    card_motifs: dict[int, frozenset[str]] = {}
    for card_id, signatures in card_signatures.items():
        tokens = tuple(sorted(signatures & vocab_set))
        if not tokens:
            continue
        card_tokens[card_id] = tokens
        card_motifs[card_id] = _card_motif_strings(tokens, max_motif_size)

    # Card pool and known pairs (Vintage-legal corpus cards only).
    name_to_id: dict[str, int] = {}
    pool: list[int] = []
    for card_id, name in card_rows:
        name_to_id.setdefault(name, card_id)
        if not vintage_only or vintage.is_legal(name):
            pool.append(card_id)

    known_pairs: list[tuple[int, int]] = []
    seen: set[str] = set()
    for name_a, name_b in known_name_pairs:
        left = name_to_id.get(name_a)
        right = name_to_id.get(name_b)
        if left is None or right is None or left == right:
            continue
        if vintage_only and not (vintage.is_legal(name_a) and vintage.is_legal(name_b)):
            continue
        key = pair_hash(name_a, name_b)
        if key in seen:
            continue
        seen.add(key)
        known_pairs.append((left, right))

    # Background.
    if background_pairs is not None:
        background_pairs = [(int(a), int(b)) for a, b in background_pairs if a != b]
    else:
        rng = random.Random(seed)
        count = int(background)
        if len(pool) >= 2 and count > 0:
            n = len(pool)
            sampled: list[tuple[int, int]] = []
            for _ in range(count):
                i = rng.randrange(n)
                j = rng.randrange(n)
                while j == i:
                    j = rng.randrange(n)
                sampled.append((pool[i], pool[j]))
            background_pairs = sampled
        else:
            background_pairs = []

    card_known, cross_known = _count_pairs(known_pairs, card_motifs, card_tokens)
    card_background, cross_background = _count_pairs(background_pairs, card_motifs, card_tokens)
    probes = DEFAULT_PROBES if probe_motifs is None else probe_motifs
    # Probes use the full signature set (not the vocab-restricted tokens) so a
    # named motif is always measurable even when a rare token is outside vocab.
    probe_known = _count_probes(known_pairs, card_signatures, probes)
    probe_background = _count_probes(background_pairs, card_signatures, probes)

    stats = _build_stats(
        card_known, card_background, cross_known, cross_background,
        probe_known, probe_background,
        len(known_pairs), len(background_pairs), min_support,
    )
    stats_by_motif = {s.motif: s for s in stats}

    pattern_view = pattern_motif_view(
        store,
        card_tokens=card_tokens,
        stats_by_motif=stats_by_motif,
        top=pattern_top,
    )

    return EnrichmentReport(
        corpus_import_id=corpus_import_id,
        known_import_id=known_import_id,
        vocab=vocab,
        n_known_total=len(known_pairs),
        n_background_total=len(background_pairs),
        seed=seed,
        min_support=min_support,
        max_motif_size=max_motif_size,
        provider_name=getattr(provider, "name", type(provider).__name__),
        stats=stats,
        pattern_view=pattern_view,
        duration_s=time.monotonic() - started,
        stats_by_motif=stats_by_motif,
    )


def pattern_motif_view(
    store: ExperimentStore,
    *,
    card_tokens: Mapping[int, tuple[str, ...]],
    stats_by_motif: Mapping[str, MotifStat],
    ontology_import_id: str | None = None,
    top: int = 5,
) -> dict[str, list[dict[str, Any]]]:
    """Per-pattern top cross-card motifs, annotated with their enrichment.

    Joins ``patterns``/``combo_hypotheses`` so each current pattern's motifs can
    be read against the known-combo statistics.
    """
    conn = store._conn
    with store_module._DB_LOCK:
        ontology_import_id = ontology_import_id or _latest_ontology_import(conn)
        sql = (
            "SELECT p.name AS pattern, h.card_ids_json AS card_ids_json "
            "FROM combo_hypotheses h JOIN patterns p ON p.id = h.pattern_id"
        )
        params: tuple[Any, ...] = ()
        if ontology_import_id:
            sql += " WHERE h.import_id = ?"
            params = (ontology_import_id,)
        rows = list(conn.execute(sql, params))

    per_pattern: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        try:
            ids = json.loads(row["card_ids_json"])
        except (ValueError, TypeError):
            continue
        if not isinstance(ids, list) or len(ids) != 2:
            continue
        left_tokens = card_tokens.get(int(ids[0]))
        right_tokens = card_tokens.get(int(ids[1]))
        if not left_tokens or not right_tokens:
            continue
        per_pattern[str(row["pattern"])].update(_pair_motif_string(left_tokens, right_tokens))

    view: dict[str, list[dict[str, Any]]] = {}
    for pattern, counter in per_pattern.items():
        def lift_key(motif: str) -> float:
            stat = stats_by_motif.get(motif)
            if stat is None:
                return 0.0
            return stat.lift if math.isfinite(stat.lift) else 1e18

        ranked = sorted(
            counter.items(), key=lambda kv: (-kv[1], -lift_key(kv[0]), kv[0])
        )[: max(0, int(top))]
        items: list[dict[str, Any]] = []
        for motif, count in ranked:
            stat = stats_by_motif.get(motif)
            items.append({
                "motif": motif,
                "count": count,
                "n_known": stat.n_known if stat else None,
                "n_background": stat.n_background if stat else None,
                "lift": (stat.lift if stat else None),
            })
        view[pattern] = items
    return view


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def persist_enrichment(
    store: ExperimentStore,
    report: EnrichmentReport,
    *,
    params: dict[str, Any] | None = None,
    notes: str | None = None,
) -> int:
    """Append a motif run + one row per computed motif; return the run id."""
    conn = store._conn
    with store_module._DB_LOCK:
        cur = conn.execute(
            "INSERT INTO motif_runs (started_at, corpus_import_id, known_import_id, "
            "tokens_json, params_json, notes) VALUES (?, ?, ?, ?, ?, ?)",
            (
                _utc_now(), report.corpus_import_id, report.known_import_id,
                json.dumps(list(report.vocab)),
                json.dumps(params or {}, sort_keys=True), notes,
            ),
        )
        run_id = int(cur.lastrowid or 0)
        rows = [
            (
                run_id, stat.motif, stat.kind, stat.n_known, stat.n_known_total,
                stat.n_background, stat.n_background_total, stat.lift,
                stat.chi_square, stat.p_value, json.dumps(list(stat.tokens)),
            )
            for stat in report.stats
        ]
        if rows:
            conn.executemany(
                "INSERT INTO motif_enrichment (run_id, motif, kind, n_known, "
                "n_known_total, n_background, n_background_total, lift, significance, "
                "p_value, tokens_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        conn.commit()
    return run_id


# ---------------------------------------------------------------------------
# CLI (thin)
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="combo-analyze-motifs",
        description="Rank predicate motifs by enrichment in known combos vs random pairs.",
    )
    parser.add_argument("--db", default="./research.db")
    parser.add_argument("--top", type=int, default=20, help="motifs to show per side")
    parser.add_argument("--min-support", type=int, default=3,
                        help="minimum known-combo hits for a motif")
    parser.add_argument("--background", type=int, default=200_000,
                        help="number of random background pairs to sample")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--vintage-only", dest="vintage_only", action="store_true",
                        default=True, help="restrict to Vintage-legal cards (default)")
    parser.add_argument("--all", dest="vintage_only", action="store_false",
                        help="include all corpus cards regardless of Vintage legality")
    parser.add_argument("--vocab-size", type=int, default=40)
    parser.add_argument("--max-motif-size", type=int, default=3)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-persist", action="store_true")
    args = parser.parse_args(argv)

    store = ExperimentStore(Path(args.db))
    try:
        report = analyze_motifs(
            store,
            vocab_size=args.vocab_size,
            max_motif_size=args.max_motif_size,
            background=args.background,
            seed=args.seed,
            min_support=args.min_support,
            vintage_only=args.vintage_only,
        )
        if not args.no_persist:
            persist_enrichment(store, report, params={
                "vocab_size": args.vocab_size,
                "max_motif_size": args.max_motif_size,
                "background": args.background,
                "seed": args.seed,
                "min_support": args.min_support,
                "vintage_only": args.vintage_only,
            })
    finally:
        store.close()

    if args.json:
        print(json.dumps(report.as_dict(top=args.top), indent=2, sort_keys=True))
    else:
        print(report.format(top=args.top))
    return 0


__all__ = [
    "DEFAULT_PROBES",
    "EnrichmentReport",
    "MotifStat",
    "PredicateSignatureProvider",
    "SignatureProvider",
    "analyze_motifs",
    "build_vocabulary",
    "chi_square_2x2",
    "main",
    "pattern_motif_view",
    "persist_enrichment",
]
