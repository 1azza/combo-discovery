"""``draw_engine``: converts life into cards plus a draw payoff."""

from __future__ import annotations

from collections.abc import Iterator

from .. import vocabulary as vocab
from .base import CardView, Edge, PatternDef, _evidence, _make_edge, _plain_marker


def draw_engine(views: dict[int, CardView]) -> Iterator[Edge]:
    producers = [v for v in views.values() if v.has(vocab.CARDS_FROM_LIFE)]
    payoffs = [v for v in views.values() if v.has(vocab.DRAWS)]
    for producer in producers:
        for payoff in payoffs:
            if producer.card_id == payoff.card_id:
                continue
            mechanism = (
                f"{producer.name} converts life into cards; {payoff.name} "
                f"turns those extra cards into value."
            )
            yield _make_edge(
                "draw_engine", producer, payoff, mechanism,
                [_evidence(producer, vocab.CARDS_FROM_LIFE), _evidence(payoff, vocab.DRAWS),
                 _plain_marker(True, "life-to-cards engine")],
            )


PATTERN = PatternDef(
    name="draw_engine",
    description="Converts life into cards plus a draw payoff.",
    rule={"producer": ["CARDS_FROM_LIFE"], "payoff": ["DRAWS"], "direction": "one_way"},
    matcher=draw_engine,
)

__all__ = ["PATTERN", "draw_engine"]
