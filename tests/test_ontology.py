"""Predicate ontology + interaction-edge tests.

Two layers:

* a hermetic mini-corpus built from the *real* Forge scripts in
  ``tests/fixtures/ontology_cards`` (copied into a synthetic cardsfolder), used
  to validate predicate extraction and the known-combo recall set deterministically;
* an optional performance sanity check over the real ``research.db`` corpus
  (read-only), skipped when that database is absent (e.g. CI).
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
from pathlib import Path

import pytest

from combo_discovery.corpus.importer import import_corpus, normalize_name
from combo_discovery.ontology import vocabulary as vocab
from combo_discovery.ontology.builder import build_ontology
from combo_discovery.ontology.edges import (
    PATTERNS,
    CardView,
    build_edges,
    check_compatibility,
    engine_can_copy,
    restriction_matches_card,
)
from combo_discovery.ontology.extractor import (
    CardContext,
    CardEffect,
    CardPredicate,
    card_subtypes,
    card_type_tokens,
    classify_effect,
    extract_card_predicates,
    load_contexts,
    mana_value,
    parse_restriction,
)
from combo_discovery.ontology.queries import QUERIES
from combo_discovery.store import ExperimentStore, _migration_3

FIXTURES = Path(__file__).parent / "fixtures" / "ontology_cards"
REPO_ROOT = Path(__file__).resolve().parent.parent
RESEARCH_DB = REPO_ROOT / "research.db"
FORGE_CARDSFOLDER = Path("/home/lza/Work/forge/forge-gui/res/cardsfolder")

# ---------------------------------------------------------------------------
# The known-combo validation set (data, with reasons)
# ---------------------------------------------------------------------------

#: Required: the classic Kiki-Jiki / Splinter Twin loops must be rediscovered.
REQUIRED_COMBOS = [
    {"cards": ("Kiki-Jiki, Mirror Breaker", "Deceiver Exarch"), "pattern": "infinite_etb_loop",
     "reason": "Kiki taps to copy Exarch; Exarch's ETB untaps Kiki for infinite tokens."},
    {"cards": ("Kiki-Jiki, Mirror Breaker", "Pestermite"), "pattern": "infinite_etb_loop",
     "reason": "Same loop with Pestermite's ETB tap-or-untap."},
    {"cards": ("Splinter Twin", "Deceiver Exarch"), "pattern": "infinite_etb_loop",
     "reason": "Splinter Twin grants the tap-to-copy ability to the enchanted Exarch."},
    {"cards": ("Splinter Twin", "Pestermite"), "pattern": "infinite_etb_loop",
     "reason": "Splinter Twin + Pestermite — the other half of the classic twin deck."},
]

#: 12 known interactions; the graph must find at least 8.  ``expected`` False
#: marks a pair the v1 predicate set deliberately does not model (Doomsday).
KNOWN_COMBOS = [
    {"cards": ("Tolarian Academy", "Lion's Eye Diamond"), "pattern": "mana_engine",
     "reason": "Academy scales with artifacts; LED is an artifact mana source.", "expected": True},
    {"cards": ("Yawgmoth's Will", "Lion's Eye Diamond"), "pattern": "free_cast_loops",
     "reason": "Will casts spells from the graveyard; LED pays for them by sacrificing itself.",
     "expected": True},
    {"cards": ("Underworld Breach", "Lion's Eye Diamond"), "pattern": "free_cast_loops",
     "reason": "Breach grants escape; LED fuels repeated recasts.", "expected": True},
    {"cards": ("Goblin Welder", "Myr Retriever"), "pattern": "sacrifice_recursion",
     "reason": "Welder sacrifices/returns artifacts; Myr Retriever recurs artifacts when it dies.",
     "expected": True},
    {"cards": ("Doomsday", "Lion's Eye Diamond"), "pattern": "mana_engine",
     "reason": "Doomsday stack + LED is a known line but not modelled by v1 predicates.",
     "expected": False},
    {"cards": ("Tendrils of Agony", "Time Spiral"), "pattern": "storm_engine",
     "reason": "Time Spiral untaps lands and refuels; Tendrils is the storm payoff.",
     "expected": True},
    {"cards": ("Painter's Servant", "Grindstone"), "pattern": "color_lock_mill",
     "reason": "Painter's makes all cards one colour; Grindstone mills and repeats.",
     "expected": True},
    {"cards": ("Necropotence", "Brainstorm"), "pattern": "draw_engine",
     "reason": "Necropotence converts life into cards; Brainstorm turns cards into value.",
     "expected": True},
    # Infinite-loop slot.  The previous entry here was Deceiver Exarch + Grindstone,
    # but Grindstone has no COPIES_CREATURE predicate, so under the round-2 hard
    # gate (the engine must re-trigger the partner's ETB by copying it) that pair
    # is correctly out of scope for infinite_etb_loop.  Replaced with another
    # genuine copy-engine loop to keep this validation set at 11/12.
    {"cards": ("Kiki-Jiki, Mirror Breaker", "Village Bell-Ringer"), "pattern": "infinite_etb_loop",
     "reason": "Kiki copies Village Bell-Ringer; its ETB untaps all your creatures, untapping Kiki.",
     "expected": True},
    {"cards": ("Dockside Extortionist", "Walking Ballista"), "pattern": "mana_engine",
     "reason": "Dockside makes Treasure; Ballista is an X-cost mana sink.", "expected": True},
    {"cards": ("Tendrils of Agony", "Rite of Flame"), "pattern": "storm_engine",
     "reason": "Rite of Flame is a cheap storm enabler.", "expected": True},
    {"cards": ("Priest of Gix", "Walking Ballista"), "pattern": "mana_engine",
     "reason": "Priest of Gix's ETB ritual pays for an X-cost payoff.", "expected": True},
]

#: Round-1 false positives (type/controller mismatch) — must stay gone.
KIKI_TYPE_FALSE_POSITIVES = [
    "Bumi, Unleashed",            # UntapAll Land.YouCtrl
    "Zacama, Primal Calamity",    # UntapAll Land.YouCtrl
    "Cloud of Faeries",           # UntapType$ Land
    "Palinchron",                 # UntapType$ Land
    "Peregrine Drake",            # UntapType$ Land
    "Great Whale",                # UntapType$ Land
    "Nissa, Vastwood Seer",       # untaps Land
    "Nissa, Who Shakes the World",  # Defined$ Targeted, no known type
    "Woodcaller Automaton",       # Land.YouCtrl
    "Goatnapper",                 # subtype Goat only
    "Godo, Bandit Warlord",       # Card.Self,Samurai.YouCtrl
    "Deepway Navigator",          # Merfolk.Other+YouCtrl
    "Great Oak Guardian",         # Creature.TargetedPlayerCtrl
    "Howlpack Piper",             # Defined$ Self
    "Jokulmorder",                # Defined$ Self
    "Xolatoyac, the Smiling Flood",  # Permanent.YouCtrl+HasCounters
]

#: Round-2 false positives (card-scoped conflation / no copy re-trigger / gates).
KIKI_ENGINE_FALSE_POSITIVES = [
    "Fear of Missing Out",        # untap on Attacks + FirstAttack/Delirium, not the ETB
    "Formidable Speaker",         # untap on a separate activated ability (1 T)
    "All-Out Assault",            # not a creature: Kiki cannot copy it
    "Dee Kay, Finder of the Lost",  # untap gated on RolledDie/ValidResult; legendary
    "Derevi, Empyrial Tactician",   # legendary: Kiki's copy restriction excludes it
    "Flash Thompson, Spider-Fan",   # legendary
    "Grim Reaper's Sprint",         # not a creature
    "Invasion of Segovia",          # not a creature; untap on the back face
    "Inverted Iceberg",             # not a creature; untap on the back face
    "Out of Time",                  # not a creature
    "Sewer-veillance Cam",          # not a creature
]

#: All negatives the gate must reject (both rounds).
KIKI_FALSE_POSITIVES = KIKI_TYPE_FALSE_POSITIVES + KIKI_ENGINE_FALSE_POSITIVES

#: True positives: part of the card's repeatable self-ETB engine, target the
#: engine legally, and are copyable by Kiki.  Must survive every gate.
KIKI_TRUE_POSITIVES = [
    "Deceiver Exarch", "Pestermite", "Bounding Krasis", "Corridor Monitor",
    "Village Bell-Ringer", "Sky Hussar", "Hyrax Tower Scout", "Sparring Mummy",
    "Breaching Hippocamp", "Little Bear", "White Plume Adventurer",
    "Eager Beaver", "Glamermite", "Janjeet Sentry",
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _seed_cardsfolder(root: Path) -> Path:
    """Copy real scripts into ``root/forge-gui/res/cardsfolder``.

    Prefers the live Forge checkout (so the test is exercising the real
    cardsfolder) and falls back to the vendored fixtures, keeping the suite
    hermetic on machines without Forge.
    """
    folder = root / "forge-gui" / "res" / "cardsfolder"
    for source_fixture in sorted(FIXTURES.glob("*.txt")):
        real = FORGE_CARDSFOLDER / source_fixture.name[0] / source_fixture.name
        source = real if real.is_file() else source_fixture
        target = folder / source_fixture.name[0] / source_fixture.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    return root


@pytest.fixture
def ontology_db(tmp_path: Path) -> tuple[Path, dict[str, int]]:
    root = _seed_cardsfolder(tmp_path)
    db = tmp_path / "research.db"
    store = ExperimentStore(db)
    import_corpus(store, root)
    report = build_ontology(store, apply_caps=False, mode="legacy")
    assert report.interactions_total > 0
    store.close()

    name_to_id: dict[str, int] = {}
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    for row in conn.execute("SELECT id, name FROM cards"):
        name_to_id[normalize_name(row["name"])] = int(row["id"])
    conn.close()
    return db, name_to_id


def _query(db: Path, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _edge_exists(db: Path, a: str, b: str, pattern: str) -> bool:
    return _interaction(db, a, b, pattern) is not None


def _card_exists(db: Path, name: str) -> bool:
    return bool(_query(db, "SELECT 1 FROM cards WHERE normalized_name = ?",
                       (normalize_name(name),)))


def _interaction(db: Path, a: str, b: str, pattern: str) -> sqlite3.Row | None:
    normalized = {normalize_name(a), normalize_name(b)}
    rows = _query(
        db,
        "SELECT p.name AS pattern, i.direction, i.mechanism, i.score, "
        "i.evidence_json, s.normalized_name AS src, t.normalized_name AS tgt, "
        "s.name AS src_name, t.name AS tgt_name FROM interactions i "
        "JOIN patterns p ON p.id = i.pattern_id "
        "JOIN cards s ON s.id = i.source_card_id "
        "JOIN cards t ON t.id = i.target_card_id WHERE p.name = ?",
        (pattern,),
    )
    for row in rows:
        if {row["src"], row["tgt"]} == normalized:
            return row
    return None


# ---------------------------------------------------------------------------
# Vocabulary + extractor unit tests
# ---------------------------------------------------------------------------


class TestVocabulary:
    def test_every_predicate_has_a_rule_and_description(self):
        for predicate in vocab.ALL_PREDICATES:
            assert vocab.describe(predicate), predicate
            assert predicate in vocab.PREDICATE_DESCRIPTIONS

    def test_mana_value(self):
        assert mana_value("2 R R R") == 5
        assert mana_value("X X") == 0
        assert mana_value("no cost") == 0
        assert mana_value("1 G") == 2


class TestPatternRegistry:
    """The pattern registry isolates each pattern and exposes its metadata."""

    def test_registry_exposes_every_pattern_uniquely(self):
        from combo_discovery.ontology.patterns import PATTERNS, get_pattern, iter_patterns

        names = [pattern.name for pattern in iter_patterns()]
        assert names == [
            "infinite_etb_loop", "sacrifice_recursion", "mana_engine",
            "free_cast_loops", "storm_engine", "color_lock_mill", "draw_engine",
        ]
        assert len(names) == len(set(names)) == 7
        assert [pattern.name for pattern in PATTERNS] == names
        for pattern in PATTERNS:
            assert pattern.description
            assert isinstance(pattern.rule, dict) and pattern.rule
            assert callable(pattern.matcher)
            assert callable(pattern.scorer)
            assert get_pattern(pattern.name) is pattern

    def test_registry_matchers_reachable_and_countable(self):
        from combo_discovery.ontology import edges
        from combo_discovery.ontology.patterns import PATTERNS, get_pattern

        for pattern in PATTERNS:
            assert list(pattern.match({})) == []
            assert pattern.count({}) == 0
            # Every matcher is reachable by its historical module-level name.
            assert getattr(edges, pattern.name) is pattern.matcher
        assert get_pattern("infinite_etb_loop").matcher is edges.infinite_etb_loop
        # The facade re-exports the same registry object.
        assert edges.PATTERNS is PATTERNS


class TestExtractorRules:
    @staticmethod
    def _effect(verb, **params):
        return CardEffect(id=1, card_id=1, face_index=0, effect_kind="ability",
                          verb=verb, ability_type="AB", params=params)

    def test_mana_and_tap_cost(self):
        ctx = CardContext(1, "X", "x", "1 U", "Artifact", "", "", ())
        effect = self._effect("Mana", Produced="U", Amount="2", Cost="T")
        predicates = {p for p, _ in classify_effect(effect)}
        assert {vocab.PRODUCES_MANA, vocab.TAPS_COST} <= predicates

    def test_etb_and_dies_trigger(self):
        etb = CardEffect(1, 1, 0, "trigger", "ChangesZone",
                         params={"Destination": "Battlefield", "ValidCard": "Card.Self"})
        dies = CardEffect(2, 1, 0, "trigger", "ChangesZone",
                          params={"Origin": "Battlefield", "Destination": "Graveyard",
                                  "ValidCard": "Card.Self"})
        assert vocab.ETB_TRIGGER in {p for p, _ in classify_effect(etb)}
        assert vocab.DIES_TRIGGER in {p for p, _ in classify_effect(dies)}

    def test_self_sacrifice_vs_outlet(self):
        self_sac = self._effect("Sacrifice", SacValid="Self")
        outlet = self._effect("SacrificeAll", ValidCards="Card.IsRemembered")
        assert vocab.SACRIFICES_SELF in {p for p, _ in classify_effect(self_sac)}
        assert vocab.SACRIFICE_OUTLET in {p for p, _ in classify_effect(outlet)}

    def test_copy_and_untap(self):
        copy = self._effect("CopyPermanent", Cost="T", ValidTgts="Creature.nonLegendary")
        untap = self._effect("Untap", ValidTgts="Permanent.YouCtrl")
        assert vocab.COPIES_CREATURE in {p for p, _ in classify_effect(copy)}
        assert vocab.UNTAPS in {p for p, _ in classify_effect(untap)}

    def test_unknown_effect_becomes_other(self):
        matches = classify_effect(self._effect("SomeFutureVerb"))
        assert matches and matches[0][0] == vocab.OTHER

    def test_deduplicates_by_face_and_predicate(self):
        ctx = CardContext(1, "X", "x", "1 U", "Artifact", "", "", ())
        effects = [
            CardEffect(1, 1, 0, "ability", "Mana", params={"Produced": "U", "Amount": "1"}),
            CardEffect(2, 1, 0, "ability", "Mana", params={"Produced": "R", "Amount": "1"}),
        ]
        predicates = extract_card_predicates(ctx, effects)
        mana = [p for p in predicates if p.predicate == vocab.PRODUCES_MANA]
        assert len(mana) == 1
        assert len(mana[0].evidence) == 2


class TestRestrictionParsing:
    """Forge restriction strings -> structured type/controller/rider data."""

    def test_land_youctrl(self):
        spec = parse_restriction("Land.YouCtrl")
        assert spec.unknown is False
        alt = spec.alternatives[0]
        assert alt.types == frozenset({"LAND"})
        assert alt.controller == "you"
        assert alt.riders == ()

    def test_creature_other_youctrl(self):
        spec = parse_restriction("Creature.Other+YouCtrl")
        alt = spec.alternatives[0]
        assert alt.types == frozenset({"CREATURE"})
        assert alt.controller == "you"
        assert alt.other is True

    def test_card_self_is_self_only(self):
        spec = parse_restriction("Card.Self")
        assert spec.self_only is True
        assert spec.alternatives[0].self_only is True

    def test_comma_list_alternatives(self):
        spec = parse_restriction("Artifact.YouCtrl,Creature.YouCtrl")
        assert len(spec.alternatives) == 2
        assert spec.alternatives[0].types == frozenset({"ARTIFACT"})
        assert spec.alternatives[1].types == frozenset({"CREATURE"})

    def test_counter_rider_is_recorded(self):
        spec = parse_restriction("Permanent.YouCtrl+HasCounters")
        assert spec.alternatives[0].types == frozenset({"PERMANENT"})
        assert "hascounters" in spec.alternatives[0].riders

    def test_unknown_restriction(self):
        spec = parse_restriction("")
        assert spec.unknown is True
        assert spec.alternatives == ()

    def test_subtype_only_restriction(self):
        spec = parse_restriction("Goat")
        assert spec.alternatives[0].types == frozenset()
        assert spec.alternatives[0].subtypes == frozenset({"goat"})

    def test_untap_type_is_captured(self):
        effect = CardEffect(
            id=1, card_id=1, face_index=0, effect_kind="ability", verb="Untap",
            ability_type="DB",
            params={"Amount": "2", "UntapType": "Land", "UntapUpTo": "True"},
        )
        matches = {predicate: params for predicate, params in classify_effect(effect)}
        target = parse_restriction(matches[vocab.UNTAPS]["valid"])
        assert matches[vocab.UNTAPS]["valid"] == "Land"
        assert target.alternatives[0].types == frozenset({"LAND"})

    def test_card_type_and_subtype_tokens(self):
        assert card_type_tokens("Legendary Creature Human Noble Ally") == frozenset({"CREATURE"})
        assert card_subtypes("Legendary Creature Human Noble Ally") == frozenset(
            {"human", "noble", "ally"}
        )
        assert "artifact" in {t.lower() for t in card_type_tokens("Artifact Creature Construct")}


class TestRestrictionCompatibility:
    """``restriction_matches_card`` against a Kiki-shaped engine card."""

    KIKI = CardContext(
        1, "Kiki-Jiki, Mirror Breaker", "kiki", "2 R R R",
        "Legendary Creature Goblin Shaman", "", "", (),
    )

    @pytest.mark.parametrize(
        ("restriction", "expected"),
        [
            ("Land.YouCtrl", "incompatible"),
            ("Goat", "incompatible"),
            ("Creature.TargetedPlayerCtrl", "incompatible"),
            ("Permanent.YouCtrl+HasCounters", "incompatible"),
            ("Card.Self", "incompatible"),
            ("Card.Self,Samurai.YouCtrl", "incompatible"),
            ("Permanent", "compatible"),
            ("Permanent.YouCtrl", "compatible"),
            ("Creature.Other+YouCtrl", "compatible"),
            ("Artifact.YouCtrl,Creature.YouCtrl", "compatible"),
            ("", "unknown"),
        ],
    )
    def test_restriction_against_kiki(self, restriction, expected):
        result = restriction_matches_card(parse_restriction(restriction), self.KIKI)
        assert result.status == expected

    def test_engine_copy_restriction(self):
        engine = _synthetic_view(1, "Engine", "Legendary Creature", ["COPIES_CREATURE"],
                                 {vocab.COPIES_CREATURE: {"copy": parse_restriction(
                                     "Creature.nonLegendary+YouCtrl").to_dict()}})
        legendary = _synthetic_view(2, "Legendary Buddy", "Legendary Creature Human", [])
        plain = _synthetic_view(3, "Plain Buddy", "Creature Human", [])
        assert engine_can_copy(engine, plain).status == "compatible"
        assert engine_can_copy(engine, legendary).status == "incompatible"


def _synthetic_view(card_id, name, type_line, predicates, params=None):
    params = params or {}
    ctx = CardContext(card_id, name, normalize_name(name), "1", type_line, "", "", ())
    preds = [
        CardPredicate(card_id=card_id, face_index=0, predicate=p, params=dict(params.get(p, {})))
        for p in predicates
    ]
    return CardView.build(ctx, preds)


def _ability_detail(ref, root, *, is_root=False, root_kind="trigger", root_cost="",
                    gates=None, via=()):
    gates = dict(gates or {})
    return {
        "ability_ref": ref, "root_ability_ref": root, "via": list(via),
        "is_root": is_root, "root_kind": root_kind, "root_verb": "",
        "root_cost": root_cost, "gates": gates, "chain_gates": gates,
    }


def _attach(params: dict, detail: dict) -> dict:
    merged = dict(params)
    merged["ability_ref"] = detail["ability_ref"]
    merged["root_ability_ref"] = detail["root_ability_ref"]
    merged["ability_refs"] = [detail["ability_ref"]]
    merged["root_ability_refs"] = [detail["root_ability_ref"]]
    merged["ability_details"] = [detail]
    return merged


_ENGINE_REF = "line:0:6:A:CopyPermanent"
_ETB_REF = "line:0:5:T:ChangesZone"


def _engine_view(card_id=1, name="Engine", type_line="Legendary Creature",
                 copy_restriction="Creature.nonLegendary+YouCtrl"):
    ability = _ability_detail(_ENGINE_REF, _ENGINE_REF, is_root=True,
                              root_kind="ability", root_cost="T")
    return _synthetic_view(card_id, name, type_line,
                           [vocab.TAPS_COST, vocab.COPIES_CREATURE],
                           {
                               vocab.TAPS_COST: _attach({"verb": "CopyPermanent"}, ability),
                               vocab.COPIES_CREATURE: _attach(
                                   {"copy": parse_restriction(copy_restriction).to_dict()}, ability),
                           })


def _untapper_view(card_id=2, name="Partner", type_line="Creature",
                   target="Permanent.YouCtrl", verb="Untap",
                   untap_root=_ETB_REF, root_kind="trigger", root_cost="",
                   gates=None, with_etb=True):
    untap_ability = _ability_detail(
        "svar:0:DBUntap:Untap", untap_root, root_kind=root_kind,
        root_cost=root_cost, gates=gates, via=("DBUntap",),
    )
    preds = [vocab.UNTAPS]
    params = {vocab.UNTAPS: _attach(
        {"verb": verb, "target": parse_restriction(target).to_dict()}, untap_ability)}
    if with_etb:
        etb_ability = _ability_detail(_ETB_REF, _ETB_REF, is_root=True, root_kind="trigger")
        preds.append(vocab.ETB_TRIGGER)
        params[vocab.ETB_TRIGGER] = _attach({}, etb_ability)
    return _synthetic_view(card_id, name, type_line, preds, params)


class TestAbilityLinking:
    """``build_ability_links`` follows Execute$/SubAbility$ chains per effect."""

    @staticmethod
    def _effect(effect_id, kind, verb, *, is_svar=False, svar=None, line=5, params=None):
        return CardEffect(id=effect_id, card_id=1, face_index=0, effect_kind=kind,
                          verb=verb, is_svar=is_svar, svar_name=svar, line_no=line,
                          params=params or {})

    def test_execute_subability_chain(self):
        from combo_discovery.ontology.extractor import build_ability_links

        root = self._effect(1, "trigger", "ChangesZone", line=5, params={"Execute": "TrigUntap"})
        first = self._effect(2, "ability", "Untap", is_svar=True, svar="TrigUntap", line=6,
                             params={"SubAbility": "DBExtra", "ValidTgts": "Permanent.YouCtrl"})
        second = self._effect(3, "ability", "PutCounter", is_svar=True, svar="DBExtra", line=7)
        links = build_ability_links([root, first, second])

        assert links[2]["root_ability_ref"] == links[1]["ability_ref"]
        assert links[2]["via"] == ["TrigUntap"]
        assert links[3]["root_ability_ref"] == links[1]["ability_ref"]
        assert links[3]["via"] == ["TrigUntap", "DBExtra"]
        assert links[1]["is_root"] is True

    def test_separate_ability_is_not_linked(self):
        from combo_discovery.ontology.extractor import build_ability_links

        etb = self._effect(1, "trigger", "ChangesZone", line=5, params={"Execute": "TrigDraw"})
        draw = self._effect(2, "ability", "Draw", is_svar=True, svar="TrigDraw", line=6)
        attacks = self._effect(3, "trigger", "Attacks", line=8, params={"Execute": "TrigUntap"})
        untap = self._effect(4, "ability", "Untap", is_svar=True, svar="TrigUntap", line=9,
                             params={"ValidTgts": "Creature"})
        links = build_ability_links([etb, draw, attacks, untap])
        assert links[4]["root_ability_ref"] == links[3]["ability_ref"] != links[1]["ability_ref"]

    def test_choices_chain_is_followed(self):
        from combo_discovery.ontology.extractor import build_ability_links

        root = self._effect(1, "trigger", "ChangesZone", line=6, params={"Execute": "TrigCharm"})
        charm = self._effect(2, "ability", "Charm", is_svar=True, svar="TrigCharm", line=7,
                             params={"Choices": "DBUntap,DBTap"})
        untap = self._effect(3, "ability", "Untap", is_svar=True, svar="DBUntap", line=8)
        links = build_ability_links([root, charm, untap])
        assert links[3]["root_ability_ref"] == links[1]["ability_ref"]
        assert links[3]["via"] == ["TrigCharm", "DBUntap"]

    def test_gates_propagate_down_the_chain(self):
        from combo_discovery.ontology.extractor import build_ability_links

        root = self._effect(1, "trigger", "Attacks", line=8,
                            params={"Execute": "TrigUntap", "FirstAttack": "True", "Delirium": "True"})
        untap = self._effect(2, "ability", "Untap", is_svar=True, svar="TrigUntap", line=9)
        links = build_ability_links([root, untap])
        assert links[2]["chain_gates"] == {"first_attack": "True", "delirium": "True"}

    def test_effect_gates_capture_conditional_keys(self):
        from combo_discovery.ontology.extractor import effect_gates

        effect = self._effect(1, "trigger", "Attacks", params={
            "FirstAttack": "True", "Delirium": "True", "ActivationLimit": "1",
            "ValidResult": "4", "PlayerTurn": "True",
        })
        gates = effect_gates(effect)
        assert {"first_attack", "delirium", "activation_limit", "valid_result",
                "turn_restriction"} <= set(gates)
        rolled = self._effect(2, "trigger", "RolledDie", params={"ValidResult": "4"})
        assert effect_gates(rolled).get("rolled_die") == "4"

    def test_effect_ref_is_stable(self):
        from combo_discovery.ontology.extractor import effect_ref

        direct = self._effect(1, "trigger", "ChangesZone", line=6)
        svar = self._effect(2, "ability", "Untap", is_svar=True, svar="DBUntap", line=8)
        assert effect_ref(direct) == "line:0:6:T:ChangesZone"
        assert effect_ref(svar) == "svar:0:DBUntap:Untap"


class TestGateRejections:
    @pytest.mark.parametrize(
        "gates",
        [
            {"first_attack": "True"},
            {"delirium": "True"},
            {"rolled_die": "4"},
            {"activation_limit": "1"},
            {"turn_restriction": "True"},
            {"phase_out": "True"},
        ],
    )
    def test_gated_untap_is_not_linked(self, gates):
        from combo_discovery.ontology.edges import check_ability_link

        partner = _untapper_view(2, "Gated", "Creature", untap_root=_ETB_REF,
                                 gates=gates)
        assert check_ability_link(partner).linked is False

    def test_ungated_etb_untap_is_linked(self):
        from combo_discovery.ontology.edges import check_ability_link

        partner = _untapper_view(2, "Clean", "Creature", untap_root=_ETB_REF)
        link = check_ability_link(partner)
        assert link.linked is True and link.kind == "etb_chain"


class TestPatternAbilityScope:
    def test_free_cast_enabler_needs_same_ability(self):
        from combo_discovery.ontology.edges import free_cast_loops

        caster = _synthetic_view(1, "Caster", "Sorcery", [vocab.CASTS_FROM_GRAVEYARD])
        mana = _ability_detail("line:0:5:A:Mana", "line:0:5:A:Mana",
                               is_root=True, root_kind="ability")
        sac = _ability_detail("line:0:6:A:Sacrifice", "line:0:6:A:Sacrifice",
                              is_root=True, root_kind="ability")
        split = _synthetic_view(2, "Split Enabler", "Artifact",
                                [vocab.PRODUCES_MANA, vocab.SACRIFICES_SELF],
                                {vocab.PRODUCES_MANA: _attach({}, mana),
                                 vocab.SACRIFICES_SELF: _attach({}, sac)})
        assert list(free_cast_loops({1: caster, 2: split})) == []

        same = _synthetic_view(3, "Same Enabler", "Artifact",
                               [vocab.PRODUCES_MANA, vocab.SACRIFICES_SELF],
                               {vocab.PRODUCES_MANA: _attach({}, mana),
                                vocab.SACRIFICES_SELF: _attach({}, mana)})
        assert len(list(free_cast_loops({1: caster, 3: same}))) == 1

    def test_mana_producer_needs_etb_or_tap_link(self):
        from combo_discovery.ontology.edges import mana_engine

        etb = _ability_detail(_ETB_REF, _ETB_REF, is_root=True, root_kind="trigger")
        other = _ability_detail("line:0:9:A:Mana", "line:0:9:A:Mana",
                                is_root=True, root_kind="ability", root_cost="T")
        linked = _synthetic_view(
            1, "Linked Producer", "Creature", [vocab.ETB_TRIGGER, vocab.PRODUCES_MANA],
            {vocab.ETB_TRIGGER: _attach({}, etb),
             vocab.PRODUCES_MANA: _attach({"amount": "3"}, etb)},
        )
        split = _synthetic_view(
            2, "Split Producer", "Creature", [vocab.ETB_TRIGGER, vocab.PRODUCES_MANA],
            {vocab.ETB_TRIGGER: _attach({}, etb),
             vocab.PRODUCES_MANA: _attach({"amount": "3"}, other)},
        )
        spender = CardView.build(
            CardContext(3, "Spender", "spender", "X", "Sorcery", "", "", ()), []
        )
        assert len(list(mana_engine({1: linked, 3: spender}))) == 1
        assert list(mana_engine({2: split, 3: spender})) == []


class TestInfiniteLoopScoring:
    def test_noncreature_uncopyable_untapper_is_excluded(self):
        """A non-creature ETB untapper cannot be copied by a creature-copy engine."""
        from combo_discovery.ontology.edges import infinite_etb_loop

        engine = _engine_view(1, "Engine", "Legendary Creature")
        creature = _untapper_view(2, "Creature Untapper", "Creature")
        aura = _untapper_view(3, "Aura Untapper", "Enchantment Aura")
        targets = {
            edge.target.name for edge in infinite_etb_loop({1: engine, 2: creature, 3: aura})
        }
        assert targets == {"Creature Untapper"}

    def test_incompatible_untapper_is_excluded(self):
        from combo_discovery.ontology.edges import infinite_etb_loop

        engine = _engine_view()
        land_untapper = _untapper_view(2, "Land Untapper", "Creature",
                                       target="Land.YouCtrl", verb="UntapAll")
        assert list(infinite_etb_loop({1: engine, 2: land_untapper})) == []

    def test_self_only_untapper_is_excluded(self):
        from combo_discovery.ontology.edges import infinite_etb_loop

        engine = _engine_view()
        self_untapper = _untapper_view(2, "Self Untapper", "Creature", target="Card.Self")
        assert list(infinite_etb_loop({1: engine, 2: self_untapper})) == []

    def test_gated_different_ability_is_excluded(self):
        """Fear-of-Missing-Out shape: the untap is on a gated Attacks trigger,
        not the self-ETB engine."""
        from combo_discovery.ontology.edges import infinite_etb_loop

        engine = _engine_view()
        partner = _untapper_view(
            2, "Split Ability", "Creature", untap_root="line:0:8:T:Attacks",
            gates={"first_attack": "True", "delirium": "True"},
        )
        assert list(infinite_etb_loop({1: engine, 2: partner})) == []

    def test_recurring_trigger_untap_is_linked(self):
        """White-Plume shape: an ungated recurring trigger untaps the engine."""
        from combo_discovery.ontology.edges import infinite_etb_loop

        engine = _engine_view()
        partner = _untapper_view(2, "Upkeep Untapper", "Creature",
                                 untap_root="line:0:7:T:Phase")
        edges = list(infinite_etb_loop({1: engine, 2: partner}))
        assert len(edges) == 1
        marker = next(e for e in edges[0].evidence if e.get("kind") == "compatibility")
        assert marker["link_kind"] == "repeatable_trigger"

    def test_activated_untap_without_etb_resource_is_excluded(self):
        """Formidable-Speaker shape: untap is an activated mana-cost ability."""
        from combo_discovery.ontology.edges import infinite_etb_loop

        engine = _engine_view()
        partner = _untapper_view(
            2, "Activated Untap", "Creature", target="Permanent.Other",
            untap_root="line:0:7:A:Untap", root_kind="ability", root_cost="1 T",
        )
        assert list(infinite_etb_loop({1: engine, 2: partner})) == []

    def test_activated_untap_with_etb_energy_resource_is_linked(self):
        """Janjeet-Sentry shape: ETB gives energy, untap pays energy."""
        from combo_discovery.ontology.edges import infinite_etb_loop

        engine = _engine_view()
        etb = _ability_detail(_ETB_REF, _ETB_REF, is_root=True, root_kind="trigger")
        energy = _ability_detail("svar:0:TrigEnergy:PutCounter", _ETB_REF,
                                 root_kind="trigger", via=("TrigEnergy",))
        untap = _ability_detail("line:0:7:A:TapOrUntap", "line:0:7:A:TapOrUntap",
                                is_root=True, root_kind="ability", root_cost="T PayEnergy<2>")
        partner = _synthetic_view(2, "Energy Untapper", "Creature",
                                  [vocab.ETB_TRIGGER, vocab.ADDS_COUNTERS, vocab.UNTAPS], {
                                      vocab.ETB_TRIGGER: _attach({}, etb),
                                      vocab.ADDS_COUNTERS: _attach({}, energy),
                                      vocab.UNTAPS: _attach(
                                          {"verb": "TapOrUntap",
                                           "target": parse_restriction("Artifact,Creature").to_dict()},
                                          untap),
                                  })
        assert len(list(infinite_etb_loop({1: engine, 2: partner}))) == 1

    def test_copy_hard_gate_excludes_legendary_and_noncreature(self):
        from combo_discovery.ontology.edges import infinite_etb_loop

        engine = _engine_view()
        legendary = _untapper_view(2, "Legendary Partner", "Legendary Creature Human")
        aura = _untapper_view(3, "Aura Partner", "Enchantment Aura")
        assert list(infinite_etb_loop({1: engine, 2: legendary})) == []
        assert list(infinite_etb_loop({1: engine, 3: aura})) == []

    def test_engine_without_copy_predicate_is_excluded(self):
        """A tap-cost impact engine that cannot copy the partner is out of scope."""
        from combo_discovery.ontology.edges import infinite_etb_loop

        ability = _ability_detail("line:0:6:A:Mill", "line:0:6:A:Mill",
                                  is_root=True, root_kind="ability", root_cost="T")
        engine = _synthetic_view(1, "Mill Engine", "Artifact",
                                 [vocab.TAPS_COST, vocab.MILLS],
                                 {vocab.TAPS_COST: _attach({}, ability),
                                  vocab.MILLS: _attach({}, ability)})
        partner = _untapper_view(2, "Partner", "Creature")
        assert list(infinite_etb_loop({1: engine, 2: partner})) == []

    def test_tap_or_untap_carries_a_penalty(self):
        from combo_discovery.ontology.edges import infinite_etb_loop

        engine = _engine_view()
        plain = _untapper_view(2, "Plain", "Creature", verb="Untap")
        flexible = _untapper_view(3, "Flexible", "Creature", verb="TapOrUntap")
        scores = {
            edge.target.name: edge.score
            for edge in infinite_etb_loop({1: engine, 2: plain, 3: flexible})
        }
        assert scores["Plain"] > scores["Flexible"]

    def test_compatibility_evidence_marks_verified_links(self):
        from combo_discovery.ontology.edges import infinite_etb_loop

        engine = _engine_view()
        partner = _untapper_view(2, "Partner", "Creature")
        edges = list(infinite_etb_loop({1: engine, 2: partner}))
        marker = next(e for e in edges[0].evidence if e.get("kind") == "compatibility")
        assert marker["type_verified"] is True
        assert marker["copy_verified"] is True
        assert marker["ability_linked"] is True
        assert marker["link_kind"] == "etb_chain"
        assert marker["target_restriction"] == "Permanent.YouCtrl"

    def test_per_source_cap_keeps_best_partners(self):
        from combo_discovery.ontology.edges import Edge, _cap_per_source_iter

        source = _synthetic_view(1, "Source", "Creature", [vocab.TAPS_COST])
        targets = [_synthetic_view(10 + i, f"T{i}", "Creature", [vocab.UNTAPS]) for i in range(5)]
        edges = [
            Edge("p", source, target, mechanism="m", score=float(i))
            for i, target in enumerate(targets)
        ]
        capped = list(_cap_per_source_iter(edges, 2))
        assert len(capped) == 2
        assert [edge.score for edge in capped] == [4.0, 3.0]



# ---------------------------------------------------------------------------
# Mini-corpus ontology validation
# ---------------------------------------------------------------------------


class TestPredicatesOnRealScripts:
    def test_key_predicates(self, ontology_db):
        db, _ = ontology_db
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row

        def preds(name: str) -> set[str]:
            rows = conn.execute(
                "SELECT cp.predicate FROM card_predicates cp JOIN cards c ON c.id = cp.card_id "
                "WHERE c.normalized_name = ?",
                (normalize_name(name),),
            )
            return {row["predicate"] for row in rows}

        assert {vocab.TAPS_COST, vocab.COPIES_CREATURE} <= preds("Kiki-Jiki, Mirror Breaker")
        assert {vocab.ETB_TRIGGER, vocab.UNTAPS} <= preds("Deceiver Exarch")
        assert {vocab.ETB_TRIGGER, vocab.UNTAPS} <= preds("Pestermite")
        assert {vocab.TAPS_COST, vocab.COPIES_CREATURE} <= preds("Splinter Twin")
        assert {vocab.PRODUCES_MANA, vocab.SACRIFICES_SELF} <= preds("Lion's Eye Diamond")
        assert vocab.CASTS_FROM_GRAVEYARD in preds("Yawgmoth's Will")
        assert vocab.CASTS_FROM_GRAVEYARD in preds("Underworld Breach")
        assert {vocab.TAPS_COST, vocab.MILLS} <= preds("Grindstone")
        assert vocab.SETS_COLOR in preds("Painter's Servant")
        assert vocab.CARDS_FROM_LIFE in preds("Necropotence")
        assert vocab.STORM in preds("Tendrils of Agony")
        assert preds("Grizzly Bears") == set()
        conn.close()

    def test_kiki_evidence_keeps_cost_and_targets(self, ontology_db):
        db, _ = ontology_db
        params = _query(
            db,
            "SELECT cp.params_json FROM card_predicates cp JOIN cards c ON c.id = cp.card_id "
            "WHERE c.normalized_name = ? AND cp.predicate = ?",
            (normalize_name("Kiki-Jiki, Mirror Breaker"), vocab.TAPS_COST),
        )
        assert params
        payload = json.loads(params[0]["params_json"])
        assert payload["cost"] == "T"
        assert payload["verb"] == "CopyPermanent"


class TestKnownCombos:
    def test_required_kiki_and_twin_loops(self, ontology_db):
        db, _ = ontology_db
        for combo in REQUIRED_COMBOS:
            a, b = combo["cards"]
            row = _interaction(db, a, b, combo["pattern"])
            assert row is not None, f"missing {a} + {b} ({combo['reason']})"
            assert row["direction"] == "mutual"
            mechanism = row["mechanism"]
            assert a in mechanism and b in mechanism
            assert "untap" in mechanism.lower()
            assert "tap-cost" in mechanism.lower()

    def test_at_least_eight_of_twelve_known_combos(self, ontology_db):
        db, _ = ontology_db
        matched = []
        missed = []
        for combo in KNOWN_COMBOS:
            a, b = combo["cards"]
            if _interaction(db, a, b, combo["pattern"]) is not None:
                matched.append(combo)
            else:
                missed.append(combo)
        assert len(matched) >= 8, (
            f"only {len(matched)}/12 known combos found; missed: "
            f"{[c['cards'] for c in missed]}"
        )
        # The expected misses are only the ones we deliberately did not model.
        unexpected = [c for c in missed if c.get("expected", True)]
        assert unexpected == [], f"unexpected misses: {[c['cards'] for c in unexpected]}"

    def test_negative_control_kiki_grizzly(self, ontology_db):
        db, _ = ontology_db
        assert not _edge_exists(db, "Kiki-Jiki, Mirror Breaker", "Grizzly Bears", "infinite_etb_loop")
        # And Grizzly Bears contributes no predicate at all.
        assert _query(
            db,
            "SELECT COUNT(*) AS n FROM card_predicates cp JOIN cards c ON c.id = cp.card_id "
            "WHERE c.normalized_name = ?",
            (normalize_name("Grizzly Bears"),),
        )[0]["n"] == 0

    def test_kiki_type_incompatible_partners_are_excluded(self, ontology_db):
        """Every round-1 type/controller false positive must be gone."""
        db, _ = ontology_db
        absent = [name for name in KIKI_TYPE_FALSE_POSITIVES if not _card_exists(db, name)]
        assert absent == [], f"fixtures missing from mini corpus: {absent}"
        remaining = [
            name for name in KIKI_TYPE_FALSE_POSITIVES
            if _interaction(db, "Kiki-Jiki, Mirror Breaker", name, "infinite_etb_loop") is not None
        ]
        assert remaining == [], f"type-incompatible Kiki partners still emitted: {remaining}"

    def test_kiki_round2_engine_gate_excludes(self, ontology_db):
        """The ability-scope / copy / gate false positives must be gone."""
        db, _ = ontology_db
        absent = [name for name in KIKI_ENGINE_FALSE_POSITIVES if not _card_exists(db, name)]
        assert absent == [], f"fixtures missing from mini corpus: {absent}"
        remaining = [
            name for name in KIKI_ENGINE_FALSE_POSITIVES
            if _interaction(db, "Kiki-Jiki, Mirror Breaker", name, "infinite_etb_loop") is not None
        ]
        assert remaining == [], f"engine-gate Kiki partners still emitted: {remaining}"

    def test_kiki_type_compatible_partners_remain(self, ontology_db):
        """Every required positive must survive the gate."""
        db, _ = ontology_db
        absent = [name for name in KIKI_TRUE_POSITIVES if not _card_exists(db, name)]
        assert absent == [], f"fixtures missing from mini corpus: {absent}"
        missing = [
            name for name in KIKI_TRUE_POSITIVES
            if _interaction(db, "Kiki-Jiki, Mirror Breaker", name, "infinite_etb_loop") is None
        ]
        assert missing == [], f"compatible Kiki partners dropped: {missing}"

    def test_kiki_edge_carries_all_verification_markers(self, ontology_db):
        db, _ = ontology_db
        row = _interaction(db, "Kiki-Jiki, Mirror Breaker", "Deceiver Exarch", "infinite_etb_loop")
        assert row is not None
        evidence = json.loads(row["evidence_json"])
        marker = next(item for item in evidence if item.get("kind") == "compatibility")
        assert marker["type_verified"] is True
        assert marker["copy_verified"] is True
        assert marker["ability_linked"] is True
        assert marker["link_kind"] == "etb_chain"
        assert marker["target"] == "compatible"
        assert marker["target_restriction"] == "Permanent.YouCtrl"

    def test_every_infinite_hypothesis_is_fully_verified(self, ontology_db):
        """Invariant: no emitted infinite_etb_loop edge may lack a verified
        target, a verified copy, or an ability link."""
        db, _ = ontology_db
        rows = _query(
            db,
            "SELECT i.evidence_json FROM interactions i "
            "JOIN patterns p ON p.id = i.pattern_id WHERE p.name = 'infinite_etb_loop'",
        )
        assert rows
        for row in rows:
            marker = next(
                item for item in json.loads(row["evidence_json"])
                if item.get("kind") == "compatibility"
            )
            assert marker["type_verified"] is True
            assert marker["copy_verified"] is True
            assert marker["ability_linked"] is True

    def test_hypotheses_carry_scores_and_status(self, ontology_db):
        db, _ = ontology_db
        rows = _query(
            db,
            "SELECT h.score, h.status, h.card_ids_json, p.name AS pattern "
            "FROM combo_hypotheses h JOIN patterns p ON p.id = h.pattern_id "
            "ORDER BY h.score DESC LIMIT 5",
        )
        assert rows
        assert all(row["status"] == "proposed" for row in rows)
        assert all(0.0 < row["score"] <= 1.0 for row in rows)
        assert all(len(json.loads(row["card_ids_json"])) == 2 for row in rows)


class TestSchemaAndPersistence:
    def test_patterns_vocabulary_persisted(self, ontology_db):
        db, _ = ontology_db
        rows = _query(db, "SELECT name, version, pattern_json FROM patterns ORDER BY name")
        assert {row["name"] for row in rows} == {p.name for p in PATTERNS}
        assert all(row["version"] == 1 for row in rows)
        assert all(json.loads(row["pattern_json"]) for row in rows)

    def test_schema_version_is_six(self, ontology_db):
        db, _ = ontology_db
        versions = [row[0] for row in _query(db, "SELECT version FROM schema_version")]
        assert versions == [1, 2, 3, 4, 5, 6]

    def test_migration_3_creates_tables(self, tmp_path):
        conn = sqlite3.connect(tmp_path / "raw.db")
        _migration_3(conn)
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        assert {"patterns", "card_predicates", "interactions", "combo_hypotheses"} <= tables

    def test_builder_is_idempotent_without_force(self, tmp_path, ontology_db):
        db, _ = ontology_db
        before = _query(db, "SELECT COUNT(*) AS n FROM interactions")[0]["n"]
        store = ExperimentStore(db)
        report = build_ontology(store, mode="legacy")  # already built
        store.close()
        assert report.already_built is True
        assert _query(db, "SELECT COUNT(*) AS n FROM interactions")[0]["n"] == before

    def test_export_jsonl_for_ontology_tables(self, ontology_db, tmp_path):
        db, _ = ontology_db
        store = ExperimentStore(db)
        out = tmp_path / "predicates.jsonl"
        store.export_jsonl("card_predicates", out)
        store.close()
        lines = out.read_text().splitlines()
        assert len(lines) == _query(db, "SELECT COUNT(*) AS n FROM card_predicates")[0]["n"]
        assert json.loads(lines[0])["predicate"]


# ---------------------------------------------------------------------------
# Algebra builder (default path): q: patterns, cycles, determinism
# ---------------------------------------------------------------------------


class TestAlgebraBuilder:
    @staticmethod
    def _build(root: Path):
        store = ExperimentStore(root / "research.db")
        try:
            import_corpus(store, root)
            return build_ontology(store, mode="algebra")
        finally:
            store.close()

    def test_registers_q_patterns_and_keeps_legacy(self, tmp_path):
        root = _seed_cardsfolder(tmp_path)
        report = self._build(root)
        db = root / "research.db"
        assert report.mode == "algebra"
        assert report.interactions_total > 0
        names = {row["name"] for row in _query(db, "SELECT name FROM patterns")}
        assert {p.name for p in PATTERNS} <= names
        assert {f"q:{q.name}" for q in QUERIES} <= names
        used = {
            row["name"] for row in _query(
                db,
                "SELECT DISTINCT p.name AS name FROM interactions i "
                "JOIN patterns p ON p.id = i.pattern_id",
            )
        }
        assert used and all(name.startswith("q:") for name in used)

    def test_maps_cycles_to_hypotheses_and_links_to_interactions(self, tmp_path):
        root = _seed_cardsfolder(tmp_path)
        report = self._build(root)
        db = root / "research.db"
        hypotheses = _query(
            db,
            "SELECT h.card_ids_json, h.mechanism FROM combo_hypotheses h "
            "JOIN patterns p ON p.id = h.pattern_id WHERE p.name LIKE 'q:%'",
        )
        assert hypotheses and report.hypotheses_total > 0
        for row in hypotheses:
            assert len(json.loads(row["card_ids_json"])) >= 2
            assert row["mechanism"]
        assert report.links_total > 0
        assert report.pool_size > 0

    def test_finds_kiki_exarch_as_an_algebra_proposal(self, tmp_path):
        root = _seed_cardsfolder(tmp_path)
        self._build(root)
        db = root / "research.db"
        row = _interaction(db, "Kiki-Jiki, Mirror Breaker", "Deceiver Exarch",
                           "q:infinite_etb_loop")
        assert row is not None

    def test_is_idempotent_without_force(self, tmp_path):
        root = _seed_cardsfolder(tmp_path)
        store = ExperimentStore(root / "research.db")
        try:
            import_corpus(store, root)
            first = build_ontology(store, mode="algebra")
            second = build_ontology(store, mode="algebra")
        finally:
            store.close()
        assert first.interactions_total > 0
        assert second.already_built is True

    def test_two_builds_are_deterministic(self, tmp_path):
        sql = (
            "SELECT p.name, i.source_card_id, i.target_card_id, i.score, "
            "i.evidence_json FROM interactions i "
            "JOIN patterns p ON p.id = i.pattern_id "
            "WHERE p.name LIKE 'q:%' "
            "ORDER BY p.name, i.source_card_id, i.target_card_id"
        )
        digests = []
        for sub in ("one", "two"):
            root = _seed_cardsfolder(tmp_path / sub)
            self._build(root)
            digests.append([tuple(row) for row in _query(root / "research.db", sql)])
        assert digests[0] and digests[0] == digests[1]


# ---------------------------------------------------------------------------
# Full-corpus performance sanity (read-only; skipped without research.db)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not RESEARCH_DB.is_file(), reason="research.db corpus absent")
def test_full_corpus_extraction_and_edges_performance(capsys):
    conn = sqlite3.connect(f"file:{RESEARCH_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        # The latest import_runs row may be a non-corpus import (Scryfall oracle-id
        # backfill / Commander Spellbook); pick the most recent one with cards.
        import_id = conn.execute(
            "SELECT r.import_id AS import_id FROM import_runs r "
            "WHERE EXISTS (SELECT 1 FROM cards c WHERE c.import_id = r.import_id) "
            "ORDER BY r.rowid DESC LIMIT 1"
        ).fetchone()["import_id"]
        started = time.monotonic()
        contexts, effects = load_contexts(conn, import_id)
        predicates = {
            cid: extract_card_predicates(ctx, effects.get(cid, []))
            for cid, ctx in contexts.items()
        }
        extract_s = time.monotonic() - started
        assert len(contexts) > 30_000
        assert sum(len(p) for p in predicates.values()) > 50_000

        views = {
            cid: CardView.build(ctx, predicates.get(cid, []), effects.get(cid, []))
            for cid, ctx in contexts.items()
        }
        edge_started = time.monotonic()
        edges = build_edges(views)
        edge_s = time.monotonic() - edge_started
    finally:
        conn.close()

    # Report the numbers (visible with -s) and assert only loose sanity bounds.
    print(
        f"[ontology perf] cards={len(contexts)} predicates="
        f"{sum(len(p) for p in predicates.values())} edges={len(edges)} "
        f"extract={extract_s:.1f}s edges_time={edge_s:.1f}s"
    )
    assert len(edges) > 100_000
    patterns = {edge.pattern for edge in edges}
    assert "infinite_etb_loop" in patterns
