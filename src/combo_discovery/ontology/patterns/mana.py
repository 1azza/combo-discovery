"""``mana_engine``: ETB/tap mana producer plus an X-cost or artifact spender."""

from __future__ import annotations

from collections.abc import Iterator

from .. import vocabulary as vocab
from .base import (
    CardView,
    Edge,
    PatternDef,
    _evidence,
    _make_edge,
    _mana_linked,
    _plain_marker,
)


def _strong_producer(view: CardView) -> bool:
    """A mana source worth building around (ritual, rock, or Treasure)."""
    predicate = view.first(vocab.PRODUCES_MANA)
    if predicate is None:
        return False
    params = predicate.params
    if params.get("x") or params.get("source") == "treasure":
        return True
    amount = str(params.get("amount") or "")
    return amount.isdigit() and int(amount) >= 2


def _is_spender(view: CardView) -> bool:
    return view.context.has_x_cost


def mana_engine(views: dict[int, CardView]) -> Iterator[Edge]:
    # Ability-scope: the mana production must belong to the ETB trigger or the
    # tap-cost ability, not merely sit on the same card.
    producers = [v for v in views.values() if _strong_producer(v) and _mana_linked(v)]
    x_spenders = [v for v in views.values() if _is_spender(v)]
    # An X-scaling producer also pairs with artifact mana (Tolarian Academy).
    artifact_mana = [v for v in views.values()
                     if v.context.is_artifact and v.has(vocab.PRODUCES_MANA)]
    for producer in producers:
        produced = producer.first(vocab.PRODUCES_MANA)
        targets = x_spenders
        if produced is not None and produced.params.get("x"):
            targets = x_spenders + artifact_mana
        for spender in targets:
            if spender.card_id == producer.card_id:
                continue
            amount = (produced.params.get("amount") if produced else None) or ""
            activation = str((produced.params.get("activation") if produced else None) or "")
            mechanism = (
                f"{producer.name} produces mana"
                + (f" ({amount})" if amount else "")
                + f"; {spender.name} converts that mana into value "
                f"(X-cost / artifact mana scaling)."
            )
            yield _make_edge(
                "mana_engine", producer, spender, mechanism,
                [_evidence(producer, vocab.PRODUCES_MANA),
                 _plain_marker(
                     not activation,
                     f"mana activation={activation}" if activation else "unconditional mana",
                 )],
                score_bonus=0.0 if not activation else -0.05,
            )


PATTERN = PatternDef(
    name="mana_engine",
    description="ETB/tap mana producer plus an X-cost or artifact-mana spender.",
    rule={"producer": ["PRODUCES_MANA", "& (ETB_TRIGGER | TAPS_COST)"],
          "spender": ["mana_cost contains X | artifact with PRODUCES_MANA"],
          "direction": "one_way", "confidence": "low"},
    matcher=mana_engine,
)

__all__ = ["PATTERN", "mana_engine"]
