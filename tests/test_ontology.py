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
from combo_discovery.ontology.edges import PATTERNS, CardView, build_edges
from combo_discovery.ontology.extractor import (
    CardContext,
    CardEffect,
    CardPredicate,
    classify_effect,
    extract_card_predicates,
    load_contexts,
    mana_value,
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

# Scripts in the default mini corpus (a subset exercises every pattern).
MINI_CARDS = [
    "kiki_jiki_mirror_breaker.txt", "deceiver_exarch.txt", "pestermite.txt",
    "splinter_twin.txt", "grizzly_bears.txt", "tolarian_academy.txt",
    "lions_eye_diamond.txt", "yawgmoths_will.txt", "underworld_breach.txt",
    "goblin_welder.txt", "myr_retriever.txt", "tendrils_of_agony.txt",
    "rite_of_flame.txt", "time_spiral.txt", "painters_servant.txt",
    "grindstone.txt", "necropotence.txt", "brainstorm.txt",
    "priest_of_gix.txt", "dockside_extortionist.txt", "walking_ballista.txt",
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
    for name in MINI_CARDS:
        source_fixture = FIXTURES / name
        if not source_fixture.is_file():
            continue
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
        "SELECT p.name AS pattern, i.direction, i.mechanism, "
        "s.normalized_name AS src, t.normalized_name AS tgt, s.name AS src_name, "
        "t.name AS tgt_name FROM interactions i "
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


class TestInfiniteLoopScoring:
    @staticmethod
    def _view(card_id, name, type_line, predicates):
        from combo_discovery.ontology.edges import CardView

        ctx = CardContext(card_id, name, normalize_name(name), "1", type_line, "", "", ())
        preds = [CardPredicate(card_id=card_id, face_index=0, predicate=p) for p in predicates]
        return CardView.build(ctx, preds)

    def test_creature_untapper_outranks_aura_untapper(self):
        """The canonical Kiki/Exarch shape (copy a creature, untap it) scores
        above a non-creature ETB untapper that cannot be copied."""
        from combo_discovery.ontology.edges import infinite_etb_loop

        engine = self._view(1, "Engine", "Creature", [vocab.TAPS_COST, vocab.COPIES_CREATURE])
        creature = self._view(2, "Creature Untapper", "Creature", [vocab.UNTAPS, vocab.ETB_TRIGGER])
        aura = self._view(3, "Aura Untapper", "Enchantment Aura", [vocab.UNTAPS, vocab.ETB_TRIGGER])
        scores = {
            edge.target.name: edge.score
            for edge in infinite_etb_loop({1: engine, 2: creature, 3: aura})
        }
        assert scores["Creature Untapper"] > scores["Aura Untapper"]

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
