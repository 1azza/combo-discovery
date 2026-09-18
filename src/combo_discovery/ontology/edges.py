"""Thin facade over the pattern registry.

The individual combo patterns live in :mod:`combo_discovery.ontology.patterns`
(one module each, registered in ``patterns/__init__.py``).  This module keeps
the long-standing public API working -- ``build_edges``, ``PATTERNS``, the
matcher functions, the compatibility helpers and the scoring/gate helpers -- and
owns the orchestration (per-source / per-pattern caps) that runs every
registered pattern.

Scoring weights (documented, additive, clamped to 1.0):

* ``base`` per pattern (see :data:`~.patterns.base.PATTERN_BASE`);
* ``+0.04`` per contributing predicate pair, capped at ``+0.16``;
* ``+0.05`` when the two cards share a colour identity;
* ``+0.03`` when the more expensive card costs <= 3 mana;
* ``+0.02`` when both cards cost <= 2 mana.

These are heuristics for *ranking*; recall is decided by the predicate rules,
not the weights.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from .patterns import (
    PATTERNS,
    color_lock_mill,
    draw_engine,
    free_cast_loops,
    get_pattern,
    infinite_etb_loop,
    iter_patterns,
    mana_engine,
    sacrifice_recursion,
    storm_engine,
)
from .patterns.base import (
    PATTERN_BASE,
    AbilityLink,
    CardView,
    Edge,
    PatternDef,
    check_ability_link,
    score_edge,
)
from .restrictions import (
    Alternative,
    Compatibility,
    CompatResult,
    Restriction,
    check_compatibility,
    engine_can_copy,
    parse_restriction,
    restriction_matches_card,
)

#: name -> matcher, retained for back-compat with the pre-registry layout.
_GENERATORS = {pattern.name: pattern.matcher for pattern in PATTERNS}


# ---------------------------------------------------------------------------
# Pair generation with caps
# ---------------------------------------------------------------------------


#: Per-pattern ``(max_per_source, max_per_pattern)`` bounds, applied only when
#: the caller opts in (``build_edges(..., apply_caps=True)`` / the CLI
#: ``--caps``).  The structural filters already bring the full 33k-card graph to
#: ~0.5M edges, so the default build keeps every pair; these bounds exist for
#: larger corpora or latency-sensitive serving.
DEFAULT_CAPS: dict[str, tuple[int | None, int | None]] = {
    "infinite_etb_loop": (80, 20_000),
    "sacrifice_recursion": (80, 20_000),
    "mana_engine": (60, 20_000),
    "free_cast_loops": (80, 20_000),
    "storm_engine": (80, 20_000),
    "color_lock_mill": (80, 20_000),
    "draw_engine": (80, 20_000),
}


def _cap_per_source_iter(iterable: Iterable[Edge], cap: int | None) -> Iterator[Edge]:
    """Bound each source card to its ``cap`` best partners.

    Generators emit contiguous runs per source (the outer loop is the source),
    so this keeps only a single run in memory and never materialises the full
    product.
    """
    if cap is None:
        yield from iterable
        return
    buffer: list[Edge] = []
    current: tuple[str, int] | None = None
    for edge in iterable:
        key = (edge.pattern, edge.source.card_id)
        if current is not None and key != current:
            buffer.sort(key=lambda e: (-e.score, e.target.name))
            yield from buffer[:cap]
            buffer = []
        current = key
        buffer.append(edge)
    if buffer:
        buffer.sort(key=lambda e: (-e.score, e.target.name))
        yield from buffer[:cap]


def build_edges(
    views: dict[int, CardView],
    *,
    apply_caps: bool = False,
    max_per_source: int | None = None,
    max_per_pattern: int | None = None,
) -> list[Edge]:
    """Run every registered pattern and deduplicate by unordered card pair.

    ``apply_caps`` enables the per-pattern :data:`DEFAULT_CAPS` bounds (off by
    default, so the small validation corpus and the full build see every pair).
    ``max_per_source`` / ``max_per_pattern`` override the per-pattern values
    when supplied.
    """
    all_edges: list[Edge] = []
    for pattern in iter_patterns():
        name = pattern.name
        source_cap, pattern_cap = (
            DEFAULT_CAPS.get(name, (40, 20_000)) if apply_caps else (None, None)
        )
        if max_per_source is not None:
            source_cap = max_per_source
        if max_per_pattern is not None:
            pattern_cap = max_per_pattern
        best: dict[tuple[str, frozenset[int]], Edge] = {}
        for edge in _cap_per_source_iter(pattern.matcher(views), source_cap):
            key = edge.pair_key
            current = best.get(key)
            if current is None or edge.score > current.score:
                best[key] = edge
        items = sorted(best.values(), key=lambda e: (-e.score, e.source.name, e.target.name))
        if pattern_cap is not None:
            items = items[:pattern_cap]
        all_edges.extend(items)
    return sorted(all_edges, key=lambda e: (-e.score, e.pattern, e.source.name))


__all__ = [
    "AbilityLink",
    "Alternative",
    "CardView",
    "CompatResult",
    "Compatibility",
    "Edge",
    "PATTERN_BASE",
    "PATTERNS",
    "PatternDef",
    "Restriction",
    "build_edges",
    "check_ability_link",
    "check_compatibility",
    "color_lock_mill",
    "draw_engine",
    "engine_can_copy",
    "free_cast_loops",
    "get_pattern",
    "infinite_etb_loop",
    "iter_patterns",
    "mana_engine",
    "parse_restriction",
    "restriction_matches_card",
    "sacrifice_recursion",
    "score_edge",
    "storm_engine",
]
