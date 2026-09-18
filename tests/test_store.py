import json
import re
import sqlite3
import threading
from pathlib import Path

import pytest

from combo_discovery import store as store_module
from combo_discovery.generated import forge_env_pb2 as pb
from combo_discovery.research_config import (
    DEFAULT_ENGINE_COMMIT,
    ResearchConfig,
    load_config,
)
from combo_discovery.runner import GameResult, run_game
from combo_discovery.store import DecisionRef, ExperimentStore

DECKS = [("a", "/tmp/a.dck"), ("b", "/tmp/b.dck")]
REMOTE = [pb.PLAYER_TYPE_REMOTE, pb.PLAYER_TYPE_REMOTE]


class FakeClient:
    """Minimal v6 fake: two PRIORITY decisions, then game over (3 events)."""

    def __init__(self):
        self.game_id = 42
        self.answered = 0
        self.stopped = None
        self.events = [
            pb.GameEvent(seq=1, type="TurnStarted", turn=1, phase="Main1", player=0,
                         detail_raw="start"),
            pb.GameEvent(seq=2, type="CardDrawn", turn=1, phase="Main1", player=0,
                         card_id=7, card_name="Forest", detail_raw="draw",
                         old_value=5, new_value=6, extra="library->hand"),
            pb.GameEvent(seq=3, type="GameOver", turn=2, phase="Main1", player=0,
                         detail_raw="over"),
        ]

    def start_game(self, decks, seed, player_types=None, **kw):
        self.answered = 0
        return self.game_id

    def is_game_over(self, game_id):
        return pb.GameOver(
            over=self.answered >= 2, winner=0, outcome=pb.OUTCOME_WIN, reason="lethal"
        )

    def get_decision(self, game_id):
        n = self.answered + 1
        return pb.DecisionRequest(
            game_id=game_id, decision_id=n, player=0, turn=n, phase="Main1",
            decision_type=pb.DECISION_TYPE_PRIORITY,
            options=[pb.Option(id=0, kind="pass")],
        )

    def submit_decision(self, game_id, decision_id, answer):
        e = self.events[self.answered]
        self.answered += 1
        return pb.StepResult(events=[e], game_over=self.answered >= 2)

    def drain_events(self, game_id, cursor=0):
        return list(self.events)

    def get_state(self, game_id):
        return pb.FullState(game_id=game_id, turn=1)

    def stop_game(self, game_id):
        self.stopped = game_id


def make_result(game_id=1, seed=1):
    return GameResult(
        game_id=game_id, seed=seed, winner=0, turns=2, n_events=3,
        duration_s=0.01, outcome=pb.OUTCOME_WIN, reason="lethal",
    )


def start_run(store, **overrides):
    meta = ResearchConfig().experiment_meta()
    meta.update(overrides)
    return store.start_experiment(config={"k": "v"}, **meta)


class TestSchema:
    def test_creation_is_idempotent(self, tmp_path):
        path = tmp_path / "exp.sqlite"
        s1 = ExperimentStore(path)
        s1.close()
        s2 = ExperimentStore(path)  # must not fail or duplicate version rows
        with s2._conn:
            tables = {
                r[0]
                for r in s2._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            versions = s2._conn.execute("SELECT version FROM schema_version").fetchall()
        s2.close()
        for t in (
            "schema_version", "experiments", "games", "decisions", "events",
            "candidates", "adjudications",
            "import_runs", "cards", "card_faces", "card_aliases",
            "card_scripts", "card_effects", "corpus_coverage",
            "patterns", "card_predicates", "interactions", "combo_hypotheses",
            "card_oracle_ids", "known_combos", "known_combo_cards",
            "known_combo_pairs", "known_aliases", "observed_decks",
            "observed_deck_cards", "observed_pairs",
            "evaluation_runs", "evaluation_results",
            "motif_runs", "motif_enrichment",
            "witness_runs", "witness_results", "witness_observations",
        ):
            assert t in tables
        indexes = sqlite3.connect(path).execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
        idx_names = {r[0] for r in indexes}
        assert {
            "idx_events_game", "idx_decisions_game", "idx_cards_name",
            "idx_predicates_pred", "idx_hypotheses_status_score",
            "idx_oracle_ids_oracle", "idx_known_pairs_hash", "idx_eval_results_scope",
            "idx_motif_enrichment_motif", "idx_witness_results_run",
            "idx_witness_results_verdict", "idx_witness_observations_run",
        } <= idx_names
        assert [tuple(r) for r in versions] == [
            (1,), (2,), (3,), (4,), (5,), (6,), (7,)
        ]

    def test_foreign_keys_enforced(self, tmp_path):
        store = ExperimentStore(tmp_path / "exp.sqlite")
        with pytest.raises(sqlite3.IntegrityError):
            store.record_game("no-such-run", make_result(), DECKS, 1, REMOTE, 0)
        run_id = start_run(store)
        game_row = store.record_game(run_id, make_result(), DECKS, 1, REMOTE, 0)
        with pytest.raises(sqlite3.IntegrityError):
            store.record_events(999999, [pb.GameEvent(seq=1, type="TurnStarted")])
        store.record_events(game_row, [pb.GameEvent(seq=1, type="TurnStarted")])
        store.close()

    def test_v5_database_migrates_to_v6(self, tmp_path, monkeypatch):
        path = tmp_path / "v5.sqlite"
        monkeypatch.setattr(store_module, "_SCHEMA_VERSION", 5)
        old = ExperimentStore(path)
        old.close()
        monkeypatch.setattr(store_module, "_SCHEMA_VERSION", 6)
        upgraded = ExperimentStore(path)
        tables = {
            r[0]
            for r in upgraded._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        versions = [
            tuple(r)
            for r in upgraded._conn.execute(
                "SELECT version FROM schema_version"
            ).fetchall()
        ]
        upgraded.close()
        assert "witness_runs" in tables
        assert "witness_results" in tables
        assert versions == [(1,), (2,), (3,), (4,), (5,), (6,)]

    def test_v7_migration_applies_to_fresh_and_existing_v6_db(self, tmp_path, monkeypatch):
        # Fresh DB: created through the full migration chain, so it is stamped
        # all the way to v7 and carries the v7 table/columns.
        fresh_path = tmp_path / "fresh.sqlite"
        fresh = ExperimentStore(fresh_path)
        fresh_columns = {
            r["name"] for r in fresh._conn.execute("PRAGMA table_info(witness_results)")
        }
        fresh_obs = {
            r["name"]
            for r in fresh._conn.execute("PRAGMA table_info(witness_observations)")
        }
        fresh_versions = [
            tuple(r) for r in fresh._conn.execute("SELECT version FROM schema_version")
        ]
        fresh.close()
        assert {"evidence_json", "diagnostics_json"} <= fresh_columns
        assert fresh_obs == {
            "id", "run_id", "iteration", "turn", "phase",
            "signature", "resources_json", "event_seq", "created_at",
        }
        assert fresh_versions == [(1,), (2,), (3,), (4,), (5,), (6,), (7,)]

        # Existing v6 DB: build one at v6, then upgrade through migration 7.
        old_path = tmp_path / "v6.sqlite"
        monkeypatch.setattr(store_module, "_SCHEMA_VERSION", 6)
        old = ExperimentStore(old_path)
        old_columns = {
            r["name"] for r in old._conn.execute("PRAGMA table_info(witness_results)")
        }
        old.close()
        assert "evidence_json" not in old_columns
        assert "diagnostics_json" not in old_columns

        monkeypatch.setattr(store_module, "_SCHEMA_VERSION", 7)
        upgraded = ExperimentStore(old_path)
        new_columns = {
            r["name"]
            for r in upgraded._conn.execute("PRAGMA table_info(witness_results)")
        }
        tables = {
            r[0]
            for r in upgraded._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        versions = [
            tuple(r)
            for r in upgraded._conn.execute("SELECT version FROM schema_version")
        ]
        upgraded.close()
        assert {"evidence_json", "diagnostics_json"} <= new_columns
        assert "witness_observations" in tables
        assert versions == [(1,), (2,), (3,), (4,), (5,), (6,), (7,)]


class TestWitnessPersistence:
    def test_witness_run_result_roundtrip(self, tmp_path):
        store = ExperimentStore(tmp_path / "w.sqlite")
        run_id = store.start_witness_run(
            engine_commit="abc", proto_version=7, policy_version="witness-v1",
            scenario_json='{"players": []}', seeds=[1, 2],
            params={"max_iterations": 4}, notes="n",
        )
        result_id = store.record_witness_result(
            run_id, candidate_kind="pair", candidate_key="42",
            card_names=["A", "B"], verdict="loops", infinite=True,
            iterations=3, signature=["s1", "s2", "s1"],
            resource_deltas=[{"mana": 0}, {"mana": 1}],
            state_hash_before="H0", state_hash_after="H1",
            event_start_seq=5, event_end_seq=12,
            trace=[[1, 1, ("option_id", 0)]],
        )
        runs = store.witness_runs()
        results = store.witness_results(run_id)
        assert runs[0]["id"] == run_id
        assert runs[0]["engine_commit"] == "abc"
        assert runs[0]["proto_version"] == 7
        assert json.loads(runs[0]["seeds_json"]) == [1, 2]
        assert json.loads(runs[0]["params_json"]) == {"max_iterations": 4}
        assert results[0]["id"] == result_id
        assert results[0]["run_id"] == run_id
        assert results[0]["verdict"] == "loops"
        assert results[0]["infinite"] == 1
        assert results[0]["iterations"] == 3
        assert json.loads(results[0]["card_names_json"]) == ["A", "B"]
        assert json.loads(results[0]["signature_json"]) == ["s1", "s2", "s1"]
        assert (results[0]["event_start_seq"], results[0]["event_end_seq"]) == (5, 12)
        store.close()

    def test_witness_verdict_check_constraint(self, tmp_path):
        store = ExperimentStore(tmp_path / "w.sqlite")
        run_id = store.start_witness_run()
        with pytest.raises(sqlite3.IntegrityError):
            store.record_witness_result(run_id, verdict="bogus")
        store.close()

    def test_witness_result_requires_run_fk(self, tmp_path):
        store = ExperimentStore(tmp_path / "w.sqlite")
        with pytest.raises(sqlite3.IntegrityError):
            store.record_witness_result(999999, verdict="no_loop")
        store.close()

    def test_witness_observation_roundtrip(self, tmp_path):
        store = ExperimentStore(tmp_path / "w.sqlite")
        run_id = store.start_witness_run(seeds=[1])
        # Observations are keyed by run_id alone: they can be appended before
        # any witness_results row exists (the live-UI contract).
        first = store.record_witness_observation(
            run_id, 0, turn=1, phase="Main1", signature="sig-base",
            resources={"mana": 8, "tokens": 0}, event_seq=0,
        )
        second = store.record_witness_observation(
            run_id, 1, turn=1, phase="Main1", signature="sig-iter1",
            resources={"mana": 9, "tokens": 1}, event_seq=4,
        )
        # Nullable turn/event_seq round-trip as NULL.
        third = store.record_witness_observation(run_id, 2, phase="Combat")
        rows = store.witness_observations(run_id)
        store.close()
        assert [r["id"] for r in rows] == [first, second, third]
        assert [r["iteration"] for r in rows] == [0, 1, 2]
        assert rows[0]["phase"] == "Main1"
        assert rows[0]["signature"] == "sig-base"
        assert json.loads(rows[0]["resources_json"]) == {"mana": 8, "tokens": 0}
        assert rows[1]["event_seq"] == 4
        assert json.loads(rows[1]["resources_json"]) == {"mana": 9, "tokens": 1}
        assert rows[2]["turn"] is None
        assert rows[2]["event_seq"] is None
        assert rows[2]["signature"] == ""
        assert json.loads(rows[2]["resources_json"]) == {}
        assert all(r["created_at"].endswith("+00:00") for r in rows)

    def test_evidence_and_diagnostics_roundtrip(self, tmp_path):
        store = ExperimentStore(tmp_path / "w.sqlite")
        run_id = store.start_witness_run(seeds=[1])
        evidence = {
            "kind": "recurrence",
            "grown": ["mana", "tokens"],
            "diagnostics": {"executed_actions": 3, "link_hits": [2, 1]},
        }
        store.record_witness_result(
            run_id, verdict="loops", evidence=evidence,
            diagnostics=evidence["diagnostics"],
        )
        # Omitting them stores NULL (the columns are nullable).
        store.record_witness_result(run_id, verdict="no_loop")
        rows = store.witness_results(run_id)
        store.close()
        assert json.loads(rows[0]["evidence_json"]) == evidence
        assert json.loads(rows[0]["diagnostics_json"]) == evidence["diagnostics"]
        assert rows[1]["evidence_json"] is None
        assert rows[1]["diagnostics_json"] is None


class TestRecording:
    def test_record_game_decision_event_roundtrip(self, tmp_path):
        path = tmp_path / "exp.sqlite"
        store = ExperimentStore(path)
        run_id = start_run(store, engine_commit="deadbeef", proto_version=6)
        client = FakeClient()
        run_game(client, DECKS, seed=7, player_types=REMOTE, store=store, run_id=run_id)
        assert client.stopped == 42
        store.close()

        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        exp = conn.execute("SELECT * FROM experiments").fetchone()
        assert exp["id"] == run_id
        assert exp["engine_commit"] == "deadbeef"
        assert exp["proto_version"] == 6
        assert json.loads(exp["config_json"]) == {"k": "v"}
        assert exp["created_at"].endswith("+00:00")

        game = conn.execute("SELECT * FROM games").fetchone()
        assert game["run_id"] == run_id
        assert (game["server_game_id"], game["seed"], game["max_turns"]) == (42, 7, 0)
        assert game["outcome"] == "OUTCOME_WIN"
        assert game["reason"] == "lethal"
        assert (game["turn_count"], game["event_count"]) == (2, 3)
        assert json.loads(game["decks_json"]) == [["a", "/tmp/a.dck"], ["b", "/tmp/b.dck"]]
        assert json.loads(game["player_types_json"]) == [
            "PLAYER_TYPE_REMOTE", "PLAYER_TYPE_REMOTE"
        ]

        decisions = conn.execute("SELECT * FROM decisions ORDER BY id").fetchall()
        assert len(decisions) == 2
        assert decisions[0]["game_row"] == game["id"]
        assert decisions[0]["decision_id"] == 1
        assert decisions[0]["type"] == "DECISION_TYPE_PRIORITY"
        assert decisions[0]["player"] == -1  # unknown from the trace
        assert json.loads(decisions[0]["answer_json"]) == {
            "arm": "option_id", "payload": 0
        }

        events = conn.execute("SELECT * FROM events ORDER BY seq").fetchall()
        assert [e["type"] for e in events] == ["TurnStarted", "CardDrawn", "GameOver"]
        assert events[1]["card_name"] == "Forest"
        assert events[1]["detail_raw"] == "draw"

    def test_run_game_store_requires_run_id(self, tmp_path):
        store = ExperimentStore(tmp_path / "exp.sqlite")
        with pytest.raises(ValueError, match="requires run_id"):
            run_game(FakeClient(), DECKS, seed=1, player_types=REMOTE, store=store)
        store.close()

    def test_nothing_recorded_when_store_absent(self, tmp_path):
        path = tmp_path / "exp.sqlite"
        store = ExperimentStore(path)
        start_run(store)
        run_game(FakeClient(), DECKS, seed=7, player_types=REMOTE)  # no store
        conn = sqlite3.connect(path)
        assert conn.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0
        store.close()

    def test_rejected_decision_not_persisted(self, tmp_path):
        store = ExperimentStore(tmp_path / "exp.sqlite")
        run_id = start_run(store)
        game_row = store.record_game(run_id, make_result(), DECKS, 1, REMOTE, 0)
        store.record_decision(
            game_row, DecisionRef(1, pb.DECISION_TYPE_PRIORITY), ("option_id", 0),
            accepted=False,
        )
        assert store._conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0
        store.close()


class TestExport:
    def test_export_jsonl_matches_table(self, tmp_path):
        store = ExperimentStore(tmp_path / "exp.sqlite")
        run_id = start_run(store)
        game_row = store.record_game(run_id, make_result(), DECKS, 1, REMOTE, 0)
        store.record_decision(game_row, DecisionRef(1, pb.DECISION_TYPE_PRIORITY), ("option_id", 0))
        store.record_events(game_row, [pb.GameEvent(seq=1, type="TurnStarted", turn=1)])
        out = tmp_path / "games.jsonl"
        store.export_jsonl("games", out)
        lines = out.read_text().splitlines()
        assert len(lines) == 1
        row = json.loads(lines[0])
        assert row["id"] == game_row and row["outcome"] == "OUTCOME_WIN"
        with pytest.raises(ValueError, match="unknown table"):
            store.export_jsonl("not_a_table", out)
        store.close()


class TestAppendOnly:
    def test_no_mutating_statements_in_module(self):
        src = Path(store_module.__file__).read_text(encoding="utf-8")
        assert not re.search(r"\b(update|delete)\b", src, re.IGNORECASE), (
            "store.py must stay append-only (no UPDATE/DELETE statements)"
        )


class TestConcurrency:
    def test_two_writers_do_not_corrupt(self, tmp_path):
        path = tmp_path / "exp.sqlite"
        store = ExperimentStore(path)
        run_ids = [start_run(store) for _ in range(2)]
        errors: list[BaseException] = []

        def worker(run_id, base):
            try:
                for i in range(20):
                    row = store.record_game(
                        run_id, make_result(base + i, base + i), DECKS, base + i, REMOTE, 0
                    )
                    store.record_events(row, [pb.GameEvent(seq=1, type="TurnStarted")])
                    store.record_decision(
                        row, DecisionRef(1, pb.DECISION_TYPE_PRIORITY), ("option_id", 0)
                    )
            except BaseException as e:  # pragma: no cover - surfaced below
                errors.append(e)

        threads = [
            threading.Thread(target=worker, args=(run_ids[0], 100)),
            threading.Thread(target=worker, args=(run_ids[1], 200)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert store._conn.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 40
        assert store._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 40
        assert store._conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 40
        assert store._conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        store.close()


class TestConfig:
    def test_defaults_when_file_missing(self, tmp_path):
        cfg = load_config(tmp_path / "absent.toml")
        assert cfg == ResearchConfig()
        assert cfg.engine_commit == DEFAULT_ENGINE_COMMIT
        assert cfg.proto_version == 7
        assert cfg.model == "openrouter/z-ai/glm-5.3-flash"
        meta = cfg.experiment_meta()
        assert meta == {
            "engine_commit": DEFAULT_ENGINE_COMMIT,
            "proto_version": 7,
            "policy_version": "default-v1",
            "model_version": "openrouter/z-ai/glm-5.3-flash",
        }

    def test_parses_a_file(self, tmp_path):
        p = tmp_path / "research.toml"
        p.write_text(
            '[meta]\nproject = "p"\nmodel = "m"\npolicy_version = "v9"\n'
            '[engine]\nforge_commit = "abc"\nproto_version = 7\n'
            '[defaults]\nmax_turns = 30\ntimeout_seconds = 5\nn_workers = 3\nbase_port = 6000\n'
        )
        cfg = load_config(p)
        assert (cfg.project, cfg.model, cfg.policy_version) == ("p", "m", "v9")
        assert (cfg.engine_commit, cfg.proto_version) == ("abc", 7)
        assert (
            cfg.max_turns, cfg.timeout_seconds, cfg.n_workers, cfg.base_port
        ) == (30, 5, 3, 6000)

    def test_repo_research_toml_loads_real_values(self):
        cfg = load_config("research.toml")
        assert cfg.engine_commit == "4f577da7b2a9074f9f66544aaf99405e38cf5ac3"
        assert cfg.proto_version == 7
        assert cfg.model == "openrouter/z-ai/glm-5.3-flash"


class TestAdjudications:
    def test_unlinked_adjudication_is_appended(self, tmp_path):
        """Hypothesis ids are stored as candidate_id until the tables link."""
        store = ExperimentStore(tmp_path / "exp.sqlite")
        row_id = store.record_adjudication(
            4242, "verified", reviewer="tui", notes="status cycled in TUI"
        )
        row = store._conn.execute(
            "SELECT * FROM adjudications WHERE id = ?", (row_id,)
        ).fetchone()
        assert (row["candidate_id"], row["verdict"]) == (4242, "verified")
        assert (row["reviewer"], row["notes"]) == ("tui", "status cycled in TUI")
        # Enforcement is restored for later writes on this connection.
        with pytest.raises(sqlite3.IntegrityError):
            store._conn.execute(
                "INSERT INTO adjudications (candidate_id, verdict, created_at)"
                " VALUES (?, ?, ?)",
                (999999, "x", "now"),
            )
        store._conn.rollback()
        store.close()

    def test_linked_adjudication_enforces_fk(self, tmp_path):
        store = ExperimentStore(tmp_path / "exp.sqlite")
        with pytest.raises(sqlite3.IntegrityError):
            store.record_adjudication(999999, "verified", allow_unlinked=False)
        store.close()
