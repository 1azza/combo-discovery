"""Known-combo coverage-gap classification (diagnostic, read-only).

Answers *why* a known 2-card combo is absent from the generated candidate space.
The unit is a **card pair**: either an exact 2-card combo's pair, or any pair of
cards that co-occur in a known combo.  Each pair falls into one bucket:

* ``net_present``  -- the pair is a generated hypothesis;
* ``no_graph_edge`` -- no ``interactions`` row exists at all (a pure coverage
  gap: the generator never created an edge between the two cards);
* ``edge_exists_filtered`` -- an ``interactions`` row exists but the pair is not
  a 2-card hypothesis (it only ever appeared inside a >=3-card cycle).

The module is deliberately pure where it matters (mapping, bucketing, capability
tagging) so the classification logic is unit-testable without a database.  The
capability tags are **heuristic oracle-text profiles**, not proof: they say what
a pair *looks like*, which is the actionable signal for what the generator's
vocabulary fails to model.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..corpus.names import normalize_card_name

# ---------------------------------------------------------------------------
# Buckets
# ---------------------------------------------------------------------------

NET_PRESENT = "net_present"
NO_GRAPH_EDGE = "no_graph_edge"
EDGE_EXISTS_FILTERED = "edge_exists_filtered"

BUCKETS = (NET_PRESENT, NO_GRAPH_EDGE, EDGE_EXISTS_FILTERED)

# ---------------------------------------------------------------------------
# Capability tags (heuristic oracle-text profiles)
# ---------------------------------------------------------------------------

#: Priority order: the first matching tag is the pair's *primary* capability.
CAPABILITY_ORDER = (
    "aura_equipment",
    "one_shot",
    "copy_clone",
    "triggered_engine",
    "activated_copy_engine",
    "draw_damage_loop",
    "sacrifice_recursion",
    "extra_combat",
    "mana_engine",
    "counters_synergy",
    "etb_blink",
    "land_animation",
    "untapper",
    "token_maker",
    "other_synergy",
)

_CREATE_TOKEN = re.compile(r"create[sd]? .{0,40}token")
_ACTIVATED_COPY = re.compile(r"\{t\}.*(create a token|copy)")
_CLONE = re.compile(r"enter[s]? as a copy|enter[s]? the battlefield as a copy")
_RECUR = re.compile(
    r"from (your |a |the )?graveyard|return .{0,30}graveyard|"
    r"cast .{0,30}from .{0,10}graveyard"
)
_COUNTERS = re.compile(r"\+1/\+1 counter|additional \+1/\+1 counter")
_BLINK = re.compile(
    r"return target (creature|permanent|artifact|enchantment).{0,40}owner's hand|"
    r"exile (target|another target) .{0,40}return (it|that card|those cards) .{0,20}battlefield"
)
_LAND_ANIM = re.compile(r"lands? (you control |all )?are .{0,30}creatures?")


def _types(meta: CardMeta) -> str:
    return (meta.type_line or "").lower()


def _text(meta: CardMeta) -> str:
    return (meta.oracle_text or "").lower()


def _is_copy(text: str) -> bool:
    return "create a token" in text and "copy" in text


def _is_activated_copy(text: str) -> bool:
    return bool(_ACTIVATED_COPY.search(text))


def _is_triggered_copy(text: str) -> bool:
    return _is_copy(text) and not _is_activated_copy(text)


def _has_draw(text: str) -> bool:
    return "draw a card" in text or "draws a card" in text or "draw cards" in text


def _has_damage(text: str) -> bool:
    return "deals" in text and "damage" in text


def _has_mana(text: str) -> bool:
    return "add {" in text or "adds {" in text


def _draw_damage_loop(a: str, b: str) -> bool:
    return (_has_draw(a) and _has_damage(b)) or (_has_damage(a) and _has_draw(b))


def capability_tags(a: CardMeta, b: CardMeta) -> tuple[str, ...]:
    """All heuristic capability tags for a pair, primary first."""
    ta, tb = _types(a), _types(b)
    xa, xb = _text(a), _text(b)
    found: list[str] = []

    def add(tag: str, cond: bool) -> None:
        if cond and tag not in found:
            found.append(tag)

    add("aura_equipment",
        "aura" in ta or "aura" in tb or "equipment" in ta or "equipment" in tb)
    add("one_shot",
        "instant" in ta or "instant" in tb or "sorcery" in ta or "sorcery" in tb)
    add("copy_clone", bool(_CLONE.search(xa) or _CLONE.search(xb)))
    add("triggered_engine", _is_triggered_copy(xa) or _is_triggered_copy(xb))
    add("activated_copy_engine", _is_activated_copy(xa) or _is_activated_copy(xb))
    add("draw_damage_loop", _draw_damage_loop(xa, xb))
    add("sacrifice_recursion",
        ("sacrifice" in xa or "sacrifice" in xb)
        and bool(_RECUR.search(xa) or _RECUR.search(xb)))
    add("extra_combat", "additional combat" in xa or "additional combat" in xb)
    add("mana_engine", _has_mana(xa) or _has_mana(xb))
    add("counters_synergy", bool(_COUNTERS.search(xa) or _COUNTERS.search(xb)))
    add("etb_blink", bool(_BLINK.search(xa) or _BLINK.search(xb)))
    add("land_animation", bool(_LAND_ANIM.search(xa) or _LAND_ANIM.search(xb)))
    add("untapper", "untap" in xa or "untap" in xb)
    add("token_maker",
        bool(_CREATE_TOKEN.search(xa)) or bool(_CREATE_TOKEN.search(xb)))
    return tuple(found) or ("other_synergy",)


def primary_capability(tags: Sequence[str]) -> str:
    """The highest-priority tag (``CAPABILITY_ORDER``, then stable order)."""
    order = {name: index for index, name in enumerate(CAPABILITY_ORDER)}
    return min(tags, key=lambda tag: order.get(tag, len(order)))


# ---------------------------------------------------------------------------
# Card metadata + mapping
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CardMeta:
    """The card facts the classifier needs (no effects, no scripts)."""

    card_id: int
    name: str
    normalized_name: str
    type_line: str
    oracle_text: str


def load_card_meta(conn: Any) -> dict[int, CardMeta]:
    """``cards.id`` -> :class:`CardMeta` for the corpus import."""
    metas: dict[int, CardMeta] = {}
    for row in conn.execute(
        "SELECT id, name, normalized_name, type_line, oracle_text FROM cards"
    ):
        metas[int(row["id"])] = CardMeta(
            card_id=int(row["id"]),
            name=row["name"] or "",
            normalized_name=row["normalized_name"] or "",
            type_line=row["type_line"] or "",
            oracle_text=row["oracle_text"] or "",
        )
    return metas


def oracle_index(conn: Any) -> dict[str, int]:
    """``oracle_id`` -> ``cards.id`` (the precise join, preferred)."""
    index: dict[str, int] = {}
    for row in conn.execute("SELECT card_id, oracle_id FROM card_oracle_ids"):
        index.setdefault(row["oracle_id"], int(row["card_id"]))
    return index


def norm_index(metas: Mapping[int, CardMeta]) -> dict[str, int]:
    """``normalized_name`` -> ``cards.id`` (first wins, deterministic by id)."""
    index: dict[str, int] = {}
    for card_id in sorted(metas):
        index.setdefault(metas[card_id].normalized_name, card_id)
    return index


def map_card(
    *,
    raw_name: str | None,
    normalized_name: str | None,
    oracle_id: str | None,
    by_oracle: Mapping[str, int],
    by_norm: Mapping[str, int],
) -> tuple[int | None, str]:
    """Resolve one Spellbook ``use`` row to a card id; returns ``(id, via)``.

    Priority: oracle id, then the stored ``normalized_name`` (already front-face
    for DFCs), then the front-face of ``raw_name`` under the repo normalizer.
    """
    if oracle_id and oracle_id in by_oracle:
        return by_oracle[oracle_id], "oracle"
    if normalized_name and normalized_name in by_norm:
        return by_norm[normalized_name], "name"
    if raw_name:
        front = raw_name.split(" // ")[0]
        mapped = by_norm.get(normalize_card_name(front))
        if mapped is not None:
            return mapped, "raw_front"
    return None, "unmatched"


@dataclass
class KnownCombo:
    """One known combo's mapped ``use`` cards (``None`` = unmappable)."""

    combo_id: int
    card_ids: tuple[int | None, ...]
    raw_names: tuple[str, ...]

    @property
    def mapped(self) -> tuple[int, ...]:
        return tuple(cid for cid in self.card_ids if cid is not None)

    @property
    def n_unmapped(self) -> int:
        return sum(1 for cid in self.card_ids if cid is None)


def load_known_combos(
    conn: Any,
    metas: Mapping[int, CardMeta],
    *,
    by_oracle: Mapping[str, int] | None = None,
) -> list[KnownCombo]:
    """Load known combos' ``use`` cards, mapped to corpus ids."""
    by_oracle = by_oracle if by_oracle is not None else oracle_index(conn)
    by_norm = norm_index(metas)
    grouped: dict[int, list[tuple[int | None, str]]] = defaultdict(list)
    for row in conn.execute(
        "SELECT combo_id, raw_name, normalized_name, oracle_id "
        "FROM known_combo_cards WHERE role = 'use'"
    ):
        card_id, _via = map_card(
            raw_name=row["raw_name"],
            normalized_name=row["normalized_name"],
            oracle_id=row["oracle_id"],
            by_oracle=by_oracle,
            by_norm=by_norm,
        )
        grouped[int(row["combo_id"])].append((card_id, row["raw_name"] or ""))
    return [
        KnownCombo(
            combo_id=combo_id,
            card_ids=tuple(cid for cid, _ in items),
            raw_names=tuple(name for _, name in items),
        )
        for combo_id, items in grouped.items()
    ]


# ---------------------------------------------------------------------------
# Pair sets
# ---------------------------------------------------------------------------


def combo_pairs(
    combos: Iterable[KnownCombo],
    *,
    sizes: Iterable[int] | None = None,
    require_all_mapped: bool = True,
) -> set[frozenset[int]]:
    """Distinct unordered mapped card pairs, optionally filtered by combo size."""
    allowed = set(sizes) if sizes is not None else None
    pairs: set[frozenset[int]] = set()
    for combo in combos:
        if allowed is not None and len(combo.card_ids) not in allowed:
            continue
        if require_all_mapped and combo.n_unmapped:
            continue
        ids = combo.mapped
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                if ids[i] != ids[j]:
                    pairs.add(frozenset((ids[i], ids[j])))
    return pairs


def ordered_combo_pairs(
    combos: Iterable[KnownCombo],
    *,
    sizes: Iterable[int] | None = None,
    require_all_mapped: bool = True,
) -> set[tuple[int, int]]:
    """Ordered variant of :func:`combo_pairs` (for reconciliation)."""
    allowed = set(sizes) if sizes is not None else None
    pairs: set[tuple[int, int]] = set()
    for combo in combos:
        if allowed is not None and len(combo.card_ids) not in allowed:
            continue
        if require_all_mapped and combo.n_unmapped:
            continue
        ids = combo.mapped
        for i in range(len(ids)):
            for j in range(len(ids)):
                if i != j and ids[i] != ids[j]:
                    pairs.add((ids[i], ids[j]))
    return pairs


def load_candidate_pairs(conn: Any) -> dict[str, set[frozenset[int]]]:
    """``{"pairs": ...}`` (any hypothesis) and ``{"pairs2": ...}`` (2-card only).

    ``interactions`` pairs are also returned; they are expected to equal the
    any-hypothesis set (both are written from the same combo enumeration).
    """
    pairs2: set[frozenset[int]] = set()
    pairs: set[frozenset[int]] = set()
    for row in conn.execute("SELECT card_ids_json FROM combo_hypotheses"):
        ids = json.loads(row["card_ids_json"])
        if len(ids) == 2 and ids[0] != ids[1]:
            pairs2.add(frozenset(ids))
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                if ids[i] != ids[j]:
                    pairs.add(frozenset((ids[i], ids[j])))
    edges: set[frozenset[int]] = set()
    for row in conn.execute(
        "SELECT source_card_id, target_card_id FROM interactions"
    ):
        edges.add(frozenset((int(row["source_card_id"]), int(row["target_card_id"]))))
    return {"pairs2": pairs2, "pairs": pairs, "edges": edges}


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def classify_pair(
    pair: frozenset[int],
    *,
    present_pairs: set[frozenset[int]],
    edge_pairs: set[frozenset[int]],
) -> str:
    """Bucket one known pair.

    ``present_pairs`` is the 2-card hypothesis set (the "proposed as a pairing"
    set).  A pair with an ``interactions`` row that is not in it is *filtered*:
    it only ever closed as part of a larger cycle.
    """
    if pair in present_pairs:
        return NET_PRESENT
    if pair in edge_pairs:
        return EDGE_EXISTS_FILTERED
    return NO_GRAPH_EDGE


@dataclass
class BucketReport:
    """Counts + capability breakdown for one denominator."""

    denominator: str
    n_pairs: int
    buckets: dict[str, int]
    capabilities: dict[str, int]
    capability_examples: dict[str, list[tuple[str, str]]]
    filtered_examples: list[tuple[str, str]]
    present_examples: list[tuple[str, str]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "denominator": self.denominator,
            "n_pairs": self.n_pairs,
            "buckets": self.buckets,
            "capabilities": self.capabilities,
            "capability_examples": {
                key: [list(example) for example in examples]
                for key, examples in self.capability_examples.items()
            },
            "filtered_examples": [list(e) for e in self.filtered_examples],
            "present_examples": [list(e) for e in self.present_examples],
        }


def _names(
    pair: frozenset[int], metas: Mapping[int, CardMeta]
) -> tuple[str, str]:
    ordered = sorted(pair)
    first = metas[ordered[0]].name if ordered[0] in metas else str(ordered[0])
    if len(ordered) < 2:
        return first, first
    second = metas[ordered[1]].name if ordered[1] in metas else str(ordered[1])
    return first, second


def analyze_denominator(
    name: str,
    pairs: Iterable[frozenset[int]],
    *,
    present_pairs: set[frozenset[int]],
    edge_pairs: set[frozenset[int]],
    metas: Mapping[int, CardMeta],
    examples_per_capability: int = 3,
) -> BucketReport:
    """Classify every pair and summarise buckets + missing capabilities."""
    pair_list = sorted(pairs, key=lambda p: tuple(sorted(p)))
    bucket_counts: Counter[str] = Counter()
    capability_counts: Counter[str] = Counter()
    capability_examples: dict[str, list[tuple[str, str]]] = defaultdict(list)
    filtered_examples: list[tuple[str, str]] = []
    present_examples: list[tuple[str, str]] = []

    for pair in pair_list:
        bucket = classify_pair(
            pair, present_pairs=present_pairs, edge_pairs=edge_pairs
        )
        bucket_counts[bucket] += 1
        names = _names(pair, metas)
        if bucket == NO_GRAPH_EDGE:
            ordered = sorted(pair)
            if all(cid in metas for cid in ordered):
                tags = capability_tags(metas[ordered[0]], metas[ordered[1]])
            else:
                tags = ("other_synergy",)
            primary = primary_capability(tags)
            capability_counts[primary] += 1
            if len(capability_examples[primary]) < examples_per_capability:
                capability_examples[primary].append(names)
        elif bucket == EDGE_EXISTS_FILTERED:
            if len(filtered_examples) < 10:
                filtered_examples.append(names)
        elif len(present_examples) < 10:
            present_examples.append(names)

    ordered_caps = sorted(
        capability_counts.items(), key=lambda kv: (-kv[1], kv[0])
    )
    return BucketReport(
        denominator=name,
        n_pairs=len(pair_list),
        buckets={bucket: bucket_counts.get(bucket, 0) for bucket in BUCKETS},
        capabilities=dict(ordered_caps),
        capability_examples={
            cap: capability_examples[cap] for cap, _ in ordered_caps
        },
        filtered_examples=filtered_examples,
        present_examples=present_examples,
    )


def precision_view(
    candidate_pairs: set[frozenset[int]],
    known_pairs: set[frozenset[int]],
) -> dict[str, Any]:
    """Inverse view: how much of the candidate space is a known pair."""
    hit = len(candidate_pairs & known_pairs)
    total = len(candidate_pairs)
    return {
        "candidate_pairs": total,
        "known_pairs": hit,
        "fraction": round(hit / total, 6) if total else 0.0,
    }


def loop_pool_cards(conn: Any, import_id: str) -> set[int]:
    """Card ids in the algebra's tight loop-machinery pool.

    Lazy imports keep this module cheap to import in tests; the call itself
    builds the full corpus signatures (seconds).
    """
    from .cycles import loop_machinery_cards
    from .extractor import load_contexts
    from .ports import build_signatures

    contexts, effects = load_contexts(conn, import_id)
    sigs = build_signatures(contexts, effects)
    return set(loop_machinery_cards(sigs))


def pool_gap_view(
    pairs: Iterable[frozenset[int]], pool: set[int]
) -> dict[str, int]:
    """For a set of pairs, how many have neither card in the tight pool."""
    neither = one = both = 0
    for pair in pairs:
        inside = sum(1 for cid in pair if cid in pool)
        if inside == 0:
            neither += 1
        elif inside == 1:
            one += 1
        else:
            both += 1
    return {"neither_in_pool": neither, "one_in_pool": one, "both_in_pool": both}


__all__ = [
    "BUCKETS",
    "CAPABILITY_ORDER",
    "EDGE_EXISTS_FILTERED",
    "NET_PRESENT",
    "NO_GRAPH_EDGE",
    "BucketReport",
    "CardMeta",
    "KnownCombo",
    "analyze_denominator",
    "capability_tags",
    "classify_pair",
    "combo_pairs",
    "load_candidate_pairs",
    "load_card_meta",
    "load_known_combos",
    "loop_pool_cards",
    "map_card",
    "norm_index",
    "oracle_index",
    "ordered_combo_pairs",
    "pool_gap_view",
    "precision_view",
    "primary_capability",
]
