import logging
from unittest.mock import MagicMock

import grpc
import pytest

from combo_discovery.env import (
    GameNotActiveError,
    HarnessConnectionError,
    HarnessTimeoutError,
    InvalidRequestError,
    ProtocolMismatchError,
    StaleDecisionError,
)
from combo_discovery.generated import forge_env_pb2 as pb
from combo_discovery.pool import WorkerPool


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


def failing_client(exc: Exception):
    c = MagicMock()
    c.start_game.side_effect = exc
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
        self._revive_fail_counts = [0] * len(clients)
        self._probe_readmit_used = [False] * len(clients)
        self._readmitted = [False] * len(clients)
        self._probe_cursor = 0
        self._attempts_consumed = 0
        self._rr = 0
        self._cmd_template = None
        # Small so all-excluded readiness waits don't stall unit tests.
        self._startup_timeout = 0.2


def make_pool(clients, probe=False, revive=False):
    """FakePool with network-touching recovery mocked out by default."""
    pool = FakePool(clients)
    pool._probe = MagicMock(return_value=probe)
    pool._revive = MagicMock(return_value=revive)
    return pool


DECK_PAIR = (("a", "/a.dck"), ("b", "/b.dck"))


class TestPoolScheduling:
    def test_round_robin_distribution(self):
        clients = [good_client(1), good_client(2)]
        pool = make_pool(clients)
        results = pool.map_games(DECK_PAIR, seeds=[1, 2])
        assert [r.seed for r in results] == [1, 2]
        assert clients[0].start_game.called
        assert clients[1].start_game.called

    def test_many_successful_seeds_do_not_hit_budget(self):
        """Regression for the confirmed bug: 25 healthy seeds over 2 workers
        must all complete (the old global 2x-worker budget raised at seed 17)."""
        clients = [good_client(1), good_client(2)]
        pool = make_pool(clients)
        seeds = list(range(1, 26))
        results = pool.map_games(DECK_PAIR, seeds=seeds)
        assert [r.seed for r in results] == seeds

    def test_seed_retried_on_other_worker(self):
        """A failed job's seed moves to a healthy worker instead of dying."""
        bad = dead_client()
        good = good_client(9)
        pool = make_pool([bad, good])
        results = pool.map_games(DECK_PAIR, seeds=[1])
        assert results[0].game_id == 9
        assert bad.start_game.called
        assert good.start_game.called

    def test_worker_dying_mid_game_is_retried_then_excluded(self):
        """A worker that dies after start_game (mid-game, not at StartGame)
        fails the job; its seed moves to the healthy worker and, after
        CONSECUTIVE_FAILURES_BEFORE_EXCLUDE failures, it is excluded while the
        map still completes every seed."""
        dying = MagicMock()
        dying.start_game.return_value = 1
        dying.is_game_over.side_effect = HarnessConnectionError(
            "UNAVAILABLE from harness", code=grpc.StatusCode.UNAVAILABLE
        )
        dying.drain_events.return_value = []
        good = good_client(9)
        pool = make_pool([dying, good])
        results = pool.map_games(DECK_PAIR, seeds=[1, 2, 3])
        assert [r.game_id for r in results] == [9, 9, 9]
        assert good.start_game.call_count == 3
        assert pool._healthy[0] is False  # failed worker excluded
        assert pool._healthy[1] is True

    def test_harness_timeout_is_worker_failure_not_fatal(self):
        bad = failing_client(
            HarnessTimeoutError("DEADLINE_EXCEEDED", code=grpc.StatusCode.DEADLINE_EXCEEDED)
        )
        good = good_client(11)
        pool = make_pool([bad, good])
        results = pool.map_games(DECK_PAIR, seeds=[1])
        assert results[0].game_id == 11

    def test_stale_decision_is_worker_failure_not_fatal(self):
        bad = failing_client(
            StaleDecisionError("INVALID_ARGUMENT: stale", code=grpc.StatusCode.INVALID_ARGUMENT)
        )
        good = good_client(12)
        pool = make_pool([bad, good])
        results = pool.map_games(DECK_PAIR, seeds=[1])
        assert results[0].game_id == 12

    def test_all_workers_dead_raises(self):
        pool = make_pool([dead_client()])
        with pytest.raises(HarnessConnectionError, match="unhealthy"):
            pool.map_games(DECK_PAIR, seeds=[1])

    def test_permanent_seed_failure_raises_after_two_rounds(self):
        """A seed that every healthy worker fails is given up on only after
        MAX_SEED_ROUNDS full rounds, with a clear message. Health accounting is
        isolated so the round budget itself is under test."""
        pool = make_pool([failing_client(HarnessConnectionError("down"))])
        pool._note_failure = MagicMock()  # keep the worker healthy to isolate rounds
        with pytest.raises(HarnessConnectionError, match="2 full rounds"):
            pool.map_games(DECK_PAIR, seeds=[7])


class TestPoolRecovery:
    def test_force_stop_recovery_on_leftover_game(self, caplog):
        """Worker with a leftover active game: one forced stop, then retry."""
        flaky = leftover_game_client(n_failures=1)
        pool = make_pool([flaky])
        with caplog.at_level(logging.WARNING):
            results = pool.map_games(DECK_PAIR, seeds=[7])
        assert results[0].game_id == 42
        # First attempt without, retry with force_stop_active=True.
        assert flaky.calls == [False, True]

    def test_excluded_worker_reprobe_readmits(self):
        # A worker excluded earlier (but healthy) is re-admitted by the probe
        # and then scheduled successfully.
        recovered = good_client(5)
        other = good_client(6)
        pool = make_pool([recovered, other], probe=True, revive=False)
        pool._healthy[0] = False  # previously excluded
        results = pool.map_games(DECK_PAIR, seeds=[1])
        assert results[0].game_id in (5, 6)
        assert recovered.start_game.called  # re-admitted and scheduled
        assert pool._healthy[0] is True

    def test_unhealthy_worker_not_scheduled_when_probe_fails(self):
        bad = dead_client()
        good = good_client(5)
        pool = make_pool([bad, good], probe=False)
        pool._healthy[0] = False
        results = pool.map_games(DECK_PAIR, seeds=[1, 2])
        assert [r.game_id for r in results] == [5, 5]
        assert not bad.start_game.called
        assert pool._healthy[0] is False

    def test_exclusion_after_consecutive_failures_even_if_revive_pings(self):
        """P2(a): a pingable-but-broken worker must still be excludable."""
        pool = make_pool(
            [failing_client(HarnessConnectionError("down"))],
            probe=False,
            revive=True,  # revive reports success (pingable)
        )
        with pytest.raises(HarnessConnectionError):
            pool.map_games(DECK_PAIR, seeds=[1])
        assert pool._healthy == [False]

    def test_wedged_alive_process_killed_and_respawned(self, monkeypatch):
        """P2(d): process alive but revive keeps failing -> kill + respawn."""
        import combo_discovery.pool as pool_mod

        class FailingClient:
            def __init__(self, *args, **kwargs):
                pass

            def connect(self):
                raise HarnessConnectionError("nope")

            def close(self):
                pass

        monkeypatch.setattr(pool_mod, "ForgeEnvClient", FailingClient)
        pool = FakePool([good_client()])
        pool._cmd_template = "fake-harness --port {port}"
        pool._startup_timeout = 1.0
        proc = MagicMock()
        proc.pid = 4242
        proc.poll.return_value = None  # alive
        pool._procs = [proc]
        pool._spawn = MagicMock(return_value=MagicMock())
        pool._kill_proc = MagicMock()

        assert pool._revive(0) is False  # failure count 1
        assert not pool._kill_proc.called
        assert pool._revive(0) is False  # failure count 2 -> wedged
        pool._kill_proc.assert_called_once()
        pool._spawn.assert_called_once()

    def test_spawn_uses_new_session_and_kill_is_group_safe(self, monkeypatch):
        """P2(e): spawned harnesses get their own process group; kill must not
        blow up on an already-gone process."""
        import combo_discovery.pool as pool_mod

        calls: dict = {}

        def fake_popen(cmd, **kwargs):
            calls["cmd"] = cmd
            calls["kwargs"] = kwargs
            return MagicMock()

        monkeypatch.setattr(pool_mod.subprocess, "Popen", fake_popen)
        WorkerPool._spawn("harness --port {port}", 5555)
        assert calls["cmd"] == "harness --port 5555"
        assert calls["kwargs"]["start_new_session"] is True
        assert calls["kwargs"]["shell"] is True

        # killpg on a bogus-but-int pid falls back to proc.kill() and never raises
        proc = MagicMock()
        proc.pid = 999999
        WorkerPool._kill_proc(proc)
        proc.kill.assert_called_once()


class TestProbeReviveTaxonomy:
    """Q1: probe/revive treat any ForgeEnvError (incl. timeout) as not
    recovered; unexpected non-ForgeEnv exceptions propagate."""

    def _install_client(self, monkeypatch, exc_factory):
        import combo_discovery.pool as pool_mod

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def connect(self):
                raise exc_factory()

            def close(self):
                pass

        monkeypatch.setattr(pool_mod, "ForgeEnvClient", FakeClient)

    def test_probe_and_revive_treat_timeout_as_not_recovered(self, monkeypatch):
        self._install_client(
            monkeypatch,
            lambda: HarnessTimeoutError(
                "DEADLINE_EXCEEDED", code=grpc.StatusCode.DEADLINE_EXCEEDED
            ),
        )
        pool = FakePool([good_client()])  # real _probe/_revive (not mocked)
        assert pool._probe(0) is False
        assert pool._revive(0) is False

    def test_probe_and_revive_treat_connection_error_as_not_recovered(self, monkeypatch):
        self._install_client(
            monkeypatch,
            lambda: HarnessConnectionError(
                "UNAVAILABLE", code=grpc.StatusCode.UNAVAILABLE
            ),
        )
        pool = FakePool([good_client()])
        assert pool._probe(0) is False
        assert pool._revive(0) is False

    def test_probe_propagates_unexpected_exceptions(self, monkeypatch):
        self._install_client(monkeypatch, lambda: RuntimeError("boom"))
        pool = FakePool([good_client()])
        with pytest.raises(RuntimeError, match="boom"):
            pool._probe(0)

    def test_immediate_revive_uses_short_probe_timeout(self, monkeypatch):
        """M2: immediate revive must not use _startup_timeout (120s default);
        it connects once with the short probe timeout."""
        import combo_discovery.pool as pool_mod

        recorded: dict = {}

        class FakeClient:
            def __init__(self, *args, **kwargs):
                recorded["timeout"] = kwargs.get("timeout")

            def connect(self):
                recorded["connected"] = True

            def close(self):
                pass

        monkeypatch.setattr(pool_mod, "ForgeEnvClient", FakeClient)
        pool = FakePool([good_client()])
        pool._startup_timeout = 999.0  # must NOT be used by the immediate revive
        assert pool._revive(0) is True
        assert recorded["connected"] is True
        assert recorded["timeout"] == pool_mod.PROBE_TIMEOUT

    def test_probe_timeout_is_conserved_for_readiness(self):
        import combo_discovery.pool as pool_mod

        assert pool_mod.PROBE_TIMEOUT < 5.0  # short, not the 120s startup wait
        assert pool_mod.READINESS_PROBE_INTERVAL_S > 0

    # --- M3: configuration failures propagate ------------------------------

    def test_probe_propagates_protocol_mismatch(self, monkeypatch):
        self._install_client(
            monkeypatch, lambda: ProtocolMismatchError("protocol version mismatch: v2 vs v3")
        )
        pool = FakePool([good_client()])
        with pytest.raises(ProtocolMismatchError, match="protocol version mismatch"):
            pool._probe(0)

    def test_revive_propagates_protocol_mismatch(self, monkeypatch):
        self._install_client(
            monkeypatch, lambda: ProtocolMismatchError("protocol version mismatch: v2 vs v3")
        )
        pool = FakePool([good_client()])
        with pytest.raises(ProtocolMismatchError, match="protocol version mismatch"):
            pool._revive(0)

    def test_map_games_propagates_protocol_mismatch_from_probe(self, monkeypatch):
        self._install_client(
            monkeypatch, lambda: ProtocolMismatchError("protocol version mismatch: v2 vs v3")
        )
        pool = FakePool([good_client()])
        pool._healthy = [False]
        with pytest.raises(ProtocolMismatchError, match="protocol version mismatch"):
            pool.map_games(DECK_PAIR, seeds=[1])


class TestReadmissionPolicy:
    """Q2: at most one probe re-admission per worker per map_games call."""

    def test_readmitted_worker_excluded_after_next_failure(self):
        # worker0 is pingable (probe True) but every job fails; worker1 is good.
        bad = failing_client(HarnessConnectionError("down"))
        good = good_client(7)
        pool = make_pool([bad, good], probe=True, revive=False)
        results = pool.map_games(DECK_PAIR, seeds=list(range(1, 10)))
        assert [r.game_id for r in results] == [7] * 9
        # worker0 got its one re-admission, failed again, and is now excluded
        # for the rest of the call.
        assert pool._probe_readmit_used[0] is True
        assert pool._healthy[0] is False
        assert pool._healthy[1] is True

    def test_probe_success_with_spent_readmission_stays_excluded(self):
        pool = make_pool([dead_client()], probe=True)
        pool._healthy = [False]
        pool._probe_readmit_used = [True]  # one re-admission already spent
        pool._reprobe_excluded()
        assert pool._probe.called
        assert pool._healthy == [False]  # not re-admitted again

    def test_readmission_available_again_on_next_map_call(self):
        # Two seed batches; the worker's one re-admission resets per call.
        bad = failing_client(HarnessConnectionError("down"))
        good = good_client(4)
        pool = make_pool([bad, good], probe=True, revive=False)
        pool._healthy[0] = False
        pool.map_games(DECK_PAIR, seeds=[1])
        assert pool._probe_readmit_used[0] is True
        # New call resets the per-call re-admission budget.
        pool._healthy[0] = False
        pool.map_games(DECK_PAIR, seeds=[2])
        assert pool._probe_readmit_used[0] is True
        assert good.start_game.call_count >= 2


class TestProbeStarvation:
    """Q3: one probe per iteration, rotating; readiness wait when all down."""

    def test_one_probe_per_iteration_rotates(self):
        pool = make_pool([good_client(), good_client()], probe=False)
        pool._healthy = [False, False]
        pool._probe = MagicMock(return_value=False)
        pool._reprobe_excluded()
        assert pool._probe.call_count == 1
        first = pool._probe.call_args[0][0]
        pool._reprobe_excluded()
        assert pool._probe.call_count == 2
        second = pool._probe.call_args[0][0]
        assert {first, second} == {0, 1}  # rotates through excluded workers

    def test_all_excluded_waits_for_readiness_then_recovers(self):
        pool = make_pool([good_client(3)], probe=False)
        pool._healthy = [False]
        pool._startup_timeout = 5.0
        attempts = {"n": 0}

        def probe(idx):
            attempts["n"] += 1
            return attempts["n"] >= 3  # harness boots after a couple of tries

        pool._probe = MagicMock(side_effect=probe)
        results = pool.map_games(DECK_PAIR, seeds=[1])
        assert results[0].game_id == 3
        assert pool._healthy == [True]

    def test_all_excluded_never_recovers_raises_after_readiness_wait(self):
        pool = make_pool([good_client()], probe=False)
        pool._healthy = [False]
        pool._startup_timeout = 0.2
        with pytest.raises(HarnessConnectionError, match="unhealthy"):
            pool.map_games(DECK_PAIR, seeds=[1])


class TestInvalidRequestNotBlamedOnWorkers:
    """Q4: InvalidRequestError is a policy/request bug — it propagates and does
    not touch worker health."""

    def test_policy_invalid_request_propagates_without_excluding_workers(self):
        client = MagicMock()
        client.start_game.return_value = 1
        client.is_game_over.return_value = pb.GameOver(over=False)
        client.get_decision.return_value = pb.DecisionRequest(
            game_id=1,
            decision_id=1,
            decision_type=pb.DECISION_TYPE_PRIORITY,
            options=[pb.Option(id=0, kind="pass")],
        )
        client.drain_events.return_value = []
        pool = make_pool([client])

        def bad_policy(ctx):
            raise InvalidRequestError("INVALID_ARGUMENT: wrong answer kind")

        with pytest.raises(InvalidRequestError, match="wrong answer kind"):
            pool.map_games(DECK_PAIR, seeds=[1], policy=bad_policy)
        assert pool._healthy == [True]
        assert pool._fail_counts == [0]

    def test_invalid_request_from_start_game_propagates_without_excluding(self):
        bad = failing_client(InvalidRequestError("INVALID_ARGUMENT: need 2 decks"))
        good = good_client(3)
        pool = make_pool([bad, good])
        with pytest.raises(InvalidRequestError, match="need 2 decks"):
            pool.map_games(DECK_PAIR, seeds=[1])
        assert pool._healthy == [True, True]  # no worker blamed
        assert not good.start_game.called
