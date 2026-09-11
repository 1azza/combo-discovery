"""``free_cast_loops``: cast-from-graveyard engine plus a free mana enabler."""

from __future__ import annotations

from collections.abc import Iterator

from .. import vocabulary as vocab
from .base import (
    CardView,
    Edge,
    PatternDef,
    _evidence,
    _make_edge,
    _plain_marker,
    _same_ability,
)


def free_cast_loops(views: dict[int, CardView]) -> Iterator[Edge]:
    freecasters = [v for v in views.values() if v.has(vocab.CASTS_FROM_GRAVEYARD)]
    # Ability-scope: a self-sacrificing mana source must produce the mana and
    # sacrifice itself in the same ability (LED), not two unrelated abilities.
    enablers = [
        v for v in views.values()
        if (v.has(vocab.PRODUCES_MANA) and v.has(vocab.SACRIFICES_SELF)
            and _same_ability(v.first(vocab.PRODUCES_MANA), v.first(vocab.SACRIFICES_SELF)))
        or v.has(vocab.ALTERNATIVE_COST)
    ]
    for caster in freecasters:
        for enabler in enablers:
            if caster.card_id == enabler.card_id:
                continue
            mechanism = (
                f"{caster.name} lets you cast cards from your graveyard; "
                f"{enabler.name} provides free mana or an alternative cost, "
                f"enabling repeated casts."
            )
            evidence = [_evidence(caster, vocab.CASTS_FROM_GRAVEYARD),
                        _evidence(enabler, vocab.PRODUCES_MANA),
                        _plain_marker(True, "cast-from-graveyard enabler")]
            yield _make_edge("free_cast_loops", caster, enabler, mechanism, evidence)


PATTERN = PatternDef(
    name="free_cast_loops",
    description="Cast-from-graveyard engine plus a free mana/cost enabler.",
    rule={"freecaster": ["CASTS_FROM_GRAVEYARD"],
          "enabler": ["(PRODUCES_MANA & SACRIFICES_SELF) | ALTERNATIVE_COST"],
          "direction": "one_way"},
    matcher=free_cast_loops,
)

__all__ = ["PATTERN", "free_cast_loops"]
