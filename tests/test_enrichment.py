"""Motif-enrichment analysis tests.

Synthetic fixture with hand-computable lift/chi-square, a fake signature
provider, support filtering, anti-enrichment, the Vintage filter, persistence
and append-only discipline.
"""

from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path

import pytest

from combo_discovery.analysis.enrichment import (
    PredicateSignatureProvider,
    analyze_motifs,
    build_vocabulary,
    chi_square_2x2,
    persist_enrichment,
)
from combo_discovery.corpus.names import normalize_card_name, pair_hash
from combo_discovery.corpus.spellbook import VintageLegality
from combo_discovery.store import ExperimentStore

CARDS = [
    (1, "Card One"), (2, "Card Two"), (3, "Card Three"),
    (4, "Card Four"), (5, "Card Five"), (6, "Card Six"),
]
PREDICATES = {
    1: ["TAPS_COST", "COPIES_CREATURE"],
    2: ["UNTAPS", "ETB_TRIGGER"],
    3: ["PRODUCES_MANA"],
    4: ["PRODUCES_MANA", "STORM"],
    5: ["DRAWS"],
    6: ["TAPS_COST"],
}
KNOWN_PAIRS = [("Card One", "Card Two"), ("Card Three", "Card Four")]
BACKGROUND = [(1, 4), (3, 6), (2, 5), (5, 6)]
PERMISSIVE = VintageLegality.permissive()
BANNED = VintageLegality(banned=frozenset({normalize_card_name("Banned Card")}))

_P = math.erfc(math.sqrt(0.375 / 2.0))  # p for chi2 = 0.375, 1 df


def _seed_db(path: Path, *, extra_banned_pair: bool = False) -> Path:
    ExperimentStore(path).close()
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO import_runs (import_id, started_at, scryfall_source) "
            "VALUES ('imp1', '2026-01-01T00:00:00+00:00', 'forge_script')"
        )
        conn.execute(
            "INSERT INTO import_runs (import_id, started_at, scryfall_source) "
            "VALUES ('sb1', '2026-01-01T00:00:00+00:00', 'spellbook')"
        )
        cards = list(CARDS)
        if extra_banned_pair:
            cards.append((7, "Banned Card"))
        conn.executemany(
            "INSERT INTO cards (id, import_id, file_sha256, name, normalized_name) "
            "VALUES (?, 'imp1', 'sha', ?, ?)",
            [(cid, name, normalize_card_name(name)) for cid, name in cards],
        )
        rows = []
        for cid, predicates in PREDICATES.items():
            for predicate in predicates:
                rows.append(("imp1", cid, 0, predicate, "{}", "[]", 1.0))
        if extra_banned_pair:
            rows.append(("imp1", 7, 0, "TAPS_COST", "{}", "[]", 1.0))
        conn.executemany(
            "INSERT INTO card_predicates (import_id, card_id, face_index, predicate, "
            "params_json, evidence_json, confidence) VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        # A qualifier-bearing card (not used by any pair).
        conn.execute(
            "INSERT INTO cards (id, import_id, file_sha256, name, normalized_name) "
            "VALUES (8, 'imp1', 'sha', 'Qualified Card', 'qualified card')"
        )
        conn.executemany(
            "INSERT INTO card_predicates (import_id, card_id, face_index, predicate, "
            "params_json, evidence_json, confidence) VALUES ('imp1', 8, 0, ?, ?, '[]', 1.0)",
            [
                ("PRODUCES_MANA", json.dumps({"color": "B", "x": True})),
                ("UNTAPS", json.dumps({"target": {"alternatives": [
                    {"types": ["CREATURE"], "controller": "you"}]}})),
            ],
        )

        known = list(KNOWN_PAIRS)
        if extra_banned_pair:
            known.append(("Card Five", "Banned Card"))
        combo_ids = []
        for index, (name_a, name_b) in enumerate(known, start=101):
            combo_ids.append((index, name_a, name_b))
            conn.execute(
                "INSERT INTO known_combos (id, source, source_id, source_version, "
                "fetched_at, n_uses, n_requires, n_produces, import_id) "
                "VALUES (?, 'commander_spellbook', ?, 'test', "
                "'2026-01-01T00:00:00+00:00', 2, 0, 1, 'sb1')",
                (index, f"k-{index}"),
            )
            conn.execute(
                "INSERT INTO known_combo_cards (combo_id, role, raw_name, normalized_name, "
                "quantity, import_id) VALUES (?, 'use', ?, ?, 1, 'sb1')",
                (index, name_a, normalize_card_name(name_a)),
            )
            conn.execute(
                "INSERT INTO known_combo_cards (combo_id, role, raw_name, normalized_name, "
                "quantity, import_id) VALUES (?, 'use', ?, ?, 1, 'sb1')",
                (index, name_b, normalize_card_name(name_b)),
            )
            conn.execute(
                "INSERT INTO known_combo_pairs (combo_id, pair_hash, is_full_variant, "
                "source, import_id) VALUES (?, ?, 1, 'commander_spellbook', 'sb1')",
                (index, pair_hash(normalize_card_name(name_a), normalize_card_name(name_b))),
            )

        # A pattern + hypothesis for the per-pattern view.
        conn.execute(
            "INSERT INTO patterns (id, name, description, pattern_json, version) "
            "VALUES (1, 'infinite_etb_loop', 'test', '{}', 1)"
        )
        conn.execute(
            "INSERT INTO combo_hypotheses (import_id, pattern_id, card_ids_json, "
            "mechanism, score, status, created_at) "
            "VALUES ('imp1', 1, '[1, 2]', 'loop', 0.9, 'proposed', "
            "'2026-01-01T00:00:00+00:00')"
        )
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture
def enrichment_db(tmp_path: Path) -> Path:
    return _seed_db(tmp_path / "enrich.db")


@pytest.fixture
def vintage_db(tmp_path: Path) -> Path:
    return _seed_db(tmp_path / "vintage.db", extra_banned_pair=True)


def _report(path: Path, **kwargs):
    kwargs.setdefault("min_support", 1)
    store = ExperimentStore(path)
    try:
        return analyze_motifs(
            store,
            vintage=PERMISSIVE,
            background_pairs=BACKGROUND,
            **kwargs,
        )
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Statistics core
# ---------------------------------------------------------------------------


class TestChiSquare:
    def test_hand_computed(self):
        chi2, p_value, low_expected = chi_square_2x2(1, 1, 1, 3)
        assert chi2 == pytest.approx(0.375)
        assert p_value == pytest.approx(math.erfc(math.sqrt(0.375 / 2.0)))
        assert low_expected is True

    def test_degenerate_margin(self):
        chi2, p_value, low = chi_square_2x2(0, 0, 3, 4)
        assert chi2 == 0.0 and p_value == 1.0 and low is True


class TestVocabulary:
    def test_top_tokens_by_card_frequency(self):
        signatures = {1: frozenset({"A", "B"}), 2: frozenset({"A"}), 3: frozenset({"C"})}
        assert build_vocabulary(signatures, 10) == ("A", "B", "C")
        assert build_vocabulary(signatures, 1) == ("A",)


# ---------------------------------------------------------------------------
# Provider abstraction
# ---------------------------------------------------------------------------


class _FakeProvider:
    name = "fake"

    def __init__(self, signatures):
        self._signatures = signatures

    def card_signatures(self):
        return {cid: frozenset(tokens) for cid, tokens in self._signatures.items()}


class TestProvider:
    def test_predicate_provider_tokens_and_qualifiers(self, enrichment_db):
        store = ExperimentStore(enrichment_db)
        try:
            signatures = PredicateSignatureProvider(store._conn, "imp1").card_signatures()
        finally:
            store.close()
        assert signatures[1] == frozenset({"TAPS_COST", "COPIES_CREATURE"})
        assert signatures[4] == frozenset({"PRODUCES_MANA", "STORM"})
        qualified = signatures[8]
        assert {"PRODUCES_MANA:B", "PRODUCES_MANA:x", "UNTAPS:creature", "UNTAPS:you"} <= qualified

    def test_fake_provider_slots_in(self, enrichment_db):
        provider = _FakeProvider({1: {"X"}, 2: {"Y"}})
        report = _report(enrichment_db, provider=provider)
        assert report.provider_name == "fake"
        stat = report.stats_by_motif["X~Y"]
        assert stat.n_known == 1 and stat.n_background == 0
        assert math.isinf(stat.lift)


# ---------------------------------------------------------------------------
# Enrichment
# ---------------------------------------------------------------------------


class TestEnrichment:
    def test_hand_computed_lifts(self, enrichment_db):
        report = _report(enrichment_db)
        assert report.n_known_total == 2
        assert report.n_background_total == 4
        assert report.provider_name == "card_predicates"

        pair_motif = report.stats_by_motif["COPIES_CREATURE|TAPS_COST"]
        assert pair_motif.kind == "pair"
        assert (pair_motif.n_known, pair_motif.n_background) == (1, 1)
        assert pair_motif.lift == pytest.approx(2.0)
        assert pair_motif.chi_square == pytest.approx(0.375)
        assert pair_motif.p_value == pytest.approx(_P)
        assert pair_motif.low_expected is True

        single = report.stats_by_motif["TAPS_COST"]
        assert single.kind == "single"
        assert (single.n_known, single.n_background) == (1, 3)
        assert single.lift == pytest.approx(0.5 / 0.75)
        assert single.lift < 1.0

        cross = report.stats_by_motif["TAPS_COST~UNTAPS"]
        assert cross.kind == "card_pair"
        assert cross.tokens == ("TAPS_COST", "UNTAPS")
        assert (cross.n_known, cross.n_background) == (1, 0)
        assert math.isinf(cross.lift)

        neutral = report.stats_by_motif["PRODUCES_MANA"]
        assert neutral.lift == pytest.approx(1.0)

    def test_enriched_and_anti_enriched_rankings(self, enrichment_db):
        report = _report(enrichment_db)
        enriched = {s.motif for s in report.top_enriched(50)}
        anti = {s.motif for s in report.top_anti_enriched(50)}
        assert "COPIES_CREATURE|TAPS_COST" in enriched
        assert "TAPS_COST" in anti
        assert "TAPS_COST" not in enriched

    def test_support_filter(self, enrichment_db):
        with_support = _report(enrichment_db, min_support=1)
        assert "COPIES_CREATURE|TAPS_COST" in with_support.stats_by_motif
        high_support = _report(enrichment_db, min_support=2)
        assert "COPIES_CREATURE|TAPS_COST" not in high_support.stats_by_motif
        assert "DRAWS" not in with_support.stats_by_motif  # 0 known hits

    def test_pattern_view_joins_hypotheses(self, enrichment_db):
        report = _report(enrichment_db)
        assert "infinite_etb_loop" in report.pattern_view
        by_motif = {item["motif"]: item for item in report.pattern_view["infinite_etb_loop"]}
        assert "TAPS_COST~UNTAPS" in by_motif
        assert by_motif["TAPS_COST~UNTAPS"]["count"] == 1
        lift = by_motif["TAPS_COST~UNTAPS"]["lift"]
        assert lift == "inf" or math.isinf(lift)

    def test_probe_motif_union_containment(self, enrichment_db):
        # The infinite-loop union spans both cards; counted as a probe motif.
        probe = ("TAPS_COST", "COPIES_CREATURE", "UNTAPS", "ETB_TRIGGER")
        report = _report(enrichment_db, probe_motifs=[probe])
        stat = report.stats_by_motif["COPIES_CREATURE|ETB_TRIGGER|TAPS_COST|UNTAPS"]
        assert stat.kind == "card_pair"
        assert stat.n_known == 1  # Card One + Card Two
        assert stat.n_background == 0
        assert math.isinf(stat.lift)

    def test_probe_motif_filters_by_support(self, enrichment_db):
        probe = ("TAPS_COST", "COPIES_CREATURE", "UNTAPS", "ETB_TRIGGER")
        report = _report(enrichment_db, probe_motifs=[probe], min_support=2)
        assert "COPIES_CREATURE|ETB_TRIGGER|TAPS_COST|UNTAPS" not in report.stats_by_motif

    def test_lookup_specific_motifs(self, enrichment_db):
        report = _report(enrichment_db)
        found = report.lookup(["TAPS_COST", "not a motif"])
        assert [s.motif for s in found] == ["TAPS_COST"]


class TestVintageFilter:
    def test_vintage_only_excludes_banned_card(self, vintage_db):
        store = ExperimentStore(vintage_db)
        try:
            permissive = analyze_motifs(
                store, vintage=PERMISSIVE, background_pairs=BACKGROUND, min_support=1
            )
            banned = analyze_motifs(
                store, vintage=BANNED, background_pairs=BACKGROUND, min_support=1
            )
        finally:
            store.close()
        assert permissive.n_known_total == 3
        assert banned.n_known_total == 2

    def test_all_includes_everything(self, vintage_db):
        store = ExperimentStore(vintage_db)
        try:
            report = analyze_motifs(
                store, vintage=BANNED, vintage_only=False,
                background_pairs=BACKGROUND, min_support=1,
            )
        finally:
            store.close()
        assert report.n_known_total == 3


# ---------------------------------------------------------------------------
# Persistence + append-only discipline
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_round_trip(self, enrichment_db):
        store = ExperimentStore(enrichment_db)
        try:
            report = analyze_motifs(
                store, vintage=PERMISSIVE, background_pairs=BACKGROUND, min_support=1
            )
            run_id = persist_enrichment(store, report, params={"seed": 0}, notes="test")
            run = store._conn.execute(
                "SELECT tokens_json, params_json FROM motif_runs WHERE id = ?", (run_id,)
            ).fetchone()
            assert run is not None and json.loads(run["params_json"])["seed"] == 0
            rows = store._conn.execute(
                "SELECT motif, kind, lift FROM motif_enrichment WHERE run_id = ?", (run_id,)
            ).fetchall()
            assert len(rows) == len(report.stats)
            by_motif = {r["motif"]: r for r in rows}
            assert by_motif["COPIES_CREATURE|TAPS_COST"]["kind"] == "pair"
            assert by_motif["COPIES_CREATURE|TAPS_COST"]["lift"] == pytest.approx(2.0)
        finally:
            store.close()

    def test_rerun_appends_not_replaces(self, enrichment_db):
        store = ExperimentStore(enrichment_db)
        try:
            report = analyze_motifs(
                store, vintage=PERMISSIVE, background_pairs=BACKGROUND, min_support=1
            )
            first = persist_enrichment(store, report)
            second = persist_enrichment(store, report)
            assert first != second
            assert store._conn.execute("SELECT COUNT(*) FROM motif_runs").fetchone()[0] == 2
            before = store._conn.execute(
                "SELECT COUNT(*) FROM motif_enrichment WHERE run_id = ?", (first,)
            ).fetchone()[0]
            assert before == len(report.stats)
        finally:
            store.close()

    def test_export_jsonl_includes_new_tables(self, enrichment_db, tmp_path):
        store = ExperimentStore(enrichment_db)
        try:
            report = analyze_motifs(
                store, vintage=PERMISSIVE, background_pairs=BACKGROUND, min_support=1
            )
            persist_enrichment(store, report)
            out = tmp_path / "enrichment.jsonl"
            store.export_jsonl("motif_enrichment", out)
            assert len(out.read_text().splitlines()) == len(report.stats)
        finally:
            store.close()

    def test_module_has_no_mutating_sql(self):
        import re

        source = (
            Path(__file__).resolve().parent.parent
            / "src" / "combo_discovery" / "analysis" / "enrichment.py"
        ).read_text(encoding="utf-8")
        assert not re.search(r"\b(UPDATE|DELETE)\s+[A-Za-z_]", source, re.IGNORECASE)
