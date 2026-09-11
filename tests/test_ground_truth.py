"""Ground-truth (Commander Spellbook) ingestion + evaluation harness tests.

Everything runs against a vendored trimmed ``variants.json`` fixture and a
synthetic corpus; no network.
"""

from __future__ import annotations

import gzip
import json
import shutil
import sqlite3
import uuid
from pathlib import Path

import pytest

from combo_discovery.corpus.importer import normalize_name
from combo_discovery.corpus.names import (
    front_face_name,
    is_alchemy,
    normalize_card_name,
    pair_hash,
    resolve_spellbook_use,
    split_dfc,
)
from combo_discovery.corpus.spellbook import VintageLegality, import_spellbook
from combo_discovery.evaluation import (
    classify_pairs,
    diagnostics,
    metrics,
    novelty_status,
    persist_evaluation,
)
from combo_discovery.store import ExperimentStore

FIXTURE = Path(__file__).parent / "fixtures" / "ground_truth" / "variants.json"
BANNED = normalize_card_name("Dee Kay, Finder of the Lost")

CORPUS_CARDS = [
    "Kiki-Jiki, Mirror Breaker", "Deceiver Exarch", "Pestermite", "Combat Celebrant",
    "Fable of the Mirror-Breaker", "Dee Kay, Finder of the Lost", "Grizzly Bears",
    "Fog Bank",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_corpus(store: ExperimentStore) -> str:
    conn = store._conn
    import_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO import_runs (import_id, started_at, scryfall_source) VALUES (?, ?, ?)",
        (import_id, "2026-01-01T00:00:00+00:00", "forge_script"),
    )
    for name in CORPUS_CARDS:
        conn.execute(
            "INSERT INTO cards (import_id, file_sha256, name, normalized_name) VALUES (?, ?, ?, ?)",
            (import_id, "0" * 64, name, normalize_card_name(name)),
        )
    conn.commit()
    return import_id


def _seed_oracle_ids(store: ExperimentStore, import_id: str, mapping: dict[str, str]) -> None:
    conn = store._conn
    for name, oracle_id in mapping.items():
        row = conn.execute(
            "SELECT id FROM cards WHERE normalized_name = ?", (normalize_card_name(name),)
        ).fetchone()
        conn.execute(
            "INSERT INTO card_oracle_ids (import_id, card_id, oracle_id, source, imported_at) "
            "VALUES (?, ?, ?, 'scryfall', '2026-01-01T00:00:00+00:00')",
            (import_id, int(row["id"]), oracle_id),
        )
    conn.commit()


def _card_id(store: ExperimentStore, name: str) -> int:
    row = store._conn.execute(
        "SELECT id FROM cards WHERE normalized_name = ?", (normalize_card_name(name),)
    ).fetchone()
    assert row is not None, name
    return int(row["id"])


def _ensure_pattern(store: ExperimentStore, name: str, pattern_id: int = 1) -> int:
    store._conn.execute(
        "INSERT OR IGNORE INTO patterns (id, name, description, pattern_json, version) "
        "VALUES (?, ?, ?, '{}', 1)",
        (pattern_id, name, "test pattern"),
    )
    store._conn.commit()
    return pattern_id


def _propose(
    store: ExperimentStore,
    import_id: str,
    a_name: str,
    b_name: str,
    score: float,
    predicates: tuple[str, ...] = ("TAPS_COST", "UNTAPS"),
    pattern_id: int = 1,
) -> None:
    evidence = json.dumps([{"predicate": p} for p in predicates])
    store._conn.execute(
        "INSERT INTO interactions (import_id, source_card_id, target_card_id, pattern_id, "
        "direction, mechanism, score, evidence_json, created_at) "
        "VALUES (?, ?, ?, ?, 'mutual', 'm', ?, ?, '2026-01-01T00:00:00+00:00')",
        (import_id, _card_id(store, a_name), _card_id(store, b_name), pattern_id, score, evidence),
    )
    store._conn.commit()


@pytest.fixture
def gt_store(tmp_path: Path):
    db = tmp_path / "gt.db"
    store = ExperimentStore(db)
    import_id = _seed_corpus(store)
    _seed_oracle_ids(store, import_id, {
        "Kiki-Jiki, Mirror Breaker": "oracle-kiki",
        "Deceiver Exarch": "oracle-exarch",
    })
    report = import_spellbook(
        store, FIXTURE, vintage=VintageLegality(banned=frozenset({BANNED}))
    )
    yield store, import_id, report
    store.close()


def _rows(store: ExperimentStore, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return store._conn.execute(sql, params).fetchall()


# ---------------------------------------------------------------------------
# Name matching
# ---------------------------------------------------------------------------


class TestNameNormalization:
    def test_normalization_matches_corpus(self):
        for name in [
            "Kiki-Jiki, Mirror Breaker", "Lim-Dûl's Vault", "Déjà Vu",
            "SP//dr, Piloted by Peni",
            "Birgi, God of Storytelling // Harnfel, Horn of Bounty",
        ]:
            assert normalize_card_name(name) == normalize_name(name)

    def test_multi_face_front_face(self):
        name = "Birgi, God of Storytelling // Harnfel, Horn of Bounty"
        assert split_dfc(name) == ["Birgi, God of Storytelling", "Harnfel, Horn of Bounty"]
        assert front_face_name(name) == "Birgi, God of Storytelling"
        assert normalize_card_name(front_face_name(name)) == "birgi god of storytelling"

    def test_accents(self):
        assert normalize_card_name("Déjà Vu") == "deja vu"
        assert normalize_card_name("Lim-Dûl's Vault") == "lim dul s vault"

    def test_single_face_literal_slashes_not_split(self):
        name = "SP//dr, Piloted by Peni"
        # No " // " separator, so it is one name even though it contains "//".
        assert split_dfc(name) == [name]
        assert front_face_name(name) == name
        resolved = resolve_spellbook_use(
            {"name": name, "oracleId": "oracle-spdr", "faces": 1},
            local_names={normalize_card_name(name)},
        )
        assert resolved.via == "name"
        assert resolved.normalized_name == normalize_card_name(name)

    def test_multi_face_used_face_two_front_fallback(self):
        resolved = resolve_spellbook_use(
            {
                "name": "Fable of the Mirror-Breaker // Reflection of Kiki-Jiki",
                "oracleId": "oracle-fable", "faces": 2,
            },
            used_face=2,
            local_names={"fable of the mirror breaker"},
        )
        assert resolved.via == "name"
        assert resolved.normalized_name == "fable of the mirror breaker"

    def test_alchemy_excluded(self):
        assert is_alchemy("A-Something Rebalanced")
        resolved = resolve_spellbook_use(
            {"name": "A-Something Rebalanced", "oracleId": "x", "faces": 1}, local_names=set()
        )
        assert resolved.via == "excluded"

    def test_oracle_id_preferred(self):
        resolved = resolve_spellbook_use(
            {"name": "Some Renamed Card", "oracleId": "oracle-kiki", "faces": 1},
            oracle_names={"oracle-kiki": "kiki jiki mirror breaker"},
            local_names={"kiki jiki mirror breaker"},
        )
        assert resolved.via == "oracle"
        assert resolved.normalized_name == "kiki jiki mirror breaker"

    def test_unmatched_name_is_flagged(self):
        resolved = resolve_spellbook_use(
            {"name": "Not In Corpus", "oracleId": "nope", "faces": 1},
            local_names={"kiki jiki mirror breaker"},
        )
        assert resolved.via == "unmatched"


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------


class TestSpellbookImport:
    def test_filters_and_counts(self, gt_store):
        store, _import_id, report = gt_store
        assert report.variants_seen == 10
        assert report.variants_kept == 5
        assert report.skipped_status == 1
        assert report.skipped_vintage == 2   # non-vintage variant + banned card
        assert report.skipped_commander == 1
        assert report.skipped_alchemy == 1
        assert report.combos_written == 5
        assert report.pairs_written == 7
        assert report.full_variant_pairs == 2
        assert report.aliases_written == 2

    def test_oracle_and_name_matching(self, gt_store):
        _store, _import_id, report = gt_store
        # Kiki/Exarch resolve via oracle id; the rest by normalized name.
        assert report.cards_oracle_matched == 6
        assert report.cards_name_matched == 5
        assert report.cards_unmatched == 0

    def test_pair_expansion_and_full_variant(self, gt_store):
        store, _import_id, _report = gt_store
        pairs = _rows(store, "SELECT pair_hash, is_full_variant FROM known_combo_pairs")
        assert len(pairs) == 7
        assert sum(1 for p in pairs if p["is_full_variant"]) == 2
        # Kiki+Exarch and Kiki+Pestermite are the two exact variants.
        expected = {
            pair_hash("kiki jiki mirror breaker", "deceiver exarch"),
            pair_hash("kiki jiki mirror breaker", "pestermite"),
        }
        assert {p["pair_hash"] for p in pairs if p["is_full_variant"]} == expected

    def test_three_card_combo_expands_to_subpairs(self, gt_store):
        store, _import_id, _report = gt_store
        row = store._conn.execute(
            "SELECT id FROM known_combos WHERE source_id = 'v-kiki-exarch-celebrant'"
        ).fetchone()
        subpairs = _rows(
            store, "SELECT pair_hash FROM known_combo_pairs WHERE combo_id = ?", (row["id"],)
        )
        assert len(subpairs) == 3
        assert all(
            store._conn.execute(
                "SELECT is_full_variant FROM known_combo_pairs WHERE id = ?", (r["id"],)
            ).fetchone()["is_full_variant"] == 0
            for r in _rows(store, "SELECT id FROM known_combo_pairs WHERE combo_id = ?", (row["id"],))
        )

    def test_dfc_front_face_stored(self, gt_store):
        store, _import_id, _report = gt_store
        row = store._conn.execute(
            "SELECT normalized_name FROM known_combo_cards WHERE raw_name LIKE 'Fable%'"
        ).fetchone()
        assert row["normalized_name"] == "fable of the mirror breaker"

    def test_import_is_idempotent_by_source_id(self, gt_store):
        store, _import_id, _report = gt_store
        before = _rows(store, "SELECT COUNT(*) AS n FROM known_combos")[0]["n"]
        again = import_spellbook(
            store, FIXTURE, vintage=VintageLegality(banned=frozenset({BANNED}))
        )
        assert again.variants_kept == 0
        assert _rows(store, "SELECT COUNT(*) AS n FROM known_combos")[0]["n"] == before

    def test_gzip_streaming(self, tmp_path):
        gz = tmp_path / "variants.json.gz"
        with open(FIXTURE, "rb") as src, gzip.open(gz, "wb") as dst:
            shutil.copyfileobj(src, dst)
        store = ExperimentStore(tmp_path / "gz.db")
        _seed_corpus(store)
        report = import_spellbook(
            store, gz, vintage=VintageLegality(banned=frozenset({BANNED}))
        )
        assert report.variants_seen == 10
        assert report.variants_kept == 5
        assert report.version == "6.4.0-fixture"
        store.close()

    def test_vintage_filter_off_keeps_more(self, tmp_path):
        store = ExperimentStore(tmp_path / "novintage.db")
        _seed_corpus(store)
        report = import_spellbook(
            store, FIXTURE, vintage_only=False,
            vintage=VintageLegality(banned=frozenset({BANNED})),
        )
        # Only status and Alchemy filter now: 10 - 1 status - 1 alchemy.
        assert report.skipped_status == 1
        assert report.skipped_alchemy == 1
        assert report.variants_kept == 8
        store.close()

    def test_aliases_written(self, gt_store):
        store, _import_id, _report = gt_store
        aliases = {r["alias_id"]: r["canonical_id"] for r in _rows(store, "SELECT alias_id, canonical_id FROM known_aliases")}
        assert aliases == {"v-kiki-exarch--alias": "v-kiki-exarch", "orphan-alias": None}


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


class TestEvaluation:
    @staticmethod
    def _classified(store, import_id):
        _ensure_pattern(store, "infinite_etb_loop")
        _propose(store, import_id, "Kiki-Jiki, Mirror Breaker", "Deceiver Exarch", 0.9)
        _propose(store, import_id, "Kiki-Jiki, Mirror Breaker", "Combat Celebrant", 0.7)
        _propose(store, import_id, "Kiki-Jiki, Mirror Breaker", "Grizzly Bears", 0.5,
                 predicates=("TAPS_COST", "COPIES_CREATURE"))
        _propose(store, import_id, "Kiki-Jiki, Mirror Breaker", "Fog Bank", 0.3,
                 predicates=("TAPS_COST", "COPIES_CREATURE"))
        return classify_pairs(store)

    def test_all_four_verdicts(self, gt_store):
        store, import_id, _report = gt_store
        verdicts = self._classified(store, import_id)
        by_names = {(v.source_name, v.target_name): v.verdict for v in verdicts}
        assert by_names[("kiki jiki mirror breaker", "deceiver exarch")] == "known_pair"
        assert by_names[("kiki jiki mirror breaker", "combat celebrant")] == "contained_in_known"
        assert by_names[("kiki jiki mirror breaker", "grizzly bears")] == "unmatched"
        # Kiki + Pestermite is known but we did not propose it -> missed.
        missed = [v for v in verdicts if v.verdict == "missed"]
        assert {(m.source_name, m.target_name) for m in missed} == {
            ("kiki jiki mirror breaker", "pestermite")
        }

    def test_metrics_hand_computed(self, gt_store):
        store, import_id, _report = gt_store
        report = metrics(self._classified(store, import_id), k_values=(2, 4))
        agg = report.aggregate
        assert (agg.true_positives, agg.partials, agg.false_positives, agg.missed) == (1, 1, 2, 1)
        assert agg.precision == 0.25
        assert agg.precision_incl_partial == 0.5
        assert agg.recall == 0.5
        assert agg.f1 == 0.3333
        assert agg.precision_at_k[2] == 0.5
        assert agg.precision_at_k[4] == 0.25

    def test_metrics_per_card(self, gt_store):
        store, import_id, _report = gt_store
        report = metrics(self._classified(store, import_id))
        exarch = report.by_card["deceiver exarch"]
        assert (exarch.true_positives, exarch.false_positives, exarch.missed) == (1, 0, 0)
        assert exarch.precision == 1.0 and exarch.recall == 1.0
        pestermite = report.by_card["pestermite"]
        assert (pestermite.true_positives, pestermite.missed) == (0, 1)
        assert pestermite.recall == 0.0
        kiki = report.by_card["kiki jiki mirror breaker"]
        assert (kiki.true_positives, kiki.false_positives, kiki.missed) == (1, 2, 1)

    def test_metrics_per_pattern(self, gt_store):
        store, import_id, _report = gt_store
        report = metrics(self._classified(store, import_id))
        assert set(report.by_pattern) == {"infinite_etb_loop"}
        pattern = report.by_pattern["infinite_etb_loop"]
        assert pattern.precision == 0.25 and pattern.recall == 0.5

    def test_diagnostics_clusters(self, gt_store):
        store, import_id, _report = gt_store
        diag = diagnostics(store, self._classified(store, import_id))
        assert diag.false_positive_clusters[0] == ("COPIES_CREATURE + TAPS_COST", 2)
        # The missed Kiki+Pestermite variant produces infinite creature tokens.
        labels = dict(diag.miss_clusters)
        assert any("Infinite creature tokens" in label for label in labels)

    def test_card_filter(self, gt_store):
        store, import_id, _report = gt_store
        self._classified(store, import_id)
        kiki = classify_pairs(store, card="Kiki-Jiki, Mirror Breaker")
        assert {v.verdict for v in kiki if v.verdict != "missed"} == {
            "known_pair", "contained_in_known", "unmatched"
        }
        pestermite = classify_pairs(store, card="Pestermite")
        assert any(v.verdict == "missed" for v in pestermite)
        assert all(
            v.verdict == "missed" or "pestermite" in (v.source_name, v.target_name)
            for v in pestermite
        )

    def test_persist_evaluation(self, gt_store):
        store, import_id, _report = gt_store
        report = metrics(self._classified(store, import_id))
        run_id = persist_evaluation(store, report, [], card_filter="Kiki-Jiki, Mirror Breaker")
        assert _rows(store, "SELECT COUNT(*) AS n FROM evaluation_runs WHERE id = ?", (run_id,))[0]["n"] == 1
        scopes = {
            r["scope"] for r in _rows(
                store, "SELECT scope FROM evaluation_results WHERE run_id = ?", (run_id,)
            )
        }
        assert scopes == {"aggregate", "pattern", "card"}


# ---------------------------------------------------------------------------
# Novelty policy (two-source rule)
# ---------------------------------------------------------------------------


class TestNoveltyPolicy:
    def test_known_vs_needs_second_source(self, gt_store):
        store, _import_id, _report = gt_store
        assert novelty_status(store, ("Kiki-Jiki, Mirror Breaker", "Deceiver Exarch")) == "known"
        assert novelty_status(store, ("Kiki-Jiki, Mirror Breaker", "Grizzly Bears")) == "needs_second_source"

    def test_never_returns_novel(self, gt_store):
        store, _import_id, _report = gt_store
        for pair in [
            ("Kiki-Jiki, Mirror Breaker", "Grizzly Bears"),
            ("Deceiver Exarch", "Fog Bank"),
        ]:
            assert novelty_status(store, pair) != "novel"

    def test_tier_b_hit_is_observed(self, gt_store):
        from combo_discovery.evaluation import record_observed_deck

        store, _import_id, _report = gt_store
        deck_id = record_observed_deck(
            store, source="archidekt", source_id="deck-1", url="https://example.test",
            commander="Kiki-Jiki, Mirror Breaker", format="commander",
            cards=["Kiki-Jiki, Mirror Breaker", "Grizzly Bears", "Fog Bank"],
        )
        assert deck_id > 0
        assert _rows(store, "SELECT COUNT(*) AS n FROM observed_deck_cards WHERE deck_id = ?", (deck_id,))[0]["n"] == 3
        assert _rows(store, "SELECT COUNT(*) AS n FROM observed_pairs WHERE deck_id = ?", (deck_id,))[0]["n"] == 3
        assert novelty_status(store, ("Kiki-Jiki, Mirror Breaker", "Grizzly Bears")) == "observed"

    def test_tier_a_and_b_are_separate(self, gt_store):
        store, _import_id, _report = gt_store
        assert _rows(store, "SELECT COUNT(*) AS n FROM observed_pairs")[0]["n"] == 0
        assert _rows(store, "SELECT COUNT(*) AS n FROM observed_decks")[0]["n"] == 0
        assert novelty_status(store, ("Grizzly Bears", "Fog Bank")) == "needs_second_source"


# ---------------------------------------------------------------------------
# Append-only discipline
# ---------------------------------------------------------------------------


class TestAppendOnly:
    @pytest.mark.parametrize(
        "filename", ["corpus/spellbook.py", "corpus/names.py", "corpus/oracle.py", "evaluation.py"]
    )
    def test_new_modules_have_no_mutating_sql(self, filename):
        import re

        src = (
            Path(__file__).resolve().parent.parent
            / "src" / "combo_discovery" / filename
        ).read_text(encoding="utf-8")
        # Look for SQL DML, not Python's ``set.update`` / ``hashlib.update``.
        assert not re.search(r"\b(UPDATE|DELETE)\s+[A-Za-z_]", src, re.IGNORECASE), filename

    def test_known_import_appends_import_run(self, gt_store):
        store, _import_id, report = gt_store
        row = store._conn.execute(
            "SELECT scryfall_source, scryfall_sha256, notes FROM import_runs WHERE import_id = ?",
            (report.import_id,),
        ).fetchone()
        assert row["scryfall_source"] == "spellbook"
        assert row["scryfall_sha256"] == report.sha256
        assert json.loads(row["notes"])["version"] == "6.4.0-fixture"
