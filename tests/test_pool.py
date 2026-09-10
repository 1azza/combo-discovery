import logging
from unittest.mock import MagicMock

import grpc
import pytest

from combo_discovery.env import (
    ForgeEnvClient,
    GameNotActiveError,
    HarnessConnectionError,
)
from combo_discovery.generated import forge_env_pb2 as pb
from combo_discovery.pool import WorkerPool
from combo_discovery.runner import GameResult


def make_result(seed, winner=0):
    return GameResult(
        game_id=seed,
        seed=seed,
        winner=winner,
        turns=5,
        n_events=3,
        duration_s=0.1,
        outcome=pb.OUTCOME_WIN,
    )


def good_client(game_id=1):
    """Fake client whose run_game path completes immediately."""
    c = MagicMock()
    c.start_game.return_value = game_id
    c.is_game_over.return_value = pb.GameOver(over=True, winner=0, outcome=pb.OUTCOME_WIN)
    c.drain_events.return_value = []
    return c


def dead_client():
    c = MagicMock()
    c.start_game.side_effect = HarnessConnectionError(
        "UNAVAILABLE from harness", code=grpc.StatusCode.UNAVAILABLE
    )
    return c


def leftover_game_client(n_failures=1):
    """start_game fails with FAILED_PRECONDITION the first `n_failures` times
    (worker left with an active game), then succeeds; records the
    force_stop_active flag of each request."""
    c = MagicMock()
    c.calls = []
    responses = [GameNotActiveError("FAILED_PRECONDITION: game already active")] * n_failures

    def start_game(decks, seed, player_types=None, force_stop_active=False, **kw):
        c.calls.append(force_stop_active)
        if responses:
            raise responses.pop(0)
        return 42

    c.start_game.side_effect = start_game
    c.is_game_over.return_value = pb.GameOver(over=True, winner=0, outcome=pb.OUTCOME_WIN)
    c.drain_events.return_value = []
    return c


class FakePool(WorkerPool):
    def __init__(self, clients):
        self._clients = list(clients)
        self.n_workers = len(clients)
        self.base_port = 50060
        self.host = "localhost"
        self._procs = []
        self._healthy = [True] * len(clients)
        self._fail_counts = [0] * len(clients)
        self._rr = 0
        self._cmd_template = None
        self._startup_timeout = 120.0


DECK_PAIR = (("a", "/a.dck"), ("b", "/b.dck"))


class TestPoolScheduling:
    def test_round_robin_distribution(self):
        clients = [good_client(1), good_client(2)]
        pool = FakePool(clients)
        results = pool.map_games(DECK_PAIR, seeds=[1, 2])
        assert [r.seed for r in results] == [1, 2]
        assert clients[0].start_game.called
        assert clients[1].start_game.called

    def test_seed_retried_on_other_worker(self):
        """A failed job's seed moves to a healthy worker instead of dying."""
        bad = dead_client()
        good = good_client(9)
        pool = FakePool([bad, good])
        pool._revive = MagicMock(return_value=False)  # no real reconnect attempt
        results = pool.map_games(DECK_PAIR, seeds=[1])
        assert results[0].game_id == 9
        assert bad.start_game.called
        assert good.start_game.called

    def test_all_workers_dead_raises_after_bound(self):
        dead = dead_client()
        pool = FakePool([dead])
        pool._revive = MagicMock(return_value=False)  # no real reconnect attempt
        with pytest.raises(HarnessConnectionError, match="attempts|unhealthy"):
            pool.map_games(DECK_PAIR, seeds=[1])

    def test_total_attempts_bounded(self):
        """Job attempts are bounded by 2x workers even when workers keep
        'recovering' (revive succeeds but jobs keep failing)."""
        flaky = MagicMock()
        flaky.start_game.side_effect = HarnessConnectionError(
            "UNAVAILABLE from harness", code=grpc.StatusCode.UNAVAILABLE
        )
        pool = FakePool([flaky])
        pool._revive = MagicMock(return_value=True)  # revive always "works"
        with pytest.raises(HarnessConnectionError, match="2x workers"):
            pool.map_games(DECK_PAIR, seeds=[1, 2, 3, 4, 5, 6, 7, 8])
        # n_workers=1 → bound = 2 attempts
        assert flaky.start_game.call_count == 2


class TestPoolRecovery:
    def test_force_stop_recovery_on_leftover_game(self, caplog):
        """Worker with a leftover active game: one forced stop, then retry."""
        flaky = leftover_game_client(n_failures=1)
        pool = FakePool([flaky])
        with caplog.at_level(logging.WARNING):
            results = pool.map_games(DECK_PAIR, seeds=[7])
        assert results[0].game_id == 42
        # First attempt without, retry with force_stop_active=True.
        assert flaky.calls == [False, True]

    def test_persistent_leftover_failure_excludes_worker(self, caplog):
        # 4 failures: each of the 2 attempts burns an initial + a forced-stop
        # start_game; the worker must then be excluded from rotation.
        flaky = leftover_game_client(n_failures=4)
        pool = FakePool([flaky])
        pool._revive = MagicMock(return_value=False)  # no real reconnect attempt
        with caplog.at_level(logging.WARNING):
            with pytest.raises(HarnessConnectionError):
                pool.map_games(DECK_PAIR, seeds=[7])
        assert pool._healthy == [False]
        assert any("excluding" in r.message for r in caplog.records)

    def test_unhealthy_worker_not_scheduled(self):
        bad = dead_client()
        good = good_client(5)
        pool = FakePool([bad, good])
        pool._healthy[0] = False  # previously excluded
        results = pool.map_games(DECK_PAIR, seeds=[1, 2])
        assert [r.game_id for r in results] == [5, 5]
        assert not bad.start_game.called

    def test_connection_failure_triggers_revive(self):
        flaky = dead_client()
        pool = FakePool([flaky])
        pool._revive = MagicMock(return_value=True)  # revive works, jobs still fail
        with pytest.raises(HarnessConnectionError):
            pool.map_games(DECK_PAIR, seeds=[1])
        assert pool._revive.called

    def test_repeated_failures_excluded_then_all_dead_raises(self):
        dead = dead_client()
        pool = FakePool([dead])
        pool._revive = MagicMock(return_value=False)  # revive never helps
        with pytest.raises(HarnessConnectionError):
            pool.map_games(DECK_PAIR, seeds=[1])
        assert pool._healthy == [False]
