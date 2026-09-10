"""Cancellation / interruption tests (Python side, fake-harness only).

(a) A game that ends while get_decision is blocked (GameNotActiveError) exits
    run_game cleanly with the drained result and stops the game.
(b) A harness that dies mid-decision (UNAVAILABLE HarnessConnectionError)
    makes run_game raise, but the best-effort cleanup still attempts
    stop_game first and swallows a secondary cleanup failure.
(c) A failed game records nothing corrupt in the store: the experiment row
    exists, but there is no completed game row (and no orphan events).
"""

from __future__ import annotations

import sqlite3

import pytest

from combo_discovery.env import (
    GameNotActiveError,
    HarnessConnectionError,
)
from combo_discovery.generated import forge_env_pb2 as pb
from combo_discovery.research_config import ResearchConfig
from combo_discovery.runner import run_game
from combo_discovery.store import ExperimentStore

DECKS = [("a", "/tmp/a.dck"), ("b", "/tmp/b.dck")]
REMOTE = [pb.PLAYER_TYPE_REMOTE, pb.PLAYER_TYPE_REMOTE]


def _event(seq: int, type_: str) -> pb.GameEvent:
    return pb.GameEvent(seq=seq, type=type_, turn=1, phase="Main1", player=0)


class _BaseFake:
    def __init__(self, events):
        self.events = list(events)
        self.game_id = 0
        self.answered = 0
        self.stopped: int | None = None
        self.stop_error: Exception | None = None

    def start_game(self, decks, seed, player_types=None, **kw):
        self.answered = 0
        self.game_id = 100 + seed
        return self.game_id

    def drain_events(self, game_id, cursor=0):
        return list(self.events)

    def get_state(self, game_id):
        return pb.FullState(game_id=game_id, turn=1)

    def stop_game(self, game_id):
        self.stopped = game_id
        if self.stop_error is not None:
            raise self.stop_error


class GameEndsWhileBlockedClient(_BaseFake):
    """get_decision reports the game already ended (turn limit / opponent win):
    the harness's normal 'wake on game over' path, not an error."""

    def is_game_over(self, game_id):
        # Not yet over until the blocked get_decision notices the end.
        return pb.GameOver(over=self.answered > 0, winner=0,
                           outcome=pb.OUTCOME_TURN_LIMIT, reason="turn limit")

    def get_decision(self, game_id):
        self.answered += 1
        raise GameNotActiveError("FAILED_PRECONDITION: game over")


class HarnessDiesMidDecisionClient(_BaseFake):
    """get_decision dies with UNAVAILABLE (transport death)."""

    def is_game_over(self, game_id):
        return pb.GameOver(over=False)

    def get_decision(self, game_id):
        raise HarnessConnectionError("UNAVAILABLE from harness")


class TestGameOverMidDecision:
    def test_run_game_exits_cleanly_and_stops(self):
        client = GameEndsWhileBlockedClient([_event(1, "TurnStarted"),
                                             _event(2, "GameOver")])
        result = run_game(client, DECKS, seed=4, player_types=REMOTE)
        # No exception escaped; the recorded result reflects the drained game.
        assert result.n_events == 2
        assert result.outcome == pb.OUTCOME_TURN_LIMIT
        assert result.reason == "turn limit"
        assert client.stopped == client.game_id


class TestHarnessDiesMidDecision:
    def test_run_game_raises_and_attempts_cleanup(self):
        client = HarnessDiesMidDecisionClient([_event(1, "TurnStarted")])
        with pytest.raises(HarnessConnectionError, match="UNAVAILABLE"):
            run_game(client, DECKS, seed=5, player_types=REMOTE)
        # Best-effort cleanup was attempted before the original error escaped.
        assert client.stopped == client.game_id

    def test_secondary_cleanup_failure_does_not_mask_original(self):
        client = HarnessDiesMidDecisionClient([_event(1, "TurnStarted")])
        client.stop_error = HarnessConnectionError("UNAVAILABLE during cleanup")
        with pytest.raises(HarnessConnectionError, match="UNAVAILABLE from harness"):
            run_game(client, DECKS, seed=5, player_types=REMOTE)
        assert client.stopped == client.game_id  # stop_game was attempted


class TestStoreOnFailedGame:
    def test_failed_game_records_experiment_only(self, tmp_path):
        path = tmp_path / "exp.sqlite"
        store = ExperimentStore(path)
        run_id = store.start_experiment(config={"k": "v"},
                                        **ResearchConfig().experiment_meta())

        client = HarnessDiesMidDecisionClient([_event(1, "TurnStarted")])
        with pytest.raises(HarnessConnectionError):
            run_game(client, DECKS, seed=6, player_types=REMOTE,
                     store=store, run_id=run_id)

        # The experiment row exists (start_experiment committed) ...
        assert store._conn.execute(
            "SELECT COUNT(*) FROM experiments"
        ).fetchone()[0] == 1
        assert store._conn.execute(
            "SELECT id FROM experiments"
        ).fetchone()[0] == run_id
        # ... but the failed game left no completed game/event/decision rows.
        for table in ("games", "events", "decisions"):
            assert store._conn.execute(
                f"SELECT COUNT(*) FROM {table}"  # noqa: S608 - fixed table names
            ).fetchone()[0] == 0, table
        assert store._conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        store.close()

        # The database file is still valid and readable after the failure.
        conn = sqlite3.connect(path)
        assert conn.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 0
        conn.close()
