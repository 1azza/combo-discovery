"""``color_lock_mill``: sets all cards to a colour plus a repeat-mill engine."""

from __future__ import annotations

from collections.abc import Iterator

from .. import vocabulary as vocab
from .base import CardView, Edge, PatternDef, _evidence, _make_edge, _plain_marker


def color_lock_mill(views: dict[int, CardView]) -> Iterator[Edge]:
    locks = [v for v in views.values() if v.has(vocab.SETS_COLOR)]
    mills = [v for v in views.values() if v.has(vocab.MILLS)]
    for lock in locks:
        for mill in mills:
            if lock.card_id == mill.card_id:
                continue
            mechanism = (
                f"{lock.name} makes every card a single colour; {mill.name} "
                f"mills and repeats when two milled cards share a colour, so the "
                f"colour lock makes the mill self-sustaining."
            )
            yield _make_edge(
                "color_lock_mill", lock, mill, mechanism,
                [_evidence(lock, vocab.SETS_COLOR), _evidence(mill, vocab.MILLS),
                 _plain_marker(True, "colour-lock mill")],
                direction="mutual",
            )


PATTERN = PatternDef(
    name="color_lock_mill",
    description="Sets all cards to a colour plus a repeat-mill engine.",
    rule={"lock": ["SETS_COLOR"], "mill": ["MILLS"], "direction": "mutual"},
    matcher=color_lock_mill,
)

__all__ = ["PATTERN", "color_lock_mill"]
