"""Shared pattern machinery: views, edges, scoring, gates and the registry type.

Every pattern module imports its matcher helpers from here.  Nothing in this
module knows about a specific combo pattern: it holds :class:`CardView`,
:class:`Edge`, :class:`PatternDef`, the transparent scoring function, the
evidence/marker builders, and the ability-scope gate helpers used by more than
one pattern.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from .. import vocabulary as vocab
from ..extractor import CardContext, CardEffect, CardPredicate

#: Additive, clamped scoring weights (documented in ``edges``):
#: base per pattern + 0.04/predicate pair (max 0.16) + shared colour
#: + cheapness bonuses.  Heuristics for ranking; recall is predicate-driven.
PATTERN_BASE: dict[str, float] = {
    "infinite_etb_loop": 0.60,
    "color_lock_mill": 0.72,
    "free_cast_loops": 0.65,
    "sacrifice_recursion": 0.60,
    "storm_engine": 0.55,
    "draw_engine": 0.45,
    "mana_engine": 0.30,
}

#: Predicates that make a tap-cost ability an "impactful" engine.
_IMPACT_PREDICATES: tuple[str, ...] = (
    vocab.COPIES_CREATURE,
    vocab.COPIES_SPELL,
    vocab.DEALS_DAMAGE,
    vocab.LOSES_LIFE,
    vocab.DESTROYS,
    vocab.MILLS,
    vocab.DISCARDS,
    vocab.GAINS_CONTROL,
    vocab.ADDS_COUNTERS,
)


def score_edge(pattern: str, source: CardView, target: CardView, evidence_count: int) -> float:
    score = PATTERN_BASE.get(pattern, 0.2)
    score += 0.04 * min(4, evidence_count)
    if source.context.color_identity & target.context.color_identity:
        score += 0.05
    if max(source.context.cmc, target.context.cmc) <= 3:
        score += 0.03
    if source.context.cmc <= 2 and target.context.cmc <= 2:
        score += 0.02
    return round(min(score, 1.0), 4)


@dataclass
class Edge:
    """One oriented interaction between two cards for a pattern."""

    pattern: str
    source: CardView
    target: CardView
    mechanism: str
    score: float
    evidence: list[dict[str, Any]] = field(default_factory=list)
    direction: str = "one_way"

    @property
    def pair_key(self) -> tuple[str, frozenset[int]]:
        return (self.pattern, frozenset((self.source.card_id, self.target.card_id)))


@dataclass
class CardView:
    """A card's metadata plus its extracted predicates and effect verbs."""

    context: CardContext
    predicates: list[CardPredicate] = field(default_factory=list)
    verbs: frozenset[str] = frozenset()
    _by_pred: dict[str, CardPredicate] = field(default_factory=dict, repr=False)

    @classmethod
    def build(
        cls,
        context: CardContext,
        predicates: list[CardPredicate],
        effects: Iterable[CardEffect] = (),
    ) -> CardView:
        by_pred: dict[str, CardPredicate] = {}
        for predicate in predicates:
            by_pred.setdefault(predicate.predicate, predicate)
        return cls(
            context=context,
            predicates=list(predicates),
            verbs=frozenset(e.verb for e in effects),
            _by_pred=by_pred,
        )

    @property
    def card_id(self) -> int:
        return self.context.card_id

    @property
    def name(self) -> str:
        return self.context.name

    def has(self, predicate: str) -> bool:
        return predicate in self._by_pred

    def first(self, predicate: str) -> CardPredicate | None:
        return self._by_pred.get(predicate)

    def any_of(self, predicates: Iterable[str]) -> str | None:
        for predicate in predicates:
            if predicate in self._by_pred:
                return predicate
        return None


def _evidence(view: CardView, predicate: str) -> dict[str, Any]:
    found = view.first(predicate)
    if found is None:
        return {"card_id": view.card_id, "name": view.name, "predicate": predicate}
    return {
        "card_id": view.card_id,
        "name": view.name,
        "predicate": predicate,
        "params": found.params,
        "lines": [entry.get("line") for entry in found.evidence if entry.get("line")],
    }


def _make_edge(
    pattern: str,
    source: CardView,
    target: CardView,
    mechanism: str,
    evidence: list[dict[str, Any]],
    *,
    direction: str = "one_way",
    score_bonus: float = 0.0,
) -> Edge:
    return Edge(
        pattern=pattern,
        source=source,
        target=target,
        mechanism=mechanism,
        score=min(1.0, round(score_edge(pattern, source, target, len(evidence)) + score_bonus, 4)),
        evidence=evidence,
        direction=direction,
    )


def _plain_marker(type_verified: bool, note: str) -> dict[str, Any]:
    """A compatibility evidence entry for patterns without a target check."""
    return {
        "kind": "compatibility",
        "type_verified": type_verified,
        "copy_verified": False,
        "target": "compatible" if type_verified else "unknown",
        "copy": "not_applicable",
        "target_restriction": None,
        "copy_restriction": None,
        "detail": note,
    }


# ---------------------------------------------------------------------------
# Ability-scope: a pattern must not combine two unrelated abilities
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AbilityLink:
    """Whether an untap is part of the card's self-ETB engine."""

    linked: bool
    kind: str = "none"  # etb_chain | repeatable_trigger | resource_handoff | none
    ability_ref: str = ""
    root_ref: str = ""
    via: tuple[str, ...] = ()
    gates: tuple[str, ...] = ()


def _ability_details(pred: CardPredicate | None) -> list[dict[str, Any]]:
    if pred is None:
        return []
    details = pred.params.get("ability_details")
    if isinstance(details, list) and details:
        return details
    ref = pred.params.get("ability_ref")
    if not ref:
        return []
    return [{
        "ability_ref": ref,
        "root_ability_ref": pred.params.get("root_ability_ref", ref),
        "via": list(pred.params.get("via") or []),
        "is_root": bool(pred.params.get("is_root")),
        "root_kind": pred.params.get("root_kind"),
        "root_cost": pred.params.get("root_cost", "") or "",
        "gates": dict(pred.params.get("gates") or {}),
        "chain_gates": dict(pred.params.get("chain_gates") or {}),
    }]


def _ability_refs(pred: CardPredicate | None) -> set[str]:
    if pred is None:
        return set()
    refs = {str(r) for r in (pred.params.get("ability_refs") or []) if r}
    ref = pred.params.get("ability_ref")
    if ref:
        refs.add(str(ref))
    return refs


def _etb_refs(view: CardView) -> set[str]:
    return _ability_refs(view.first(vocab.ETB_TRIGGER))


def _same_ability(pred_a: CardPredicate | None, pred_b: CardPredicate | None) -> bool:
    return bool(_ability_refs(pred_a) & _ability_refs(pred_b))


def _cost_has_mana(cost: str) -> bool:
    """True when an activation cost contains a mana component.

    ``T PayEnergy<2>`` -> False; ``1 T`` -> True.  Angle-bracketed costs
    (``PayEnergy<...>``, ``Sac<...>``, ``PayLife<...>``) are not mana.
    """
    for token in re.split(r"[\s,]+", cost or ""):
        if not token or "<" in token or token == "T":
            continue
        if token.isdigit() or re.fullmatch(r"\{.*\}", token):
            return True
        if token.isalpha() and any(ch in token.upper() for ch in "WUBRGCS"):
            return True
    return False


_RESOURCE_PREDICATES = (vocab.PRODUCES_MANA, vocab.ADDS_COUNTERS, vocab.CREATES_TOKEN)


def _etb_resource_predicates(view: CardView) -> set[str]:
    """Predicates that belong to the card's self-ETB chain and make a resource."""
    etb_refs = _etb_refs(view)
    if not etb_refs:
        return set()
    found: set[str] = set()
    for pred in view.predicates:
        roots = {str(r) for r in (pred.params.get("root_ability_refs") or []) if r}
        if roots & etb_refs and pred.predicate in _RESOURCE_PREDICATES:
            found.add(pred.predicate)
    return found


def check_ability_link(partner: CardView) -> AbilityLink:
    """Is the partner's untap part of a repeatable self-ETB engine?

    Three accepted shapes (all require no conditional/one-shot gate):

    * ``etb_chain`` — the untap is reached from the self-ETB root through
      Execute$/SubAbility$/Choices$/… (the strict ability-scope rule);
    * ``repeatable_trigger`` — the untap is on a *different* triggered ability
      that recurs (e.g. an upkeep trigger) with no gate;
    * ``resource_handoff`` — the untap is an activated ability with no mana cost
      whose activation resource the self-ETB produces (e.g. energy counters).
    """
    etb_refs = _etb_refs(partner)
    details = _ability_details(partner.first(vocab.UNTAPS))
    for detail in details:
        if detail.get("chain_gates"):
            continue
        if detail.get("root_ability_ref") in etb_refs or detail.get("ability_ref") in etb_refs:
            return AbilityLink(
                True, "etb_chain", str(detail.get("ability_ref") or ""),
                str(detail.get("root_ability_ref") or ""), tuple(detail.get("via") or []),
            )
    for detail in details:
        if detail.get("chain_gates"):
            continue
        if detail.get("root_kind") == "trigger":
            return AbilityLink(
                True, "repeatable_trigger", str(detail.get("ability_ref") or ""),
                str(detail.get("root_ability_ref") or ""), tuple(detail.get("via") or []),
            )
    resources = _etb_resource_predicates(partner)
    for detail in details:
        if detail.get("chain_gates"):
            continue
        if detail.get("root_kind") == "ability" and resources \
                and not _cost_has_mana(str(detail.get("root_cost") or "")):
            return AbilityLink(
                True, "resource_handoff", str(detail.get("ability_ref") or ""),
                str(detail.get("root_ability_ref") or ""), tuple(detail.get("via") or []),
            )
    return AbilityLink(False)


def _mana_linked(view: CardView) -> bool:
    """True when the card's mana production is part of its ETB or tap ability."""
    allowed = _ability_refs(view.first(vocab.ETB_TRIGGER)) | _ability_refs(
        view.first(vocab.TAPS_COST)
    )
    if not allowed:
        return False
    for detail in _ability_details(view.first(vocab.PRODUCES_MANA)):
        if detail.get("root_ability_ref") in allowed or detail.get("ability_ref") in allowed:
            return True
    return False


# ---------------------------------------------------------------------------
# Pattern definition (registered in ``patterns/__init__.py``)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PatternDef:
    """A named, versioned matching rule plus its matcher and scorer.

    ``rule`` is what the builder persists in the ``patterns`` table.  ``matcher``
    turns views into edges; ``scorer`` is the ranking function (the per-pattern
    bonus is applied inside the matcher).  ``count`` is the registry's count
    hook so the evaluation layer can report per-pattern volumes without
    hard-coding names.
    """

    name: str
    description: str
    rule: dict[str, Any]
    matcher: Callable[[dict[int, CardView]], Iterator[Edge]]
    scorer: Callable[[str, CardView, CardView, int], float] = score_edge
    version: int = 1

    def match(self, views: dict[int, CardView]) -> Iterator[Edge]:
        return self.matcher(views)

    def count(self, views: dict[int, CardView]) -> int:
        return sum(1 for _ in self.matcher(views))


__all__ = [
    "AbilityLink",
    "CardView",
    "Edge",
    "PATTERN_BASE",
    "PatternDef",
    "_IMPACT_PREDICATES",
    "_ability_details",
    "_ability_refs",
    "_cost_has_mana",
    "_etb_refs",
    "_etb_resource_predicates",
    "_evidence",
    "_make_edge",
    "_mana_linked",
    "_plain_marker",
    "_same_ability",
    "check_ability_link",
    "score_edge",
]
