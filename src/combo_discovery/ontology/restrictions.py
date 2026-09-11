"""Restriction parsing and type-compatibility checks.

This module is shared between the extractor (which parses Forge restriction
strings into predicate params) and the pattern layer (which decides whether one
card can legally target/affect another, and whether an engine can copy a
partner).  Keeping it separate means restriction parsing has one home and the
patterns stay thin.

Runtime dependency direction: ``vocabulary`` -> ``restrictions`` -> ``extractor``
and ``patterns.base``.  The type-only imports below (``CardContext``,
``CardView``) are guarded so the module can be imported without a cycle; the
functions only use the duck-typed attributes they need.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import vocabulary as vocab

if TYPE_CHECKING:  # pragma: no cover - typing only (avoids import cycles)
    from .extractor import CardContext
    from .patterns.base import CardView


# ---------------------------------------------------------------------------
# Target restriction parsing (the "subject/object" half of a predicate)
# ---------------------------------------------------------------------------

# Card types / supertypes Forge can put in a restriction.  Everything else that
# is a word becomes a subtype token (Goat, Merfolk, Samurai, Forest, ...).
_RESTRICTION_TYPES = frozenset({
    "creature", "land", "artifact", "enchantment", "planeswalker", "instant",
    "sorcery", "battle", "tribal", "kindred", "permanent", "card", "aura",
    "equipment", "vehicle", "saga", "class", "background", "conspiracy",
    "phenomenon", "plane", "scheme", "vanguard", "dungeon",
})
_SUPERTYPES = frozenset({"legendary", "basic", "snow", "world", "ongoing", "host", "elite"})

# Restriction qualifiers -> structured meaning.
_CONTROLLER_QUALIFIERS = {
    "youctrl": "you",
    "oppctrl": "opponent",
    "opponentctrl": "opponent",
    "targetedplayerctrl": "targeted_player",
    "targetedcontroller": "targeted_player",
    "targetedplayectrl": "targeted_player",
}
_SELF_QUALIFIERS = frozenset({"self", "cardname", "this"})
_OTHER_QUALIFIERS = frozenset({"other", "another"})
_RIDER_QUALIFIERS = {
    "hascounters": "hascounters",
    "attacking": "attacking",
    "attackingalone": "attacking",
    "blocking": "blocking",
    "blockingalone": "blocking",
    "tapped": "tapped",
    "untapped": "untapped",
    "token": "token",
    "nontoken": "nontoken",
    "wascastbyyou": "wascastbyyou",
    "isremembered": "isremembered",
    "remembered": "remembered",
    "enchantedby": "enchantedby",
    "equippedby": "equippedby",
    "basic": "basic",
    "nonbasic": "nonbasic",
    "snow": "snow",
}
_COLOR_QUALIFIERS = frozenset({"white", "blue", "black", "red", "green", "colorless"})


@dataclass(frozen=True)
class Alternative:
    """One comma-separated alternative in a Forge restriction string."""

    types: frozenset[str] = frozenset()
    subtypes: frozenset[str] = frozenset()
    controller: str = "any"  # any | you | opponent | targeted_player
    other: bool = False
    self_only: bool = False
    legendary: bool | None = None  # True=Legendary, False=nonLegendary
    riders: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "types": sorted(self.types),
            "subtypes": sorted(self.subtypes),
            "controller": self.controller,
            "other": self.other,
            "self": self.self_only,
            "legendary": self.legendary,
            "riders": list(self.riders),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Alternative":
        legendary = data.get("legendary")
        return cls(
            types=frozenset(data.get("types") or ()),
            subtypes=frozenset(data.get("subtypes") or ()),
            controller=data.get("controller") or "any",
            other=bool(data.get("other")),
            self_only=bool(data.get("self")),
            legendary=legendary if legendary is None or isinstance(legendary, bool)
            else bool(legendary),
            riders=tuple(data.get("riders") or ()),
        )


@dataclass(frozen=True)
class Restriction:
    """A parsed Forge target/affected restriction.

    ``unknown`` means the raw string carried no recognisable type/controller
    signal (e.g. an empty restriction or one only understood by Forge), so
    callers should treat a match as low-confidence rather than assume it is
    unrestricted.
    """

    raw: str = ""
    alternatives: tuple[Alternative, ...] = ()
    unknown: bool = True
    self_only: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "unknown": self.unknown,
            "self_only": self.self_only,
            "alternatives": [alt.to_dict() for alt in self.alternatives],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Restriction | None":
        if not data:
            return None
        return cls(
            raw=str(data.get("raw") or ""),
            alternatives=tuple(
                Alternative.from_dict(item) for item in (data.get("alternatives") or ())
            ),
            unknown=bool(data.get("unknown", True)),
            self_only=bool(data.get("self_only", False)),
        )


def parse_restriction(raw: str | None) -> Restriction:
    """Parse a Forge restriction string into structured alternatives.

    Handles comma-OR alternatives (``Artifact.YouCtrl,Creature.YouCtrl``),
    dotted/plus AND qualifiers (``Creature.Other+YouCtrl``), controller
    qualifiers, ``Self``, ``Other``, legendary constraints and rider conditions.
    Unrecognised lowercase tokens become riders (so they block a strict match);
    unrecognised capitalised tokens become subtypes.
    """
    text = (raw or "").strip()
    if not text:
        return Restriction(raw="", unknown=True)

    alternatives: list[Alternative] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        types: set[str] = set()
        subtypes: set[str] = set()
        controller = "any"
        other = False
        self_only = False
        legendary: bool | None = None
        riders: set[str] = set()
        for token in (t for t in re.split(r"[.+]", part) if t):
            low = token.lower()
            if low in _CONTROLLER_QUALIFIERS:
                controller = _CONTROLLER_QUALIFIERS[low]
            elif low in _SELF_QUALIFIERS:
                self_only = True
            elif low in _OTHER_QUALIFIERS:
                other = True
            elif low == "nonlegendary":
                legendary = False
            elif low == "legendary":
                legendary = True
            elif low in _RIDER_QUALIFIERS:
                riders.add(_RIDER_QUALIFIERS[low])
            elif low in _COLOR_QUALIFIERS:
                riders.add(f"color:{low}")
            elif low.startswith(("cmc", "power", "toughness", "converted", "manavalue", "mv")):
                riders.add(f"stat:{low}")
            elif low in _RESTRICTION_TYPES:
                types.add(low.upper())
            elif token[:1].isupper():
                subtypes.add(low)
            else:
                riders.add(f"unknown:{low}")
        alternatives.append(Alternative(
            types=frozenset(types), subtypes=frozenset(subtypes),
            controller=controller, other=other, self_only=self_only,
            legendary=legendary, riders=tuple(sorted(riders)),
        ))

    if not alternatives:
        return Restriction(raw=text, unknown=True)
    recognised = any(
        alt.types or alt.subtypes or alt.self_only or alt.controller != "any"
        for alt in alternatives
    )
    return Restriction(
        raw=text,
        alternatives=tuple(alternatives),
        unknown=not recognised,
        self_only=all(alt.self_only for alt in alternatives),
    )


def _target_restriction(valid: str | None, defined: str | None) -> Restriction:
    """Resolve the restriction for a target/affected effect.

    ``valid`` wins when present; an explicit ``Defined$ Self`` becomes a
    self-only restriction (a self-untap, not an interaction); otherwise the
    target is unknown.
    """
    text = (valid or "").strip()
    if text:
        return parse_restriction(text)
    if (defined or "").strip().lower() in _SELF_QUALIFIERS:
        return parse_restriction("Card.Self")
    return parse_restriction("")


def _type_line_tokens(type_line: str) -> list[str]:
    return [
        token for token in re.split(r"[\s\u2014\u2013-]+", (type_line or "").lower())
        if token and token.isalpha()
    ]


def card_type_tokens(type_line: str) -> frozenset[str]:
    """Card types present on a type line, uppercased (CREATURE, LAND, ...)."""
    return frozenset(
        token.upper() for token in _type_line_tokens(type_line)
        if token in _RESTRICTION_TYPES
    )


def card_subtypes(type_line: str) -> frozenset[str]:
    """Subtype tokens on a type line (goat, merfolk, samurai, forest, ...)."""
    return frozenset(
        token for token in _type_line_tokens(type_line)
        if token not in _RESTRICTION_TYPES and token not in _SUPERTYPES
    )


# ---------------------------------------------------------------------------
# Type compatibility (can one card legally target/affect the other?)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompatResult:
    """Outcome of a single restriction-vs-card check."""

    status: str  # compatible | incompatible | unknown
    reason: str = ""


@dataclass(frozen=True)
class Compatibility:
    """Both directions of an engine/partner compatibility check."""

    target: CompatResult
    copy: CompatResult
    target_raw: str = ""
    copy_raw: str = ""

    @property
    def target_ok(self) -> bool:
        return self.target.status == "compatible"

    @property
    def type_verified(self) -> bool:
        return self.target.status == "compatible"

    @property
    def copy_verified(self) -> bool:
        return self.copy.status == "compatible"

    def evidence_entry(self) -> dict[str, Any]:
        """The ``evidence_json`` marker downstream consumers can filter on."""
        return {
            "kind": "compatibility",
            "type_verified": self.type_verified,
            "copy_verified": self.copy_verified,
            "target": self.target.status,
            "copy": self.copy.status,
            "target_restriction": self.target_raw or None,
            "copy_restriction": self.copy_raw or None,
            "detail": f"target={self.target.reason}; copy={self.copy.reason}",
        }


def _spec_from_params(params: dict[str, Any] | None, key: str) -> Restriction | None:
    if not params:
        return None
    return Restriction.from_dict(params.get(key))


def _pred_params(view: "CardView", predicate: str) -> dict[str, Any] | None:
    pred = view.first(predicate)
    return pred.params if pred is not None else None


def _alternative_matches(alt: Alternative, card: "CardContext") -> bool:
    """True when one restriction alternative can affect ``card``.

    Assumes the two paired cards are controlled by the same player, so an
    ``any``/``you`` controller is satisfiable; ``opponent``/``targeted_player``
    are not (we cannot guarantee the engine is on that side of the table).
    ``other`` is always satisfied because a card is never paired with itself.
    """
    if alt.controller not in ("any", "you"):
        return False
    if alt.self_only or alt.riders:
        return False
    if alt.legendary is True and not card.is_legendary:
        return False
    if alt.legendary is False and card.is_legendary:
        return False
    if alt.types:
        if alt.types & {"PERMANENT", "CARD"}:
            return True
        if alt.types & card.card_types:
            return True
        return bool(alt.subtypes & card.subtypes)
    if alt.subtypes:
        return bool(alt.subtypes & card.subtypes)
    return False


def restriction_matches_card(
    spec: Restriction | None, card: "CardContext"
) -> CompatResult:
    """Check whether a target restriction can legally select ``card``."""
    if spec is None:
        return CompatResult("unknown", "no restriction recorded")
    if spec.self_only:
        return CompatResult("incompatible", "self-only restriction")
    if spec.unknown or not spec.alternatives:
        return CompatResult("unknown", f"unparsed restriction {spec.raw!r}")
    for alt in spec.alternatives:
        if _alternative_matches(alt, card):
            return CompatResult("compatible", f"matches {spec.raw!r}")
    return CompatResult(
        "incompatible", f"{card.type_line!r} does not match {spec.raw!r}"
    )


def engine_can_copy(engine: "CardView", partner: "CardView") -> CompatResult:
    """Check the engine's copy restriction against the partner card."""
    copy_pred = engine.first(vocab.COPIES_CREATURE)
    if copy_pred is None:
        return CompatResult("compatible", "not a copy engine")
    spec = _spec_from_params(copy_pred.params, "copy")
    defined = str(copy_pred.params.get("defined") or "").lower()
    if spec is not None and not spec.unknown and not spec.self_only:
        return restriction_matches_card(spec, partner.context)
    # Defined Self (Splinter Twin) or an unparameterised copy: the engine copies
    # itself / the creature it is attached to, so the partner must be a creature.
    if spec is None or spec.unknown or spec.self_only:
        if defined in ("", "self"):
            if partner.context.is_creature:
                return CompatResult("compatible", "engine copies its host creature")
            return CompatResult("incompatible", "partner is not a copyable creature")
    return CompatResult("unknown", "unknown copy restriction")


def check_compatibility(
    engine: "CardView",
    partner: "CardView",
    *,
    target_predicate: str = vocab.UNTAPS,
) -> Compatibility:
    """Full engine/partner check: partner can target engine, engine can copy partner."""
    target_pred = partner.first(target_predicate)
    spec = _spec_from_params(target_pred.params, "target") if target_pred else None
    if spec is None and target_pred is not None:
        # Fall back to the raw ``valid`` string for predicates persisted before
        # the structured ``target`` param existed.
        spec = parse_restriction(target_pred.params.get("valid"))
    copy_pred = engine.first(vocab.COPIES_CREATURE)
    copy_spec = _spec_from_params(copy_pred.params, "copy") if copy_pred else None
    return Compatibility(
        target=restriction_matches_card(spec, engine.context),
        copy=engine_can_copy(engine, partner),
        target_raw=spec.raw if spec else "",
        copy_raw=copy_spec.raw if copy_spec else "",
    )


__all__ = [
    "Alternative",
    "CompatResult",
    "Compatibility",
    "Restriction",
    "card_subtypes",
    "card_type_tokens",
    "check_compatibility",
    "engine_can_copy",
    "parse_restriction",
    "restriction_matches_card",
]
