"""The pattern registry.

Each pattern lives in its own module and exposes a :class:`~.base.PatternDef`
(name, description, rule JSON, matcher, scorer).  This package collects them in
a fixed order, provides lookup/iteration for the builder and the evaluation
layer (which can report per-pattern counts via ``PatternDef.count``), and
re-exports the matcher functions so the historical import paths keep working.
"""

from __future__ import annotations

from collections.abc import Iterator

from .base import (
    PATTERN_BASE,
    AbilityLink,
    CardView,
    Edge,
    PatternDef,
    score_edge,
)
from .draw import PATTERN as _draw_pattern
from .draw import draw_engine
from .free_cast import PATTERN as _free_cast_pattern
from .free_cast import free_cast_loops
from .infinite_loop import PATTERN as _infinite_pattern
from .infinite_loop import infinite_etb_loop
from .mana import PATTERN as _mana_pattern
from .mana import mana_engine
from .mill import PATTERN as _mill_pattern
from .mill import color_lock_mill
from .recursion import PATTERN as _recursion_pattern
from .recursion import sacrifice_recursion
from .storm import PATTERN as _storm_pattern
from .storm import storm_engine

#: Registration order is part of the public surface (it determines the order of
#: the ``patterns`` rows on a fresh database); keep it stable.
PATTERNS: tuple[PatternDef, ...] = (
    _infinite_pattern,
    _recursion_pattern,
    _mana_pattern,
    _free_cast_pattern,
    _storm_pattern,
    _mill_pattern,
    _draw_pattern,
)

_BY_NAME: dict[str, PatternDef] = {pattern.name: pattern for pattern in PATTERNS}

#: name -> matcher, retained for the orchestration loop and back-compat.
_GENERATORS = {pattern.name: pattern.matcher for pattern in PATTERNS}


def get_pattern(name: str) -> PatternDef:
    """Return the registered pattern named ``name`` (KeyError if unknown)."""
    return _BY_NAME[name]


def iter_patterns() -> Iterator[PatternDef]:
    """Yield every registered pattern in registration order."""
    return iter(PATTERNS)


__all__ = [
    "AbilityLink",
    "CardView",
    "Edge",
    "PATTERNS",
    "PATTERN_BASE",
    "PatternDef",
    "color_lock_mill",
    "draw_engine",
    "free_cast_loops",
    "get_pattern",
    "infinite_etb_loop",
    "iter_patterns",
    "mana_engine",
    "sacrifice_recursion",
    "score_edge",
    "storm_engine",
]
