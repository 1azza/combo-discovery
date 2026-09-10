"""Controlled predicate vocabulary for the Layer-2 ontology.

A *predicate* is a typed, card-scoped observation derived from Forge script
effects.  Predicates are intentionally coarse (a card either does or does not
"produce mana") with the interesting detail kept in ``params_json``.

The matching itself lives in :mod:`combo_discovery.ontology.extractor`; this
module is the single source of truth for the vocabulary, the human-readable
description of every predicate, and the documented extraction rule (which
Forge verb / mode / parameter pattern yields it).  Keeping the rule text next
to the constant means the vocabulary and its documentation cannot drift.
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Predicate constants
# ---------------------------------------------------------------------------

PRODUCES_MANA = "PRODUCES_MANA"
UNTAPS = "UNTAPS"
TAPS_COST = "TAPS_COST"
COPIES_CREATURE = "COPIES_CREATURE"
COPIES_SPELL = "COPIES_SPELL"
ETB_TRIGGER = "ETB_TRIGGER"
DIES_TRIGGER = "DIES_TRIGGER"
SACRIFICE_OUTLET = "SACRIFICE_OUTLET"
SACRIFICES_SELF = "SACRIFICES_SELF"
MOVES_ZONE = "MOVES_ZONE"
DEALS_DAMAGE = "DEALS_DAMAGE"
GAINS_LIFE = "GAINS_LIFE"
LOSES_LIFE = "LOSES_LIFE"
DRAWS = "DRAWS"
DISCARDS = "DISCARDS"
CREATES_TOKEN = "CREATES_TOKEN"
GAINS_CONTROL = "GAINS_CONTROL"
REDUCES_COST = "REDUCES_COST"
ALTERNATIVE_COST = "ALTERNATIVE_COST"
CASTS_FROM_GRAVEYARD = "CASTS_FROM_GRAVEYARD"
STORM = "STORM"
ADDS_COUNTERS = "ADDS_COUNTERS"
DESTROYS = "DESTROYS"
EXILES = "EXILES"
RECURS_FROM_GRAVEYARD = "RECURS_FROM_GRAVEYARD"
MILLS = "MILLS"
SETS_COLOR = "SETS_COLOR"
CARDS_FROM_LIFE = "CARDS_FROM_LIFE"
TUTORS = "TUTORS"
OTHER = "OTHER"

#: Every predicate the extractor may emit, in a deterministic order.
ALL_PREDICATES: tuple[str, ...] = (
    PRODUCES_MANA,
    UNTAPS,
    TAPS_COST,
    COPIES_CREATURE,
    COPIES_SPELL,
    ETB_TRIGGER,
    DIES_TRIGGER,
    SACRIFICE_OUTLET,
    SACRIFICES_SELF,
    MOVES_ZONE,
    DEALS_DAMAGE,
    GAINS_LIFE,
    LOSES_LIFE,
    DRAWS,
    DISCARDS,
    CREATES_TOKEN,
    GAINS_CONTROL,
    REDUCES_COST,
    ALTERNATIVE_COST,
    CASTS_FROM_GRAVEYARD,
    STORM,
    ADDS_COUNTERS,
    DESTROYS,
    EXILES,
    RECURS_FROM_GRAVEYARD,
    MILLS,
    SETS_COLOR,
    CARDS_FROM_LIFE,
    TUTORS,
    OTHER,
)

PREDICATE_DESCRIPTIONS: dict[str, str] = {
    PRODUCES_MANA: "Adds mana (mana ability, ritual, or a Treasure-style token).",
    UNTAPS: "Untaps one or more permanents (Untap / TapOrUntap / UntapAll).",
    TAPS_COST: "Activated ability whose cost includes the tap symbol.",
    COPIES_CREATURE: "Creates a token copy of a permanent (CopyPermanent).",
    COPIES_SPELL: "Copies a spell on the stack (CopySpellAbility).",
    ETB_TRIGGER: "Enters-the-battlefield trigger (ChangesZone -> Battlefield, Self).",
    DIES_TRIGGER: "Death trigger (ChangesZone Battlefield -> Graveyard, Self).",
    SACRIFICE_OUTLET: "Can sacrifice permanents other than itself as a cost/effect.",
    SACRIFICES_SELF: "Sacrifices itself as a cost or effect.",
    MOVES_ZONE: "Moves cards between zones (ChangeZone / ChangeZoneAll).",
    DEALS_DAMAGE: "Deals damage to permanents or players.",
    GAINS_LIFE: "Gains life.",
    LOSES_LIFE: "Causes life loss.",
    DRAWS: "Draws cards.",
    DISCARDS: "Discards cards.",
    CREATES_TOKEN: "Creates creature/artifact tokens.",
    GAINS_CONTROL: "Gains control of a permanent.",
    REDUCES_COST: "Static cost reduction (ReduceCost).",
    ALTERNATIVE_COST: "Alternative/alternate casting cost (AlternativeCost).",
    CASTS_FROM_GRAVEYARD: "Lets you cast/play cards from the graveyard (Will, Escape).",
    STORM: "Has the Storm keyword (K:Storm).",
    ADDS_COUNTERS: "Puts counters on permanents/players.",
    DESTROYS: "Destroys permanents.",
    EXILES: "Exiles cards/permanents.",
    RECURS_FROM_GRAVEYARD: "Returns cards from the graveyard to hand/battlefield/library.",
    MILLS: "Puts cards from a library into the graveyard.",
    SETS_COLOR: "Sets or adds a colour to cards/permanents (AddColor).",
    CARDS_FROM_LIFE: "Converts life into cards (pay life, get a card).",
    TUTORS: "Searches/stack the library deterministically.",
    OTHER: "Recognised effect that no typed predicate matched (verb kept in params).",
}


@dataclass(frozen=True)
class PredicateRule:
    """Documentation pairing a predicate with the Forge pattern that yields it."""

    predicate: str
    rule: str


#: The extraction contract, mirrored by ``extractor.classify_effect``.  This is
#: deliberately explicit: it is the human-reviewable "ontology definition".
EXTRACTION_RULES: tuple[PredicateRule, ...] = (
    PredicateRule(PRODUCES_MANA, "verb='Mana' (Produced$/Amount$; X when Amount$ contains X); "
                                "or verb='Token' whose TokenScript is a Treasure."),
    PredicateRule(UNTAPS, "verb in {Untap, TapOrUntap, UntapAll} (Defined$/ValidTgts$/Amount$)."),
    PredicateRule(TAPS_COST, "activated ability (AB/ST) whose Cost$ contains the tap symbol T."),
    PredicateRule(COPIES_CREATURE, "verb='CopyPermanent' (ValidTgts$/Defined$/AddKeywords$/AtEOT$)."),
    PredicateRule(COPIES_SPELL, "verb in {CopySpellAbility, CopySpell, CopySpellMay}."),
    PredicateRule(ETB_TRIGGER, "trigger ChangesZone with Destination$=Battlefield and ValidCard$ containing Self."),
    PredicateRule(DIES_TRIGGER, "trigger ChangesZone Origin$=Battlefield Destination$=Graveyard ValidCard$ Self."),
    PredicateRule(SACRIFICE_OUTLET, "verb in {Sacrifice, SacrificeAll} not restricted to Self, "
                                    "or Cost$ containing Sac<...> of another permanent."),
    PredicateRule(SACRIFICES_SELF, "verb Sacrifice* with SacValid$/Defined$=Self, or Cost$ Sac<...CARDNAME>."),
    PredicateRule(MOVES_ZONE, "verb in {ChangeZone, ChangeZoneAll} (Origin$/Destination$)."),
    PredicateRule(DEALS_DAMAGE, "verb in {DealDamage, DamageAll, DamageEach, DamageMultiEffect}."),
    PredicateRule(GAINS_LIFE, "verb='GainLife'."),
    PredicateRule(LOSES_LIFE, "verb='LoseLife'."),
    PredicateRule(DRAWS, "verb in {Draw, DrawAll, DrawCards}."),
    PredicateRule(DISCARDS, "verb in {Discard, DiscardAll, DiscardEach}."),
    PredicateRule(CREATES_TOKEN, "verb='Token'."),
    PredicateRule(GAINS_CONTROL, "verb in {GainControl, GainControlVariant}."),
    PredicateRule(REDUCES_COST, "static mode ReduceCost."),
    PredicateRule(ALTERNATIVE_COST, "static mode AlternativeCost."),
    PredicateRule(CASTS_FROM_GRAVEYARD, "verb='Effect' whose text says cast/play from graveyard; "
                                        "or static Continuous with AddKeyword$ containing Escape."),
    PredicateRule(STORM, "card keyword K:Storm (from the raw script)."),
    PredicateRule(ADDS_COUNTERS, "verb in {PutCounter, PutCounterAll, PutCounterEach, AddCounter}."),
    PredicateRule(DESTROYS, "verb in {Destroy, DestroyAll, DestroyAllEffect}."),
    PredicateRule(EXILES, "verb Exile* or ChangeZone with Destination$=Exile."),
    PredicateRule(RECURS_FROM_GRAVEYARD, "ChangeZone* with Origin$ containing Graveyard and "
                                         "Destination$ in {Hand, Battlefield, Library}."),
    PredicateRule(MILLS, "verb in {Mill, MillAll}."),
    PredicateRule(SETS_COLOR, "static Continuous with AddColor$."),
    PredicateRule(CARDS_FROM_LIFE, "ChangeZone with Cost$ PayLife<...> and Defined$ TopOfLibrary."),
    PredicateRule(TUTORS, "verb in {RearrangeTopOfLibrary, ChangeZone} searching the library to library/hand."),
    PredicateRule(OTHER, "fallback for a typed effect that matched no rule above."),
)

RULE_BY_PREDICATE: dict[str, str] = {rule.predicate: rule.rule for rule in EXTRACTION_RULES}


def describe(predicate: str) -> str:
    """Return the documented extraction rule for ``predicate`` (or '')."""
    return RULE_BY_PREDICATE.get(predicate, "")


__all__ = [
    "ADDS_COUNTERS",
    "ALL_PREDICATES",
    "ALTERNATIVE_COST",
    "CARDS_FROM_LIFE",
    "CASTS_FROM_GRAVEYARD",
    "COPIES_CREATURE",
    "COPIES_SPELL",
    "CREATES_TOKEN",
    "DEALS_DAMAGE",
    "DESTROYS",
    "DIES_TRIGGER",
    "DISCARDS",
    "DRAWS",
    "ETB_TRIGGER",
    "EXILES",
    "EXTRACTION_RULES",
    "GAINS_CONTROL",
    "GAINS_LIFE",
    "LOSES_LIFE",
    "MILLS",
    "MOVES_ZONE",
    "OTHER",
    "PREDICATE_DESCRIPTIONS",
    "PRODUCES_MANA",
    "RECURS_FROM_GRAVEYARD",
    "REDUCES_COST",
    "RULE_BY_PREDICATE",
    "SACRIFICE_OUTLET",
    "SACRIFICES_SELF",
    "SETS_COLOR",
    "STORM",
    "TAPS_COST",
    "TUTORS",
    "UNTAPS",
    "PredicateRule",
    "describe",
]
