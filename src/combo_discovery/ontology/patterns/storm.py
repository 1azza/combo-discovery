"""``storm_engine``: storm payoff plus a cheap-spell/mana enabler."""

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
)


def _cheap_enabler(view: CardView) -> bool:
    """A mana source / untapper strong enough to chain spells in one turn."""
    produced = view.first(vocab.PRODUCES_MANA)
    if produced is not None:
        params = produced.params
        if params.get("x") or params.get("source") == "treasure":
            return True
        amount = str(params.get("amount") or "")
        if amount.isdigit() and int(amount) >= 2:
            return True
        if view.has(vocab.SACRIFICES_SELF):
            return True
    if view.has(vocab.ALTERNATIVE_COST):
        return True
    untap = view.first(vocab.UNTAPS)
    if untap is not None:
        amount = str(untap.params.get("amount") or "")
        if amount.isdigit() and int(amount) >= 3:
            return True
    return False


def storm_engine(views: dict[int, CardView]) -> Iterator[Edge]:
    payoffs = [v for v in views.values() if v.has(vocab.STORM)]
    enablers = [
        v for v in views.values()
        if _cheap_enabler(v) or (v.has(vocab.STORM) and True)
    ]
    for payoff in payoffs:
        for enabler in enablers:
            if payoff.card_id == enabler.card_id:
                continue
            kind = "a cheap mana source" if enabler.has(vocab.PRODUCES_MANA) else (
                "cost reduction" if enabler.has(vocab.ALTERNATIVE_COST) else (
                    "an untap engine" if enabler.has(vocab.UNTAPS) else "a storm engine"))
            mechanism = (
                f"{payoff.name} carries Storm; {enabler.name} is {kind}, "
                f"letting you chain spells before the storm payoff resolves."
            )
            yield _make_edge(
                "storm_engine", payoff, enabler, mechanism,
                [_evidence(payoff, vocab.STORM), _evidence(enabler, vocab.PRODUCES_MANA),
                 _plain_marker(True, "storm payoff enabler")],
            )


PATTERN = PatternDef(
    name="storm_engine",
    description="Storm payoff plus a cheap-spell/mana enabler.",
    rule={"payoff": ["STORM"],
          "enabler": ["PRODUCES_MANA | ALTERNATIVE_COST | UNTAPS | STORM(excl. self)"],
          "direction": "one_way"},
    matcher=storm_engine,
)

__all__ = ["PATTERN", "storm_engine"]
