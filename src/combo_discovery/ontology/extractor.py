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
        matches.append((vocab.UNTAPS, {
            "verb": verb,
            "defined": _p(params, "Defined") or None,
            "valid": _p(params, "ValidTgts") or _p(params, "ValidCards") or None,
            "amount": _p(params, "Amount") or None,
        }))
    if kind == "ability" and effect.ability_type in ("AB", "ST") and _has_tap_cost(cost):
        matches.append((vocab.TAPS_COST, {
            "cost": cost, "verb": verb, "ability_type": effect.ability_type,
        }))

    # -- copying / triggers -------------------------------------------------
    if verb == "CopyPermanent":
        matches.append((vocab.COPIES_CREATURE, {
            "valid": _p(params, "ValidTgts") or None,
            "defined": _p(params, "Defined") or None,
            "add_keywords": _p(params, "AddKeywords") or None,
            "at_eot": _p(params, "AtEOT") or None,
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
            matches.append((vocab.SACRIFICE_OUTLET, {
                "verb": verb,
                "valid": _p(params, "ValidCards") or _p(params, "ValidCard") or _p(params, "Defined") or None,
            }))
    if _sacrifices_self_from_cost(cost):
        matches.append((vocab.SACRIFICES_SELF, {"cost": cost}))
    elif _sacrifices_other_from_cost(cost):
        matches.append((vocab.SACRIFICE_OUTLET, {"cost": cost}))

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
            }))
    if verb in ("Exile", "ExileAll"):
        matches.append((vocab.EXILES, {"verb": verb}))
    if verb == "ChangeZone" and "PayLife" in cost and "TopOfLibrary" in _p(params, "Defined"):
        matches.append((vocab.CARDS_FROM_LIFE, {"cost": cost}))

    # -- combat / life / cards ---------------------------------------------
    if verb in ("DealDamage", "DamageAll", "DamageEach", "DamageMultiEffect"):
        matches.append((vocab.DEALS_DAMAGE, {
            "amount": _p(params, "NumDmg") or None,
            "defined": _p(params, "Defined") or None,
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
        matches.append((vocab.MILLS, {"amount": _p(params, "NumCards") or None}))
    if verb == "Token":
        matches.append((vocab.CREATES_TOKEN, {
            "script": _p(params, "TokenScript") or None,
            "amount": _p(params, "TokenAmount") or None,
            "treasure": _is_treasure(effect),
        }))
    if verb in ("GainControl", "GainControlVariant"):
        matches.append((vocab.GAINS_CONTROL, {"verb": verb}))
    if verb in ("PutCounter", "PutCounterAll", "PutCounterEach", "AddCounter"):
        matches.append((vocab.ADDS_COUNTERS, {
            "counter": _p(params, "CounterType") or None,
        }))
    if verb in ("Destroy", "DestroyAll", "DestroyAllEffect"):
        matches.append((vocab.DESTROYS, {"verb": verb}))

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
        for match in classify_effect(effect):
            record(match, effect)
            predicate = match[0]
            grouped[(effect.face_index, predicate)].evidence.append({
                "effect_id": effect.id,
                "line": effect.line_no,
                "kind": effect.effect_kind,
                "verb": effect.verb,
                "svar": effect.svar_name,
                "params": match[1],
            })

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
    "CardContext",
    "CardEffect",
    "CardPredicate",
    "classify_effect",
    "colors_from_mana_cost",
    "extract_card_predicates",
    "extract_import",
    "load_contexts",
    "mana_value",
    "parse_keywords",
]
