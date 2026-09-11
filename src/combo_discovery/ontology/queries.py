"""Named cycle queries (the discoverable combo patterns).

A query is a predicate over a :class:`ComboContext` (the abilities and links of
a candidate cycle) plus a base score and a machine-readable rule.  This mirrors
the existing pattern registry -- named, versioned, listable -- but the named
patterns are now *queries over cycles* rather than bespoke pair matchers.

The predicates read only the algebra (ports/triggers/links); they never inspect
card names, so recall comes from the derived structure.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from .links import Link
from .ports import AbilitySig


@dataclass(frozen=True)
class ComboContext:
    """The derived facts a query may inspect."""

    abilities: tuple[AbilitySig, ...]
    links: tuple[Link, ...]

    def produced_kinds(self) -> frozenset[str]:
        return frozenset(p.kind for ability in self.abilities for p in ability.produces)

    def consumed_kinds(self) -> frozenset[str]:
        return frozenset(p.kind for ability in self.abilities for p in ability.consumes)

    def any_produces(self, kind: str) -> bool:
        return any(p.kind == kind for ability in self.abilities for p in ability.produces)

    def any_consumes(self, kind: str) -> bool:
        return any(p.kind == kind for ability in self.abilities for p in ability.consumes)

    def any_trigger_producing(self, trigger_kind: str, produce_kind: str) -> bool:
        for ability in self.abilities:
            trig = ability.triggers_on
            if trig is not None and trig.kind == trigger_kind and \
                    any(p.kind == produce_kind for p in ability.produces):
                return True
        return False

    def triggered_kinds(self) -> frozenset[str]:
        return frozenset(
            ability.triggers_on.kind for ability in self.abilities
            if ability.triggers_on is not None
        )


Predicate = Callable[[ComboContext], bool]


@dataclass(frozen=True)
class Query:
    """A named cycle predicate, listable like a pattern definition."""

    name: str
    description: str
    base_score: float
    predicate: Predicate
    rule: dict[str, Any]
    version: int = 1

    def matches(self, context: ComboContext) -> bool:
        return bool(self.predicate(context))


# ---------------------------------------------------------------------------
# Query predicates
# ---------------------------------------------------------------------------


def _infinite_etb_loop(ctx: ComboContext) -> bool:
    has_copy = ctx.any_produces("copy_permanent")
    has_etb_untap = ctx.any_trigger_producing("enters_battlefield", "untap")
    return has_copy and has_etb_untap


def _combat_loop(ctx: ComboContext) -> bool:
    has_extra = ctx.any_produces("extra_phase")
    has_attack_untap = ctx.any_trigger_producing("attacks", "untap")
    return has_extra and has_attack_untap


def _sacrifice_loop(ctx: ComboContext) -> bool:
    if not ctx.any_consumes("sacrifice"):
        return False
    returns = ctx.any_produces("zone_move") or "dies" in ctx.triggered_kinds()
    return returns


def _mana_loop(ctx: ComboContext) -> bool:
    return ctx.any_produces("mana") and ctx.any_consumes("mana")


def _any_cycle(ctx: ComboContext) -> bool:
    return True


QUERIES: tuple[Query, ...] = (
    Query(
        name="infinite_etb_loop",
        description="Copy engine plus an enters-the-battlefield untapper that "
                    "closes the loop.",
        base_score=0.85,
        predicate=_infinite_etb_loop,
        rule={
            "produces": ["copy_permanent"],
            "trigger": ["enters_battlefield producing untap"],
            "re_entrant": True,
            "confidence": "high",
        },
    ),
    Query(
        name="combat_loop",
        description="Extra-combat engine plus an attack-triggered untapper.",
        base_score=0.72,
        predicate=_combat_loop,
        rule={
            "produces": ["extra_phase"],
            "trigger": ["attacks producing untap"],
            "re_entrant": True,
            "confidence": "medium",
        },
    ),
    Query(
        name="sacrifice_loop",
        description="Sacrifice outlet plus a dies/reanimation return.",
        base_score=0.68,
        predicate=_sacrifice_loop,
        rule={
            "consumes": ["sacrifice"],
            "return": ["zone_move from graveyard", "dies trigger"],
            "confidence": "medium",
        },
    ),
    Query(
        name="mana_loop",
        description="Mana production plus a mana sink inside the cycle.",
        base_score=0.50,
        predicate=_mana_loop,
        rule={"produces": ["mana"], "consumes": ["mana"], "confidence": "low"},
    ),
    Query(
        name="any_cycle",
        description="Structural fallback: any re-entrant, resource-closed cycle.",
        base_score=0.30,
        predicate=_any_cycle,
        rule={"produces": [], "consumes": [], "confidence": "low"},
    ),
)

_BY_NAME: dict[str, Query] = {query.name: query for query in QUERIES}


def iter_queries() -> Iterator[Query]:
    return iter(QUERIES)


def get_query(name: str) -> Query:
    return _BY_NAME[name]


def matching_queries(context: ComboContext) -> tuple[Query, ...]:
    return tuple(query for query in QUERIES if query.matches(context))


__all__ = [
    "ComboContext",
    "QUERIES",
    "Query",
    "get_query",
    "iter_queries",
    "matching_queries",
]
