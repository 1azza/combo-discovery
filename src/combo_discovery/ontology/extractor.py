"""Effect rows -> typed card predicates.

This is the *pure* half of the ontology: Forge ``card_effects`` rows are turned
into :class:`CardPredicate` values with the signal parameters (zones, costs,
targets, amounts) preserved in ``params_json`` and the contributing script
lines in ``evidence_json``.  The vocabulary and its extraction contract live in
:mod:`combo_discovery.ontology.vocabulary`.

Nothing here talks to the network; the only I/O is the optional
:func:`extract_import` helper that reads an already-imported corpus.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from . import vocabulary as vocab

# Tap symbol: a standalone ``T`` in an activated ability's Cost$ (``T``,
# ``3 T``, ``T Sac<1/CARDNAME>``).  Word-guarded so it never matches a word.
_TAP_COST_RE = re.compile(r"(?<![A-Za-z])T(?![A-Za-z])")
# K:<Keyword> lines in the raw script (used for keyword-only predicates).
_KEYWORD_RE = re.compile(r"(?m)^K:([A-Za-z][A-Za-z0-9_]*)")

_MANA_SYMBOLS = ("W", "U", "B", "R", "G", "C")


# ---------------------------------------------------------------------------
# Card metadata helpers
# ---------------------------------------------------------------------------


def mana_value(mana_cost: str) -> int:
    """Best-effort converted mana cost from a Forge ``ManaCost`` string.

    ``"2 R R R"`` -> 5, ``"X X"`` -> 0, ``"no cost"`` -> 0.  Hybrid/split
    symbols count as one.
    """
    total = 0
    for token in re.split(r"[\s/]+", mana_cost or ""):
        if not token:
            continue
        if token.isdigit():
            total += int(token)
        elif token.upper() in ("X", "Y", "Z", "NO", "COST"):
            continue
        else:
            total += 1
    return total


def colors_from_mana_cost(mana_cost: str) -> frozenset[str]:
    """Colour identity approximated from the pips in ``mana_cost``."""
    found = {ch for ch in (mana_cost or "") if ch in _MANA_SYMBOLS}
    found.discard("C")  # colourless is not a colour for identity purposes
    return frozenset(found)


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


@dataclass(frozen=True)
class CardContext:
    """Card metadata the extractor is allowed to look at (no effects)."""

    card_id: int
    name: str
    normalized_name: str
    mana_cost: str
    type_line: str
    colors: str
    oracle_text: str
    keywords: tuple[str, ...] = ()

    @property
    def cmc(self) -> int:
        return mana_value(self.mana_cost)

    @property
    def color_identity(self) -> frozenset[str]:
        from_cost = colors_from_mana_cost(self.mana_cost)
        if from_cost:
            return from_cost
        return frozenset(ch for ch in (self.colors or "").upper() if ch in _MANA_SYMBOLS)

    @property
    def is_artifact(self) -> bool:
        return "artifact" in (self.type_line or "").lower()

    @property
    def is_creature(self) -> bool:
        return "creature" in (self.type_line or "").lower()

    @property
    def is_legendary(self) -> bool:
        return "legendary" in (self.type_line or "").lower()

    @property
    def card_types(self) -> frozenset[str]:
        return card_type_tokens(self.type_line)

    @property
    def subtypes(self) -> frozenset[str]:
        return card_subtypes(self.type_line)

    @property
    def has_x_cost(self) -> bool:
        return "X" in (self.mana_cost or "").upper()


@dataclass
class CardEffect:
    """One row of ``card_effects`` in a DB-agnostic shape."""

    id: int
    card_id: int
    face_index: int
    effect_kind: str
    verb: str
    ability_type: str | None = None
    zone: str | None = None
    is_optional: bool = False
    is_svar: bool = False
    svar_name: str | None = None
    description: str = ""
    line_no: int | None = None
    params: dict[str, str] = field(default_factory=dict)


@dataclass
class CardPredicate:
    """A deduplicated predicate for one ``(card, face)`` pair."""

    card_id: int
    face_index: int
    predicate: str
    params: dict[str, Any] = field(default_factory=dict)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    confidence: float = 1.0

    def as_params_json(self) -> str:
        return json.dumps(self.params, sort_keys=True, default=str)

    def as_evidence_json(self) -> str:
        return json.dumps(self.evidence, sort_keys=True, default=str)


# ---------------------------------------------------------------------------
# Extraction rules
# ---------------------------------------------------------------------------


def _p(params: dict[str, str], key: str, default: str = "") -> str:
    value = params.get(key, default)
    return value if value is not None else default


def _has_tap_cost(cost: str) -> bool:
    return bool(_TAP_COST_RE.search(cost or ""))


def _is_treasure(effect: CardEffect) -> bool:
    token_script = _p(effect.params, "TokenScript").lower()
    if "treasure" in token_script or "c_a_treasure" in token_script:
        return True
    return "treasure" in f"{effect.description} {_p(effect.params, 'TokenName')}".lower()


def _origin(effect: CardEffect) -> str:
    return _p(effect.params, "Origin") or _p(effect.params, "OriginZone")


def _destination(effect: CardEffect) -> str:
    return _p(effect.params, "Destination") or _p(effect.params, "DestinationZone")


def _is_self_sacrifice(effect: CardEffect) -> bool:
    for key in ("SacValid", "Defined", "ValidCards", "ValidCard"):
        value = _p(effect.params, key)
        if value in ("Self", "CARDNAME"):
            return True
        if "CARDNAME" in value and value.replace("CARDNAME", "").strip(" .") == "Card.Self":
            return True
    return False


def _sacrifices_self_from_cost(cost: str) -> bool:
    lower = (cost or "").lower()
    if "sac<" not in lower:
        return False
    return "cardname" in lower or "self" in lower


def _sacrifices_other_from_cost(cost: str) -> bool:
    lower = (cost or "").lower()
    if "sac<" not in lower:
        return False
    return not _sacrifices_self_from_cost(cost)


def _recurs_destination(effect: CardEffect) -> bool:
    return _destination(effect) in ("Hand", "Battlefield", "Library")


# ---------------------------------------------------------------------------
# Ability-scoped effect linking
#
# Predicates are card-scoped, but combo patterns care about *which* ability an
# effect belongs to: an ETB trigger and an untap on two unrelated abilities must
# not be combined.  Every effect gets a stable reference and, when reachable via
# Execute$/SubAbility$/Choices$/True|FalseSubAbility$/AddAbility$ from a root
# line, the root ability it belongs to and the chain that reaches it.
# ---------------------------------------------------------------------------

_KIND_LETTER = {"ability": "A", "trigger": "T", "static": "S", "replacement": "R"}

#: Effect-level params that make a trigger/ability conditional or one-shot.
_GATE_KEYS: dict[str, str] = {
    "FirstAttack": "first_attack",
    "Delirium": "delirium",
    "ValidResult": "valid_result",
    "ActivationLimit": "activation_limit",
    "GameActivationLimit": "activation_limit",
    "NumLimitEachTurn": "activation_limit",
    "ResolvedLimit": "activation_limit",
    "MayPlayLimit": "activation_limit",
    "MyTurn": "turn_restriction",
    "YourTurn": "turn_restriction",
    "PlayerTurn": "turn_restriction",
    "OpponentTurn": "turn_restriction",
    "ConditionPlayerTurn": "turn_restriction",
    "ConditionOpponentTurn": "turn_restriction",
    "PhaseInOrOut": "phase_out",
    "WontPhaseInNormal": "phase_out",
    "ForgetOnPhasedIn": "phase_out",
    "PhaseOut": "phase_out",
    "FirstTime": "first_time",
    "OnlyFirst": "only_first",
    "OnlyFirstSpell": "only_first",
}

#: Keys whose value names a chained SVar/ability.
_CHAIN_KEYS = (
    "Execute", "SubAbility", "TrueSubAbility", "FalseSubAbility", "Choices",
    "AddAbility",
)
_CHAIN_SPLIT = re.compile(r"[,&]")


def effect_gates(effect: CardEffect) -> dict[str, str]:
    """Conditional / non-repeatable riders attached to one effect."""
    gates: dict[str, str] = {}
    if effect.verb == "RolledDie":
        gates["rolled_die"] = str(effect.params.get("ValidResult") or True)
    for key, label in _GATE_KEYS.items():
        value = effect.params.get(key)
        if value not in (None, ""):
            gates.setdefault(label, value)
    return gates


def effect_ref(effect: CardEffect) -> str:
    """Stable ability reference, e.g. ``line:0:6:T:ChangesZone`` or
    ``svar:0:DBUntap:Untap``."""
    kind = _KIND_LETTER.get(effect.effect_kind, "?")
    verb = effect.verb or "?"
    if effect.is_svar and effect.svar_name:
        return f"svar:{effect.face_index}:{effect.svar_name}:{verb}"
    return f"line:{effect.face_index}:{effect.line_no}:{kind}:{verb}"


def _chain_targets(effect: CardEffect) -> list[str]:
    names: list[str] = []
    for key in _CHAIN_KEYS:
        value = effect.params.get(key)
        if not value:
            continue
        for part in _CHAIN_SPLIT.split(str(value)):
            part = part.strip()
            if part:
                names.append(part)
    return names


def build_ability_links(effects: list[CardEffect]) -> dict[int, dict[str, Any]]:
    """Map ``effect.id`` -> ability metadata (ref, root, chain, gates).

    Roots are the non-SVar lines (``A:``/``T:``/``S:``/``R:``); SVars are linked
    to the root that reaches them first through the chain keys.  Deterministic:
    effects are processed in id order.  Orphan SVars become their own root.
    """
    by_svar: dict[tuple[int, str], CardEffect] = {}
    for effect in effects:
        if effect.is_svar and effect.svar_name:
            by_svar.setdefault((effect.face_index, effect.svar_name), effect)

    links: dict[int, dict[str, Any]] = {}

    def walk(root: CardEffect, current: CardEffect, via: list[str],
             chain_gates: dict[str, str]) -> None:
        gates = effect_gates(current)
        merged = dict(chain_gates)
        for key, value in gates.items():
            merged.setdefault(key, value)
        links[current.id] = {
            "ability_ref": effect_ref(current),
            "root_ability_ref": effect_ref(root),
            "via": list(via),
            "is_root": current.id == root.id,
            "root_kind": root.effect_kind,
            "root_verb": root.verb,
            "root_cost": root.params.get("Cost", "") or "",
            "gates": gates,
            "chain_gates": merged,
        }
        for name in _chain_targets(current):
            child = by_svar.get((current.face_index, name))
            if child is not None and child.id not in links:
                walk(root, child, via + [name], merged)

    for effect in effects:
        if not effect.is_svar and effect.id not in links:
            walk(effect, effect, [], {})

    for effect in effects:
        if effect.id not in links:
            gates = effect_gates(effect)
            links[effect.id] = {
                "ability_ref": effect_ref(effect),
                "root_ability_ref": effect_ref(effect),
                "via": [],
                "is_root": False,
                "root_kind": effect.effect_kind,
                "root_verb": effect.verb,
                "root_cost": effect.params.get("Cost", "") or "",
                "gates": gates,
                "chain_gates": gates,
            }
    return links


_ABILITY_PARAM_KEYS = (
    "ability_ref", "root_ability_ref", "via", "is_root", "root_kind",
    "root_verb", "root_cost", "gates", "chain_gates",
)


def classify_effect(effect: CardEffect) -> list[tuple[str, dict[str, Any]]]:
    """Map one effect to zero or more ``(predicate, params)`` pairs.

    Deterministic: the returned list order is the vocabulary order of matches
    found in a fixed if-chain.  Every public predicate has a documented rule in
    :data:`combo_discovery.ontology.vocabulary.EXTRACTION_RULES`.
    """
    verb = effect.verb
    params = effect.params
    cost = _p(params, "Cost")
    matches: list[tuple[str, dict[str, Any]]] = []
    kind = effect.effect_kind

    # -- mana ---------------------------------------------------------------
    if verb == "Mana":
        matches.append((vocab.PRODUCES_MANA, {
            "color": _p(params, "Produced") or None,
            "amount": _p(params, "Amount") or None,
            "activation": _p(params, "Activation") or None,
            "x": "X" in _p(params, "Amount").upper(),
            "source": "ability",
        }))
    if verb == "Token" and _is_treasure(effect):
        matches.append((vocab.PRODUCES_MANA, {
            "color": "Any", "amount": _p(params, "TokenAmount") or None,
            "source": "treasure",
        }))

    # -- tapping ------------------------------------------------------------
    if verb in ("Untap", "TapOrUntap", "UntapAll") and kind != "replacement":
        # Forge names untap targets three ways: ValidTgts / ValidCards, and
        # UntapType (Cloud-of-Faeries-style "untap up to N lands").
        untap_type = _p(params, "UntapType")
        effective = _p(params, "ValidTgts") or _p(params, "ValidCards") or untap_type
        target = _target_restriction(effective, _p(params, "Defined"))
        matches.append((vocab.UNTAPS, {
            "verb": verb,
            "defined": _p(params, "Defined") or None,
            "valid": effective or None,
            "untap_type": untap_type or None,
            "target": target.to_dict(),
            "amount": _p(params, "Amount") or None,
        }))
    if kind == "ability" and effect.ability_type in ("AB", "ST") and _has_tap_cost(cost):
        matches.append((vocab.TAPS_COST, {
            "cost": cost, "verb": verb, "ability_type": effect.ability_type,
        }))

    # -- copying / triggers -------------------------------------------------
    if verb == "CopyPermanent":
        copy_valid = _p(params, "ValidTgts") or _p(params, "ValidCards")
        matches.append((vocab.COPIES_CREATURE, {
            "valid": copy_valid or None,
            "defined": _p(params, "Defined") or None,
            "add_keywords": _p(params, "AddKeywords") or None,
            "at_eot": _p(params, "AtEOT") or None,
            "copy": _target_restriction(copy_valid, None).to_dict(),
        }))
    if verb in ("CopySpellAbility", "CopySpell", "CopySpellMay"):
        matches.append((vocab.COPIES_SPELL, {"verb": verb}))
    if kind == "trigger" and verb == "ChangesZone":
        if _destination(effect) == "Battlefield" and "Self" in _p(params, "ValidCard"):
            matches.append((vocab.ETB_TRIGGER, {
                "valid": _p(params, "ValidCard"),
                "origin": _origin(effect) or None,
                "execute": _p(params, "Execute") or None,
            }))
        if _origin(effect) == "Battlefield" and _destination(effect) == "Graveyard" \
                and "Self" in _p(params, "ValidCard"):
            matches.append((vocab.DIES_TRIGGER, {
                "valid": _p(params, "ValidCard"),
                "execute": _p(params, "Execute") or None,
            }))

    # -- sacrifice ----------------------------------------------------------
    if verb in ("Sacrifice", "SacrificeAll"):
        if _is_self_sacrifice(effect):
            matches.append((vocab.SACRIFICES_SELF, {"verb": verb}))
        else:
            sac_valid = _p(params, "ValidCards") or _p(params, "ValidCard")
            matches.append((vocab.SACRIFICE_OUTLET, {
                "verb": verb,
                "valid": sac_valid or _p(params, "Defined") or None,
                "target": _target_restriction(sac_valid, _p(params, "Defined")).to_dict(),
            }))
    if _sacrifices_self_from_cost(cost):
        matches.append((vocab.SACRIFICES_SELF, {"cost": cost}))
    elif _sacrifices_other_from_cost(cost):
        matches.append((vocab.SACRIFICE_OUTLET, {
            "cost": cost, "target": parse_restriction("").to_dict(),
        }))

    # -- zones --------------------------------------------------------------
    if verb in ("ChangeZone", "ChangeZoneAll"):
        origin = _origin(effect)
        dest = _destination(effect)
        matches.append((vocab.MOVES_ZONE, {
            "origin": origin or None, "destination": dest or None,
            "defined": _p(params, "Defined") or None,
            "valid": _p(params, "ValidTgts") or _p(params, "ValidCards") or None,
        }))
        if "Graveyard" in origin and _recurs_destination(effect):
            matches.append((vocab.RECURS_FROM_GRAVEYARD, {
                "origin": origin, "destination": dest,
                "defined": _p(params, "Defined") or None,
                "valid": _p(params, "ValidTgts") or _p(params, "ValidCards") or None,
            }))
        if dest == "Exile":
            matches.append((vocab.EXILES, {
                "origin": origin or None, "defined": _p(params, "Defined") or None,
                "target": _target_restriction(
                    _p(params, "ValidTgts") or _p(params, "ValidCards"), _p(params, "Defined")
                ).to_dict(),
            }))
    if verb in ("Exile", "ExileAll"):
        matches.append((vocab.EXILES, {
            "verb": verb,
            "target": _target_restriction(
                _p(params, "ValidTgts") or _p(params, "ValidCards"), _p(params, "Defined")
            ).to_dict(),
        }))
    if verb == "ChangeZone" and "PayLife" in cost and "TopOfLibrary" in _p(params, "Defined"):
        matches.append((vocab.CARDS_FROM_LIFE, {"cost": cost}))

    # -- combat / life / cards ---------------------------------------------
    if verb in ("DealDamage", "DamageAll", "DamageEach", "DamageMultiEffect"):
        matches.append((vocab.DEALS_DAMAGE, {
            "amount": _p(params, "NumDmg") or None,
            "defined": _p(params, "Defined") or None,
            "target": _target_restriction(
                _p(params, "ValidTgts") or _p(params, "ValidCards"), _p(params, "Defined")
            ).to_dict(),
        }))
    if verb == "GainLife":
        matches.append((vocab.GAINS_LIFE, {"amount": _p(params, "LifeAmount") or None}))
    if verb == "LoseLife":
        matches.append((vocab.LOSES_LIFE, {"amount": _p(params, "LifeAmount") or None}))
    if verb in ("Draw", "DrawAll", "DrawCards"):
        matches.append((vocab.DRAWS, {"amount": _p(params, "NumCards") or None}))
    if verb in ("Discard", "DiscardAll", "DiscardEach"):
        matches.append((vocab.DISCARDS, {"amount": _p(params, "NumCards") or None}))
    if verb in ("Mill", "MillAll"):
        matches.append((vocab.MILLS, {
            "amount": _p(params, "NumCards") or None,
            "target": _target_restriction(
                _p(params, "ValidTgts") or _p(params, "ValidCards"), _p(params, "Defined")
            ).to_dict(),
        }))
    if verb == "Token":
        matches.append((vocab.CREATES_TOKEN, {
            "script": _p(params, "TokenScript") or None,
            "amount": _p(params, "TokenAmount") or None,
            "treasure": _is_treasure(effect),
        }))
    if verb in ("GainControl", "GainControlVariant"):
        matches.append((vocab.GAINS_CONTROL, {
            "verb": verb,
            "target": _target_restriction(
                _p(params, "ValidTgts") or _p(params, "ValidCards"), _p(params, "Defined")
            ).to_dict(),
        }))
    if verb in ("PutCounter", "PutCounterAll", "PutCounterEach", "AddCounter"):
        matches.append((vocab.ADDS_COUNTERS, {
            "counter": _p(params, "CounterType") or None,
            "target": _target_restriction(
                _p(params, "ValidTgts") or _p(params, "ValidCards"), _p(params, "Defined")
            ).to_dict(),
        }))
    if verb in ("Destroy", "DestroyAll", "DestroyAllEffect"):
        matches.append((vocab.DESTROYS, {
            "verb": verb,
            "target": _target_restriction(
                _p(params, "ValidTgts") or _p(params, "ValidCards"), _p(params, "Defined")
            ).to_dict(),
        }))

    # -- cost / casting -----------------------------------------------------
    if kind == "static" and verb in ("ReduceCost", "ReduceCostAll"):
        matches.append((vocab.REDUCES_COST, {
            "amount": _p(params, "Amount") or _p(params, "ReduceCost") or None,
        }))
    if kind == "static" and verb == "AlternativeCost":
        matches.append((vocab.ALTERNATIVE_COST, {"cost": _p(params, "Cost") or None}))

    text = f"{effect.description} {_p(params, 'SpellDescription')}".lower()
    if verb == "Effect" and "graveyard" in text and ("cast" in text or "play" in text):
        matches.append((vocab.CASTS_FROM_GRAVEYARD, {"source": "effect"}))
    if kind == "static" and "escape" in _p(params, "AddKeyword").lower():
        matches.append((vocab.CASTS_FROM_GRAVEYARD, {
            "source": "escape", "keyword": _p(params, "AddKeyword"),
        }))

    # -- colour / library ---------------------------------------------------
    if kind == "static" and _p(params, "AddColor"):
        matches.append((vocab.SETS_COLOR, {"add_color": _p(params, "AddColor")}))
    if verb in ("RearrangeTopOfLibrary",) or (
        verb in ("ChangeZone", "ChangeZoneAll")
        and "Library" in _origin(effect)
        and _destination(effect) == "Library"
    ):
        matches.append((vocab.TUTORS, {
            "origin": _origin(effect) or None, "destination": _destination(effect) or None,
        }))

    if not matches:
        matches.append((vocab.OTHER, {"verb": verb}))
    return matches


def extract_card_predicates(
    context: CardContext, effects: list[CardEffect]
) -> list[CardPredicate]:
    """Deduplicate matches into one :class:`CardPredicate` per ``(face, predicate)``.

    ``params`` keeps the first matching rule's parameters; ``evidence`` lists
    every contributing effect (id, line, verb, svar, params).
    """
    grouped: dict[tuple[int, str], CardPredicate] = {}
    order: list[tuple[int, str]] = []
    links = build_ability_links(effects)

    def record(match: tuple[str, dict[str, Any]], effect: CardEffect) -> None:
        predicate, matched_params = match
        key = (effect.face_index, predicate)
        if key not in grouped:
            grouped[key] = CardPredicate(
                card_id=context.card_id,
                face_index=effect.face_index,
                predicate=predicate,
                params=dict(matched_params),
            )
            order.append(key)
        entry = grouped[key]
        for name, value in matched_params.items():
            entry.params.setdefault(name, value)

    # Keyword-only predicates (Storm) are card-scoped, attached to face 0.
    if any(k.lower() == "storm" for k in context.keywords):
        storm_key = (0, vocab.STORM)
        grouped[storm_key] = CardPredicate(
            card_id=context.card_id, face_index=0, predicate=vocab.STORM,
            params={"keyword": "Storm"},
            evidence=[{"kind": "keyword", "verb": "Storm", "line": None}],
        )
        order.append(storm_key)

    for effect in effects:
        ability = links.get(effect.id) or {}
        for match in classify_effect(effect):
            predicate, matched_params = match
            merged = dict(matched_params)
            merged.update(ability)
            record((predicate, merged), effect)
            grouped[(effect.face_index, predicate)].evidence.append({
                "effect_id": effect.id,
                "line": effect.line_no,
                "kind": effect.effect_kind,
                "verb": effect.verb,
                "svar": effect.svar_name,
                "params": merged,
            })

    # Aggregate the per-match ability info onto the card-level predicate so the
    # pattern gate can see every ability a predicate came from.
    for predicate in grouped.values():
        if predicate.predicate == vocab.STORM:
            continue
        details: list[dict[str, Any]] = []
        seen: set[str] = set()
        for entry in predicate.evidence:
            params = entry.get("params") or {}
            ref = params.get("ability_ref")
            if not ref or ref in seen:
                continue
            seen.add(ref)
            details.append({key: params.get(key) for key in _ABILITY_PARAM_KEYS})
        if details:
            predicate.params["ability_details"] = details
            predicate.params["ability_refs"] = sorted(
                {str(d.get("ability_ref") or "") for d in details} - {""}
            )
            predicate.params["root_ability_refs"] = sorted(
                {str(d.get("root_ability_ref") or "") for d in details} - {""}
            )

    # Deterministic output: face order, then vocabulary order.
    rank = {name: i for i, name in enumerate(vocab.ALL_PREDICATES)}
    keys = sorted(set(order), key=lambda k: (k[0], rank.get(k[1], len(rank))))
    return [grouped[key] for key in keys]


# ---------------------------------------------------------------------------
# Corpus reading (the only I/O in this module)
# ---------------------------------------------------------------------------


def parse_keywords(raw: str | None) -> tuple[str, ...]:
    """Extract ``K:<Keyword>`` names from a raw Forge script."""
    if not raw:
        return ()
    seen: list[str] = []
    for match in _KEYWORD_RE.finditer(raw):
        keyword = match.group(1)
        if keyword not in seen:
            seen.append(keyword)
    return tuple(seen)


def load_contexts(
    conn: sqlite3.Connection, import_id: str
) -> tuple[dict[int, CardContext], dict[int, list[CardEffect]]]:
    """Read cards, effects and keywords for one import into memory."""
    keywords: dict[int, tuple[str, ...]] = {}
    for row in conn.execute(
        "SELECT card_id, raw FROM card_scripts WHERE import_id = ?", (import_id,)
    ):
        keywords[int(row["card_id"])] = parse_keywords(row["raw"])

    contexts: dict[int, CardContext] = {}
    for row in conn.execute(
        "SELECT id, name, normalized_name, mana_cost, type_line, colors, oracle_text "
        "FROM cards WHERE import_id = ? ORDER BY id",
        (import_id,),
    ):
        card_id = int(row["id"])
        contexts[card_id] = CardContext(
            card_id=card_id,
            name=row["name"] or "",
            normalized_name=row["normalized_name"] or "",
            mana_cost=row["mana_cost"] or "",
            type_line=row["type_line"] or "",
            colors=row["colors"] or "",
            oracle_text=row["oracle_text"] or "",
            keywords=keywords.get(card_id, ()),
        )

    effects: dict[int, list[CardEffect]] = defaultdict(list)
    for row in conn.execute(
        "SELECT id, card_id, face_index, effect_kind, verb_or_mode, ability_type, "
        "zone, is_optional, is_svar, svar_name, description, line_no, params_json "
        "FROM card_effects WHERE import_id = ? ORDER BY card_id, face_index, id",
        (import_id,),
    ):
        try:
            params = json.loads(row["params_json"] or "{}")
        except (ValueError, TypeError):
            params = {}
        effects[int(row["card_id"])].append(CardEffect(
            id=int(row["id"]),
            card_id=int(row["card_id"]),
            face_index=int(row["face_index"] or 0),
            effect_kind=row["effect_kind"] or "",
            verb=row["verb_or_mode"] or "",
            ability_type=row["ability_type"],
            zone=row["zone"],
            is_optional=bool(row["is_optional"]),
            is_svar=bool(row["is_svar"]),
            svar_name=row["svar_name"],
            description=row["description"] or "",
            line_no=row["line_no"],
            params=params,
        ))
    return contexts, dict(effects)


def extract_import(
    conn: sqlite3.Connection, import_id: str
) -> tuple[dict[int, CardContext], dict[int, list[CardPredicate]]]:
    """Extract predicates for every card in one corpus import."""
    contexts, effects = load_contexts(conn, import_id)
    predicates = {
        card_id: extract_card_predicates(context, effects.get(card_id, []))
        for card_id, context in contexts.items()
    }
    return contexts, predicates


__all__ = [
    "Alternative",
    "CardContext",
    "CardEffect",
    "CardPredicate",
    "Restriction",
    "build_ability_links",
    "card_subtypes",
    "card_type_tokens",
    "classify_effect",
    "colors_from_mana_cost",
    "effect_gates",
    "effect_ref",
    "extract_card_predicates",
    "extract_import",
    "load_contexts",
    "mana_value",
    "parse_keywords",
    "parse_restriction",
]
