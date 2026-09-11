"""Tests for the TUI's data + configuration binding (no Textual required)."""

from __future__ import annotations

import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from combo_discovery.generated import forge_env_pb2 as pb
from combo_discovery.runner import GameResult
from combo_discovery.store import ExperimentStore
from combo_discovery.tui import data as tdata
from combo_discovery.tui.data import (
    BADGE_LABELS,
    CancellablePolicy,
    RunCancelled,
    RunConfig,
    StoreBinding,
    WorkerProbe,
    add_card_to_scratch_deck,
    badge_color,
    best_contiguous_run,
    build_pool,
    build_run_config,
    clear_scratch_deck,
    combo_badge,
    discover_decks,
    effective_status,
    effects_count,
    evidence_summary,
    fmt_duration,
    fmt_ms,
    is_basic_land,
    known_legalities,
    known_produces,
    next_status,
    novelty_badge,
    pair_hash_for_names,
    parse_dck,
    parse_json_list,
    pattern_module_name,
    pool_snapshot,
    pretty_json,
    probe_workers,
    read_scratch_deck,
    render_dck,
    resolve_policy,
    scratch_deck_path,
    scratch_deck_summary,
    short_id,
    status_color,
    write_scratch_deck,
)

DECK_A = ("goldfish_A", "/decks/goldfish_A.dck")
DECK_B = ("goldfish_B", "/decks/goldfish_B.dck")


# ---------------------------------------------------------------------------
# run configuration
# ---------------------------------------------------------------------------


class TestBuildRunConfig:
    def test_valid(self):
        config, errors = build_run_config(DECK_A, DECK_B, "goldfish", "1", "20", "4", "50060")
        assert errors == []
        assert isinstance(config, RunConfig)
        assert config.seeds == list(range(1, 21))
        assert config.deck_pair == (DECK_A, DECK_B)
        assert config.total == 20

    def test_seeds_are_start_and_count(self):
        config, _ = build_run_config(DECK_A, DECK_B, "default", 100, 3, 8, 50060)
        assert config is not None
        assert config.seeds == [100, 101, 102]

    def test_experiment_config_blob_is_jsonable(self):
        config, _ = build_run_config(DECK_A, DECK_B, "default", 0, 1, 1, 50060)
        assert config is not None
        blob = config.experiment_config()
        assert blob["policy"] == "default"
        assert blob["decks"] == [list(DECK_A), list(DECK_B)]
        json.dumps(blob)  # must be serialisable

    def test_missing_decks(self):
        config, errors = build_run_config(None, None, "default", 1, 1, 1, 50060)
        assert config is None
        assert any("seat A" in e for e in errors)
        assert any("seat B" in e for e in errors)

    def test_bad_policy(self):
        config, errors = build_run_config(DECK_A, DECK_B, "mcts", 1, 1, 1, 50060)
        assert config is None
        assert any("policy" in e for e in errors)

    def test_non_numeric_and_out_of_range(self):
        config, errors = build_run_config(DECK_A, DECK_B, "default", "x", "0", "0", "99999")
        assert config is None
        joined = " ".join(errors)
        assert "seed start must be a number" in joined
        assert "seed count must be >= 1" in joined
        assert "workers must be >= 1" in joined
        assert "base port must be between" in joined

    def test_negative_values(self):
        config, errors = build_run_config(DECK_A, DECK_B, "default", -1, 5, 1, 50060, max_turns=-2)
        assert config is None
        assert any("seed start must be >= 0" in e for e in errors)
        assert any("max turns must be >= 0" in e for e in errors)

    def test_empty_strings_are_errors(self):
        config, errors = build_run_config(DECK_A, DECK_B, "default", "", "", "", "")
        assert config is None
        assert errors  # all four numeric fields missing

    def test_bool_is_rejected_as_number(self):
        config, errors = build_run_config(DECK_A, DECK_B, "default", True, True, True, True)
        assert config is None
        assert errors


# ---------------------------------------------------------------------------
# decks, policies, cancellation
# ---------------------------------------------------------------------------


class TestDecksAndPolicies:
    def test_discover_decks_sorted_and_filtered(self, tmp_path):
        (tmp_path / "b.deck").write_text("not a dck")
        (tmp_path / "Beta.dck").write_text("x")
        (tmp_path / "alpha.dck").write_text("x")
        found = discover_decks(tmp_path)
        assert [name for name, _ in found] == ["alpha", "Beta"]

    def test_discover_decks_missing_dir(self, tmp_path):
        assert discover_decks(tmp_path / "nope") == []

    def test_resolve_policy(self):
        assert resolve_policy("default") is not None
        goldfish = resolve_policy("goldfish")
        assert goldfish.__class__.__name__ == "GoldfishPolicy"

    def test_cancellable_policy_raises(self):
        event = threading.Event()
        policy = CancellablePolicy(lambda ctx: ("option_id", 0), event)
        assert policy(object()) == ("option_id", 0)
        event.set()
        with pytest.raises(RunCancelled):
            policy(object())

    def test_cancellable_policy_forwards_new_game(self):
        calls = []
        inner = type("P", (), {"new_game": lambda self: calls.append(1)})()
        policy = CancellablePolicy(inner, threading.Event())
        policy.new_game()
        assert calls == [1]


# ---------------------------------------------------------------------------
# worker probing
# ---------------------------------------------------------------------------


class _FakeClient:
    behaviour: dict[int, Exception | None] = {}

    def __init__(self, host="localhost", port=0, timeout=30.0):
        self.port = port
        self.closed = False

    def connect(self):
        result = self.behaviour.get(self.port)
        if result is not None:
            raise result
        return object()

    def close(self):
        self.closed = True


class TestWorkerProbe:
    def test_best_contiguous_run(self):
        assert best_contiguous_run([50060, 50061, 50062, 50070]) == (50060, 3)
        assert best_contiguous_run([]) is None
        assert best_contiguous_run([5]) == (5, 1)
        assert best_contiguous_run([1, 3, 4, 5, 9]) == (3, 3)

    def test_probe_workers(self, monkeypatch):
        _FakeClient.behaviour = {
            50061: tdata.HarnessConnectionError("down"),
            50062: tdata.ProtocolMismatchError("mismatch"),
        }
        monkeypatch.setattr(tdata, "ForgeEnvClient", _FakeClient)
        probe = probe_workers("localhost", 50060, 4)
        assert probe.reachable == [50060, 50063]
        assert probe.ok_count == 2 and probe.total == 4
        assert probe.status_for(50062) == "unreachable"

    def test_build_pool_none_probe_raises(self):
        with pytest.raises(tdata.HarnessConnectionError):
            build_pool(None)

    def test_build_pool_no_reachable_raises(self):
        probe = WorkerProbe("localhost", 50060, [(50060, False, "unreachable")])
        with pytest.raises(tdata.HarnessConnectionError):
            build_pool(probe)

    def test_build_pool_uses_longest_run(self, monkeypatch):
        captured = {}

        class FakePool:
            def __init__(self, n_workers=None, base_port=None, host="localhost", config=None):
                captured["n_workers"] = n_workers
                captured["base_port"] = base_port

        monkeypatch.setattr(tdata, "WorkerPool", FakePool)
        probe = WorkerProbe(
            "localhost",
            50060,
            [(50060, True, "reachable"), (50061, True, "reachable"), (50065, True, "reachable")],
        )
        build_pool(probe)
        assert captured == {"n_workers": 2, "base_port": 50060}

    def test_pool_snapshot_none_and_rows(self):
        assert pool_snapshot(None) == []

        class FakePool:
            n_workers = 2
            base_port = 50060
            _healthy = [True, False]
            _fail_counts = [0, 3]

        rows = pool_snapshot(FakePool())
        assert rows == [
            {"index": 0, "port": 50060, "healthy": True, "failures": 0},
            {"index": 1, "port": 50061, "healthy": False, "failures": 3},
        ]

    def test_no_harness_message_mentions_ports(self):
        probe = WorkerProbe("localhost", 50060, [(p, False, "x") for p in (50060, 50061)])
        message = tdata.no_harness_message(probe)
        assert "No live harness" in message
        assert "50060" in message and "50061" in message


# ---------------------------------------------------------------------------
# small formatting helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_effects_count(self):
        assert effects_count(None) is None
        assert effects_count(4) == 4
        assert effects_count(json.dumps([1, 2, 3])) == 3
        assert effects_count(json.dumps({"a": 1})) == 1
        assert effects_count("7") == 7
        assert effects_count("nonsense") is None

    def test_parse_json_list(self):
        assert parse_json_list(json.dumps(["a", "b"])) == ["a", "b"]
        assert parse_json_list(None) == []
        assert parse_json_list("not json") == []

    def test_pretty_json(self):
        assert pretty_json('{"a": 1}') == '{\n  "a": 1\n}'
        assert pretty_json(None) == ""

    def test_short_id_and_durations(self):
        assert short_id("abcdefghijkl") == "abcdefgh"
        assert short_id(None) == "—"
        assert fmt_ms(500) == "500ms"
        assert fmt_ms(1500) == "1.5s"
        assert fmt_duration(5) == "5s"
        assert fmt_duration(65) == "1m 05s"
        assert fmt_duration(3700) == "1h 01m"


class TestWorkerTone:
    """Idle unreachable ports are amber; failed/excluded workers are red."""

    @staticmethod
    def _style_for(text, word):
        index = str(text).index(word)
        styles = [
            span.style
            for span in text.spans
            if span.start <= index < span.end
        ]
        assert styles, f"no styled span covered {word!r}"
        return styles[0]

    @staticmethod
    def _app(pool, probe):
        return SimpleNamespace(pool=pool, probe=probe)

    def test_unreachable_ports_use_warn(self):
        from combo_discovery.tui import theme as pal
        from combo_discovery.tui.views.experiments import ExperimentsView

        probe = WorkerProbe(
            "localhost",
            50060,
            [(50060, False, "unreachable"), (50061, True, "reachable")],
        )
        text = ExperimentsView._worker_rows_text(self._app(None, probe))
        assert self._style_for(text, "unreachable") == pal.WARN

    def test_excluded_pool_worker_stays_error(self):
        from combo_discovery.tui import theme as pal
        from combo_discovery.tui.views.experiments import ExperimentsView

        pool = SimpleNamespace(
            n_workers=1, base_port=50060, _healthy=[False], _fail_counts=[3]
        )
        text = ExperimentsView._worker_rows_text(self._app(pool, None))
        assert self._style_for(text, "excluded") == pal.ERR


# ---------------------------------------------------------------------------
# store binding
# ---------------------------------------------------------------------------


def _seed_store(path) -> str:
    store = ExperimentStore(path)
    run_id = store.start_experiment(
        engine_commit="deadbeef",
        proto_version=6,
        policy_version="default-v1",
        model_version="test-model",
        config={"policy": "default"},
    )
    result = GameResult(
        game_id=42, seed=7, winner=0, turns=5, n_events=2,
        duration_s=0.25, outcome=pb.OUTCOME_WIN, reason="lethal",
    )
    game_row = store.record_game(
        run_id, result, [("a", "/a.dck"), ("b", "/b.dck")], 7,
        [pb.PLAYER_TYPE_REMOTE, pb.PLAYER_TYPE_REMOTE], 0,
    )
    store.record_events(game_row, [
        pb.GameEvent(seq=1, type="TurnStarted", turn=1, phase="Main1", player=0),
        pb.GameEvent(seq=2, type="GameOver", turn=5, phase="Main1", player=0, detail_raw="over"),
    ])
    store.close()

    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO candidates (run_id, card_names_json, status, evidence_json, created_at)"
        " VALUES (?,?,?,?,datetime('now'))",
        (run_id, json.dumps(["A", "B"]), "proposed", json.dumps({"seed": 7})),
    )
    conn.execute(
        "INSERT INTO adjudications (candidate_id, verdict, reviewer, notes, created_at)"
        " VALUES (1,'inconclusive','auto','needs isolation', datetime('now'))"
    )
    conn.commit()
    conn.close()
    return run_id


class TestStoreBinding:
    def test_empty_database_is_graceful(self, tmp_path):
        binding = StoreBinding(tmp_path / "empty.sqlite")
        assert binding.counts() == {
            "experiments": 0, "games": 0, "events": 0, "candidates": 0,
            "hypotheses": 0,
        }
        assert binding.list_experiments() == []
        assert binding.list_candidates() == []
        assert binding.games_for_run("nope") == []
        assert binding.event_tail() == []
        # Schema v2 always creates the (empty) corpus tables, so the binding
        # reports a usable schema with zero rows rather than "no table".
        assert binding.card_schema() is not None
        assert binding.list_cards() == []
        assert binding.card_count() == 0
        binding.close()

    def test_cards_table_dropped_is_graceful(self, tmp_path):
        binding = StoreBinding(tmp_path / "empty.sqlite")
        binding.close()
        conn = sqlite3.connect(tmp_path / "empty.sqlite")
        conn.execute("DROP TABLE cards")
        conn.commit()
        conn.close()
        binding = StoreBinding(tmp_path / "empty.sqlite")
        assert binding.card_schema() is None
        assert binding.list_cards() == []
        assert binding.card_count() == 0
        binding.close()

    def test_experiment_and_game_queries(self, tmp_path):
        path = tmp_path / "exp.sqlite"
        run_id = _seed_store(path)
        binding = StoreBinding(path)

        experiments = binding.list_experiments()
        assert len(experiments) == 1
        assert experiments[0]["id"] == run_id
        assert experiments[0]["game_count"] == 1

        detail = binding.experiment(run_id)
        assert detail is not None and detail["policy_version"] == "default-v1"

        games = binding.games_for_run(run_id)
        assert len(games) == 1
        assert games[0]["seed"] == 7
        assert games[0]["outcome"] == "OUTCOME_WIN"
        assert binding.completed_seeds(run_id) == {7}

        events = binding.event_tail()
        assert [e["type"] for e in events] == ["TurnStarted", "GameOver"]
        assert events[1]["seed"] == 7
        binding.close()

    def test_candidate_queries(self, tmp_path):
        path = tmp_path / "exp.sqlite"
        run_id = _seed_store(path)
        binding = StoreBinding(path)

        candidates = binding.list_candidates()
        assert len(candidates) == 1
        assert candidates[0]["status"] == "proposed"
        assert candidates[0]["verdict_count"] == 1

        detail = binding.candidate(candidates[0]["id"])
        assert detail is not None
        assert parse_json_list(detail["card_names_json"]) == ["A", "B"]

        verdicts = binding.adjudications(candidates[0]["id"])
        assert verdicts[0]["verdict"] == "inconclusive"
        assert verdicts[0]["reviewer"] == "auto"
        assert binding.candidate(999) is None
        binding.close()

    def test_cards_table_detected_and_queried(self, tmp_path):
        path = tmp_path / "exp.sqlite"
        _seed_store(path)
        conn = sqlite3.connect(path)
        # The store creates the importer-owned cards table at schema v2; replace
        # it with the loose legacy shape this test is probing tolerance for.
        conn.execute("DROP TABLE IF EXISTS cards")
        conn.execute(
            "CREATE TABLE cards (id INTEGER PRIMARY KEY, name TEXT, type_line TEXT,"
            " mana_cost TEXT, oracle_text TEXT, effects_json TEXT)"
        )
        conn.executemany(
            "INSERT INTO cards (name, type_line, mana_cost, oracle_text, effects_json)"
            " VALUES (?,?,?,?,?)",
            [
                ("Brainstorm", "Instant", "{U}", "Draw three.", json.dumps([{}, {}])),
                ("Black Lotus", "Artifact", "{0}", "Add three mana.", json.dumps([{}])),
            ],
        )
        conn.commit()
        conn.close()

        binding = StoreBinding(path)
        schema = binding.card_schema()
        assert schema is not None and schema.name == "name"
        assert binding.card_count() == 2
        assert binding.card_count("brain") == 1

        cards = binding.list_cards("brain")
        assert len(cards) == 1
        card = cards[0]
        assert card["name"] == "Brainstorm"
        assert card["effects"] == 2
        assert card["mana"] == "{U}"

        by_id = binding.card(card["id"])
        assert by_id is not None and by_id["name"] == "Brainstorm"
        binding.close()

    def test_cards_table_without_name_is_unsupported(self, tmp_path):
        path = tmp_path / "exp.sqlite"
        _seed_store(path)
        conn = sqlite3.connect(path)
        conn.execute("DROP TABLE IF EXISTS cards")
        conn.execute("CREATE TABLE cards (id INTEGER PRIMARY KEY, weird TEXT)")
        conn.commit()
        conn.close()
        binding = StoreBinding(path)
        assert binding.card_schema() is None
        assert binding.list_cards() == []
        binding.close()


# ---------------------------------------------------------------------------
# research scratch deck (.dck)
# ---------------------------------------------------------------------------


class TestScratchDeck:
    def test_add_merges_and_increments(self, tmp_path):
        path = scratch_deck_path(tmp_path)
        assert add_card_to_scratch_deck(path, "Brainstorm") == (1, 1)
        assert add_card_to_scratch_deck(path, "brainstorm") == (2, 2)
        assert add_card_to_scratch_deck(path, "Island") == (1, 3)

        name, entries = read_scratch_deck(path)
        assert name == "Research Scratch"
        assert entries == [(2, "Brainstorm"), (1, "Island")]
        assert scratch_deck_summary(path) == (2, 3)

        text = path.read_text(encoding="utf-8")
        assert "[metadata]" in text
        assert "Name=Research Scratch" in text
        assert "[Main]" in text
        assert "2 Brainstorm" in text
        assert "1 Island" in text

    def test_parse_render_roundtrip(self):
        text = "[metadata]\nName=Custom\n\n[Main]\n4 Lightning Bolt\n2 Island\n"
        name, entries = parse_dck(text)
        assert name == "Custom"
        assert entries == [(4, "Lightning Bolt"), (2, "Island")]
        assert render_dck(name, entries) == text

    def test_clear_keeps_file_and_discovery(self, tmp_path):
        path = scratch_deck_path(tmp_path)
        add_card_to_scratch_deck(path, "Brainstorm")
        clear_scratch_deck(path)
        assert path.exists()
        assert read_scratch_deck(path)[1] == []
        assert "research_scratch" in [name for name, _ in discover_decks(tmp_path)]

    def test_created_scratch_is_discovered(self, tmp_path):
        write_scratch_deck(scratch_deck_path(tmp_path), [("1", "Brainstorm")])
        names = [name for name, _ in discover_decks(tmp_path)]
        assert "research_scratch" in names


class TestStatusCycle:
    def test_cycle_order(self):
        assert next_status("proposed") == "verified"
        assert next_status("verified") == "refuted"
        assert next_status("refuted") == "inconclusive"
        assert next_status("inconclusive") == "proposed"
        assert next_status("nonsense") == "proposed"

    def test_status_colors(self):
        assert status_color("proposed")
        assert status_color("verified")
        assert status_color("refuted")
        assert status_color("inconclusive")
        assert status_color("nonsense")

    def test_is_basic_land(self):
        assert is_basic_land("Island")
        assert is_basic_land("Snow-Covered Forest", "Basic Snow Land — Forest")
        assert not is_basic_land("Brainstorm", "Instant")
        assert not is_basic_land(None)


# ---------------------------------------------------------------------------
# combo hypotheses (read-only queries)
# ---------------------------------------------------------------------------


class TestHypothesisQueries:
    def test_list_sorted_and_decorated(self, ontology_db):
        binding = StoreBinding(ontology_db)
        rows = binding.list_hypotheses()
        assert [row["id"] for row in rows] == [1, 2, 4, 3]  # score desc
        assert rows[0]["pattern"] == "infinite_etb_loop"
        assert rows[0]["card_names"] == ["Kiki-Jiki, Mirror Breaker", "Pestermite"]
        assert rows[0]["cards"][0]["id"] == 1
        assert rows[0]["effective_status"] == "proposed"
        assert binding.hypothesis_total() == 4
        binding.close()

    def test_pattern_filter(self, ontology_db):
        binding = StoreBinding(ontology_db)
        rows = binding.list_hypotheses(pattern_id=1)
        assert {row["id"] for row in rows} == {1, 2, 4}
        assert binding.hypothesis_total(pattern_id=1) == 3
        binding.close()

    def test_search_by_card_name(self, ontology_db):
        binding = StoreBinding(ontology_db)
        rows = binding.list_hypotheses(search="kiki")
        assert {row["id"] for row in rows} == {1, 4}
        assert binding.hypothesis_total(search="Pestermite") == 2
        binding.close()

    def test_for_card_excludes_refuted(self, ontology_db):
        binding = StoreBinding(ontology_db)
        assert [row["id"] for row in binding.hypotheses_for_card(1)] == [1]
        assert {row["id"] for row in binding.hypotheses_for_card(2)} == {1, 2}
        binding.close()

    def test_hypothesis_detail(self, ontology_db):
        binding = StoreBinding(ontology_db)
        hypothesis = binding.hypothesis(1)
        assert hypothesis is not None
        assert hypothesis["pattern"] == "infinite_etb_loop"
        assert hypothesis["cards"][0]["name"] == "Kiki-Jiki, Mirror Breaker"
        assert binding.hypothesis(999) is None
        binding.close()

    def test_effective_status_follows_adjudication(self, ontology_db):
        store = ExperimentStore(ontology_db)
        row_id = store.record_adjudication(
            1, "verified", reviewer="tui", notes="status cycled in TUI"
        )
        assert row_id > 0
        store.close()

        binding = StoreBinding(ontology_db)
        hypothesis = binding.hypothesis(1)
        assert hypothesis is not None
        assert hypothesis["latest_verdict"] == "verified"
        assert effective_status(hypothesis) == "verified"
        assert binding.adjudications(1)[0]["reviewer"] == "tui"
        binding.close()


# ---------------------------------------------------------------------------
# novelty badges + ground-truth reads (Card Lab)
# ---------------------------------------------------------------------------


class TestNoveltyBadge:
    def test_mapping_never_says_novel(self):
        assert novelty_badge("known") == "known"
        assert novelty_badge("contained") == "contained"
        assert novelty_badge("unknown") == "candidate"
        assert novelty_badge("unknown", observed=True) == "observed"
        assert "novel" not in BADGE_LABELS.values()
        assert badge_color("known") and badge_color("candidate")

    def test_pair_hash_normalizes_and_is_order_independent(self):
        a = pair_hash_for_names("Kiki-Jiki, Mirror Breaker", "Pestermite")
        b = pair_hash_for_names("pestermite", "kiki-jiki, mirror breaker")
        assert a == b and a

    def test_combo_badge(self):
        assert combo_badge(2, 0) == "exact pair"
        assert combo_badge(3, 0) == "part of 3-card combo"
        assert combo_badge(2, 1) == "part of 3-card combo"

    def test_known_produces_and_legalities(self):
        import json as _json

        produces = [{"feature": {"name": "Infinite combat phases"}}]
        assert known_produces(_json.dumps(produces)) == ["Infinite combat phases"]
        assert known_produces("garbage") == []
        assert known_legalities('{"vintage": true, "modern": false}') == {
            "vintage": True, "modern": False,
        }

    def test_evidence_summary(self):
        summary = evidence_summary(
            '[{"predicate": "TAPS_COST"}, {"predicate": "COPIES_CREATURE"},'
            ' {"type_verified": true, "copy_verified": false, "ability_linked": true,'
            ' "link_kind": "etb_chain", "detail": "ok"}]'
        )
        assert summary["predicates"] == ["COPIES_CREATURE", "TAPS_COST"]
        assert summary["verification"]["type_verified"] is True
        assert summary["verification"]["copy_verified"] is False
        assert summary["verification"]["link_kind"] == "etb_chain"

    def test_pattern_module_name(self):
        module = pattern_module_name("infinite_etb_loop")
        assert module.endswith("patterns.infinite_loop")
        assert "patterns" in pattern_module_name("unknown_pattern")


class TestCardLabQueries:
    def test_known_combo_counts_and_badges(self, lab_db):
        binding = StoreBinding(lab_db)
        normalized = "kiki jiki mirror breaker"
        assert binding.known_combo_total(normalized) == 3
        assert binding.known_combo_total(normalized, exact_only=True) == 2

        rows = binding.known_combos_for_card(normalized)
        badges = {row["id"]: row["badge"] for row in rows}
        assert badges == {101: "exact pair", 102: "exact pair", 103: "part of 3-card combo"}

        exact = binding.known_combos_for_card(normalized, exact_only=True)
        assert {row["id"] for row in exact} == {101, 102}
        row_101 = next(row for row in rows if row["id"] == 101)
        assert [p["name"] for p in row_101["partners"]] == ["Pestermite"]
        assert row_101["partners"][0]["id"] == 2
        assert row_101["produces"] == ["Infinite creature tokens with haste"]
        assert row_101["legalities"]["vintage"] is True
        binding.close()

    def test_known_pieces_resolve_local_ids(self, lab_db):
        binding = StoreBinding(lab_db)
        pieces = binding.known_pieces([103])
        names = {piece["name"] for piece in pieces[103]}
        assert {"Kiki-Jiki, Mirror Breaker", "Deceiver Exarch", "Third Piece"} <= names
        deceiver = next(piece for piece in pieces[103] if piece["name"] == "Deceiver Exarch")
        assert deceiver["id"] == 4
        binding.close()

    def test_ground_truth_states(self, lab_db):
        binding = StoreBinding(lab_db)
        known = pair_hash_for_names("Kiki-Jiki, Mirror Breaker", "Pestermite")
        contained = pair_hash_for_names("Kiki-Jiki, Mirror Breaker", "Deceiver Exarch")
        unknown = pair_hash_for_names("Kiki-Jiki, Mirror Breaker", "Splinter Twin")
        states = binding.ground_truth_states([known, contained, unknown])
        assert states[known] == "known"
        assert states[contained] == "contained"
        assert states[unknown] == "unknown"
        assert binding.observed_hashes([known, unknown]) == set()
        binding.close()

    def test_interaction_evidence_and_import_ids(self, lab_db):
        binding = StoreBinding(lab_db)
        evidence = binding.interaction_evidence(1, 2, "infinite_etb_loop")
        assert evidence is not None
        assert "TAPS_COST" in evidence["predicates"]
        assert evidence["verification"]["type_verified"] is True
        known_import, ontology_import = binding.latest_eval_import_ids()
        assert known_import == "sb1"
        assert ontology_import == "imp1"
        binding.close()

    def test_known_descriptions(self, lab_db):
        binding = StoreBinding(lab_db)
        descriptions = binding.known_descriptions([102])
        assert descriptions[102]["produces"] == ["Infinite combat phases"]
        assert "Fear of Missing Out" in descriptions[102]["description"]
        binding.close()

    def test_novelty_status_never_returns_novel(self, lab_db):
        from combo_discovery.evaluation import novelty_status

        store = ExperimentStore(lab_db)
        try:
            status = novelty_status(store, ("Kiki-Jiki, Mirror Breaker", "Splinter Twin"))
            assert status == "needs_second_source"
            assert status != "novel"
            known = novelty_status(store, ("Kiki-Jiki, Mirror Breaker", "Pestermite"))
            assert known == "known"
        finally:
            store.close()
