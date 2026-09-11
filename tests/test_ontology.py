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
    {"cards": ("Deceiver Exarch", "Grindstone"), "pattern": "infinite_etb_loop",
     "reason": "Exarch's ETB untaps any tap-cost engine (here Grindstone).", "expected": True},
    {"cards": ("Dockside Extortionist", "Walking Ballista"), "pattern": "mana_engine",
     "reason": "Dockside makes Treasure; Ballista is an X-cost mana sink.", "expected": True},
    {"cards": ("Tendrils of Agony", "Rite of Flame"), "pattern": "storm_engine",
     "reason": "Rite of Flame is a cheap storm enabler.", "expected": True},
    {"cards": ("Priest of Gix", "Walking Ballista"), "pattern": "mana_engine",
     "reason": "Priest of Gix's ETB ritual pays for an X-cost payoff.", "expected": True},
]

#: False positives from the type-compatibility bug report: these cards' ETB
#: untaps cannot legally target Kiki-Jiki (wrong type, wrong controller,
#: self-only, or a conditional rider), so no ``infinite_etb_loop`` edge may pair
#: them with Kiki.
KIKI_FALSE_POSITIVES = [
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

#: True positives: the untapper can legally select Kiki (a creature/permanent
#: you control), so the edge must survive the compatibility gate.  Some cannot
#: actually be copied by Kiki (legendary / non-creature) and are therefore
#: penalised, not dropped.
KIKI_TRUE_POSITIVES = [
    "Deceiver Exarch", "Pestermite", "Corridor Monitor", "Village Bell-Ringer",
    "Sky Hussar", "Hyrax Tower Scout", "Sparring Mummy", "Bounding Krasis",
    "Breaching Hippocamp", "Little Bear", "White Plume Adventurer",
    "Derevi, Empyrial Tactician", "Eager Beaver", "Formidable Speaker",
    "Dee Kay, Finder of the Lost", "Janjeet Sentry", "Glamermite",
    "Flash Thompson, Spider-Fan", "Grim Reaper's Sprint", "All-Out Assault",
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
    report = build_ontology(store, apply_caps=False)
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


class TestInfiniteLoopScoring:
    @staticmethod
    def _view(card_id, name, type_line, predicates, params=None):
        return _synthetic_view(card_id, name, type_line, predicates, params)

    @staticmethod
    def _untap_params(verb, restriction):
        return {
            vocab.UNTAPS: {"verb": verb, "target": parse_restriction(restriction).to_dict()}
        }

    def test_creature_untapper_outranks_uncopyable_aura(self):
        """The canonical Kiki/Exarch shape scores above a non-creature ETB
        untapper that Kiki cannot copy."""
        from combo_discovery.ontology.edges import infinite_etb_loop

        engine = self._view(1, "Engine", "Legendary Creature",
                            [vocab.TAPS_COST, vocab.COPIES_CREATURE],
                            {vocab.COPIES_CREATURE: {"copy": parse_restriction(
                                "Creature.nonLegendary+YouCtrl").to_dict()}})
        creature = self._view(2, "Creature Untapper", "Creature",
                              [vocab.UNTAPS, vocab.ETB_TRIGGER],
                              self._untap_params("Untap", "Permanent.YouCtrl"))
        aura = self._view(3, "Aura Untapper", "Enchantment Aura",
                          [vocab.UNTAPS, vocab.ETB_TRIGGER],
                          self._untap_params("Untap", "Permanent.YouCtrl"))
        scores = {
            edge.target.name: edge.score
            for edge in infinite_etb_loop({1: engine, 2: creature, 3: aura})
        }
        assert scores["Creature Untapper"] > scores["Aura Untapper"]

    def test_incompatible_untapper_is_excluded(self):
        from combo_discovery.ontology.edges import infinite_etb_loop

        engine = self._view(1, "Engine", "Legendary Creature",
                            [vocab.TAPS_COST, vocab.COPIES_CREATURE],
                            {vocab.COPIES_CREATURE: {"copy": parse_restriction(
                                "Creature.nonLegendary+YouCtrl").to_dict()}})
        land_untapper = self._view(2, "Land Untapper", "Creature",
                                   [vocab.UNTAPS, vocab.ETB_TRIGGER],
                                   self._untap_params("UntapAll", "Land.YouCtrl"))
        assert list(infinite_etb_loop({1: engine, 2: land_untapper})) == []

    def test_self_only_untapper_is_excluded(self):
        from combo_discovery.ontology.edges import infinite_etb_loop

        engine = self._view(1, "Engine", "Creature",
                            [vocab.TAPS_COST, vocab.COPIES_CREATURE],
                            {vocab.COPIES_CREATURE: {"copy": parse_restriction(
                                "Creature.nonLegendary+YouCtrl").to_dict()}})
        self_untapper = self._view(2, "Self Untapper", "Creature",
                                   [vocab.UNTAPS, vocab.ETB_TRIGGER],
                                   self._untap_params("Untap", "Card.Self"))
        assert list(infinite_etb_loop({1: engine, 2: self_untapper})) == []

    def test_tap_or_untap_carries_a_penalty(self):
        from combo_discovery.ontology.edges import infinite_etb_loop

        engine = self._view(1, "Engine", "Creature",
                            [vocab.TAPS_COST, vocab.COPIES_CREATURE],
                            {vocab.COPIES_CREATURE: {"copy": parse_restriction(
                                "Creature.nonLegendary+YouCtrl").to_dict()}})
        plain = self._view(2, "Plain", "Creature", [vocab.UNTAPS, vocab.ETB_TRIGGER],
                           self._untap_params("Untap", "Permanent.YouCtrl"))
        flexible = self._view(3, "Flexible", "Creature", [vocab.UNTAPS, vocab.ETB_TRIGGER],
                              self._untap_params("TapOrUntap", "Permanent.YouCtrl"))
        scores = {
            edge.target.name: edge.score
            for edge in infinite_etb_loop({1: engine, 2: plain, 3: flexible})
        }
        assert scores["Plain"] > scores["Flexible"]

    def test_compatibility_evidence_marks_type_verified(self):
        from combo_discovery.ontology.edges import infinite_etb_loop

        engine = self._view(1, "Engine", "Creature",
                            [vocab.TAPS_COST, vocab.COPIES_CREATURE],
                            {vocab.COPIES_CREATURE: {"copy": parse_restriction(
                                "Creature.nonLegendary+YouCtrl").to_dict()}})
        partner = self._view(2, "Partner", "Creature", [vocab.UNTAPS, vocab.ETB_TRIGGER],
                             self._untap_params("Untap", "Permanent.YouCtrl"))
        edges = list(infinite_etb_loop({1: engine, 2: partner}))
        marker = next(e for e in edges[0].evidence if e.get("kind") == "compatibility")
        assert marker["type_verified"] is True
        assert marker["target_restriction"] == "Permanent.YouCtrl"

    def test_per_source_cap_keeps_best_partners(self):
        from combo_discovery.ontology.edges import Edge, _cap_per_source_iter

        source = self._view(1, "Source", "Creature", [vocab.TAPS_COST])
        targets = [self._view(10 + i, f"T{i}", "Creature", [vocab.UNTAPS]) for i in range(5)]
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
        """Every false positive from the bug report must be gone."""
        db, _ = ontology_db
        remaining = [
            name for name in KIKI_FALSE_POSITIVES
            if _interaction(db, "Kiki-Jiki, Mirror Breaker", name, "infinite_etb_loop") is not None
        ]
        assert remaining == [], f"type-incompatible Kiki partners still emitted: {remaining}"

    def test_kiki_type_compatible_partners_remain(self, ontology_db):
        """Every true positive from the bug report must survive the gate."""
        db, _ = ontology_db
        missing = [
            name for name in KIKI_TRUE_POSITIVES
            if _interaction(db, "Kiki-Jiki, Mirror Breaker", name, "infinite_etb_loop") is None
        ]
        assert missing == [], f"compatible Kiki partners dropped: {missing}"

    def test_kiki_edge_carries_type_verified_marker(self, ontology_db):
        db, _ = ontology_db
        row = _interaction(db, "Kiki-Jiki, Mirror Breaker", "Deceiver Exarch", "infinite_etb_loop")
        assert row is not None
        evidence = json.loads(row["evidence_json"])
        marker = next(item for item in evidence if item.get("kind") == "compatibility")
        assert marker["type_verified"] is True
        assert marker["target"] == "compatible"
        assert marker["target_restriction"] == "Permanent.YouCtrl"

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

    def test_schema_version_is_three(self, ontology_db):
        db, _ = ontology_db
        versions = [row[0] for row in _query(db, "SELECT version FROM schema_version")]
        assert versions == [1, 2, 3]

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
        report = build_ontology(store)  # already built
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
# Full-corpus performance sanity (read-only; skipped without research.db)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not RESEARCH_DB.is_file(), reason="research.db corpus absent")
def test_full_corpus_extraction_and_edges_performance(capsys):
    conn = sqlite3.connect(f"file:{RESEARCH_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        import_id = conn.execute(
            "SELECT import_id FROM import_runs ORDER BY rowid DESC LIMIT 1"
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
