from unittest.mock import MagicMock

import pytest

from combo_discovery.env import HarnessConnectionError
from combo_discovery.pool import WorkerPool
from combo_discovery.runner import GameResult


def make_result(seed, winner=0):
    return GameResult(game_id=seed, seed=seed, winner=winner, turns=5, n_events=3, duration_s=0.1)


def fake_client(game_id=1):
    c = MagicMock()
    c.start_game.return_value = MagicMock(game_id=game_id)
    c.is_game_over.return_value = MagicMock(over=True)
    c.get_decision.return_value = MagicMock(turn=3, options=[])
    return c


class FakePool(WorkerPool):
    def __init__(self, clients):
        self._clients = list(clients)
        self.n_workers = len(clients)
        self.base_port = 50060
        self.host = "localhost"
        self._procs = []
        self._next = 0


DECK_PAIR = (("a", "/a.dck"), ("b", "/b.dck"))


def test_round_robin_distribution():
    clients = [fake_client(1), fake_client(2)]
    pool = FakePool(clients)
    results = pool.map_games(DECK_PAIR, seeds=[1, 2])
    assert [r.seed for r in results] == [1, 2]
    assert clients[0].start_game.called
    assert clients[1].start_game.called


def test_worker_restart_on_connection_error():
    flaky = fake_client()
    flaky.start_game.side_effect = HarnessConnectionError("dead")
    good = fake_client(9)
    pool = FakePool([flaky])
    pool._restart = lambda dead: pool._clients.__setitem__(0, good)
    results = pool.map_games(DECK_PAIR, seeds=[1])
    assert results[0].game_id == 9


def test_all_workers_dead_raises():
    dead = fake_client()
    dead.start_game.side_effect = HarnessConnectionError("dead")
    pool = FakePool([dead])
    pool._restart = MagicMock()
    with pytest.raises(HarnessConnectionError):
        pool.map_games(DECK_PAIR, seeds=[1])
