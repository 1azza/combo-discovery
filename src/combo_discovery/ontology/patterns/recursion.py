"""``sacrifice_recursion``: sacrifice outlet plus graveyard recursion."""

from __future__ import annotations

from collections.abc import Iterator

from .. import vocabulary as vocab
from ..restrictions import _pred_params, _spec_from_params
from .base import (
    CardView,
    Edge,
    PatternDef,
    _evidence,
    _make_edge,
    _plain_marker,
)


def _mentions_artifact(view: CardView) -> bool:
    """True when a card is an artifact or its script talks about artifacts.

    Sacrifice/recursion is otherwise a quadratic free-for-all; the v1 pattern is
    scoped to the artifact-recursion archetype (Goblin Welder, Myr Retriever,
    Scrap Trawler), which is what the known-combo set exercises.
    """
    if view.context.is_artifact:
        return True
    if "artifact" in (view.context.oracle_text or "").lower():
        return True
    for predicate in view.predicates:
        for value in predicate.params.values():
            if isinstance(value, str) and "artifact" in value.lower():
                return True
    return False


def sacrifice_recursion(views: dict[int, CardView]) -> Iterator[Edge]:
    outlets = [v for v in views.values() if v.has(vocab.SACRIFICE_OUTLET)]
    recursion = [v for v in views.values() if v.has(vocab.RECURS_FROM_GRAVEYARD)]
    for outlet in outlets:
        if not _mentions_artifact(outlet):
            continue
        for recur in recursion:
            if outlet.card_id == recur.card_id or not _mentions_artifact(recur):
                continue
            mutual = recur.has(vocab.SACRIFICE_OUTLET) or outlet.has(vocab.RECURS_FROM_GRAVEYARD)
            mechanism = (
                f"{outlet.name} sacrifices artifacts; {recur.name} returns "
                f"artifact cards from the graveyard, rebuilding what was sacrificed"
                + (" (recursion runs both ways)." if mutual else ".")
            )
            outlet_spec = _spec_from_params(_pred_params(outlet, vocab.SACRIFICE_OUTLET), "target")
            recur_spec = _spec_from_params(_pred_params(recur, vocab.RECURS_FROM_GRAVEYARD), "target")
            verified = bool(
                (outlet_spec is not None and not outlet_spec.unknown)
                or (recur_spec is not None and not recur_spec.unknown)
            )
            yield _make_edge(
                "sacrifice_recursion", outlet, recur, mechanism,
                [_evidence(outlet, vocab.SACRIFICE_OUTLET),
                 _evidence(recur, vocab.RECURS_FROM_GRAVEYARD),
                 _plain_marker(verified, "artifact-scoped recursion")],
                direction="mutual" if mutual else "one_way",
                score_bonus=0.0 if verified else -0.03,
            )


PATTERN = PatternDef(
    name="sacrifice_recursion",
    description="Sacrifice outlet plus graveyard recursion.",
    rule={"outlet": ["SACRIFICE_OUTLET"], "recursion": ["RECURS_FROM_GRAVEYARD"],
          "bonus": ["DIES_TRIGGER"], "direction": "mutual"},
    matcher=sacrifice_recursion,
)

__all__ = ["PATTERN", "sacrifice_recursion"]
