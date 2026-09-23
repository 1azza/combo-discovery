"""Parallel batch runner: ordering, client exclusivity, and serial parity.

The Java harness is faked (``batch.ForgeEnvClient``) and the witness call is
faked (``batch.run_witness``), so these tests exercise the concurrency and
ordering machinery without touching a server.
"""

from __future__ import annotations

import threading
import time
from collections import Counter

import pytest

from combo_discovery import batch
from combo_discovery.env import GameNotActiveError
from combo_discovery.generated import forge_env_pb2 as pb
from combo_discovery.store import ExperimentStore
from combo_discovery.witness import WitnessResult


class FakeClient:
    """A leased client that records concurrent use; never shared in tests."""

    instances: list[FakeClient] = []
    ports: list[int] = []

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self._guard = threading.Lock()
        self.in_use = 0
        self.max_in_use = 0
        FakeClient.instances.append(self)
        FakeClient.ports.append(port)

    def connect(self):
        return None

    def close(self) -> None:
        return None


class FakePool:
    """Stand-in for WorkerPool used to check the spawn wiring."""

    spawned: list[tuple] = []
    closed = 0

    @classmethod
    def spawn_servers(cls, cmd_template, n, base_port, host="localhost"):
        cls.spawned.append((cmd_template, n, base_port, host))
        return cls()

    def close(self) -> None:
        FakePool.closed += 1


def _pair_index(client, kwargs) -> int:
    key = str(kwargs.get("candidate_key", ""))
    return int(key.split("e", 1)[1].split("+", 1)[0])


def make_fake_run_witness(sleep_scale: float = 0.0):
    def fake_run_witness(client, scenario, policy, **kwargs):
        with client._guard:
            client.in_use += 1
            client.max_in_use = max(client.max_in_use, client.in_use)
        try:
            index = _pair_index(client, kwargs)
            # Later pairs (higher index) sleep less, so they finish first.
            if sleep_scale:
                time.sleep(max(0.0, sleep_scale * (4 - index)))
            return WitnessResult(
                verdict="loops" if index % 2 == 0 else "no_loop",
                scenario=scenario,
                iterations=index + 1,
                evidence={"diagnostics": {"executed_actions": index}},
            )
        finally:
            with client._guard:
                client.in_use -= 1

    return fake_run_witness


@pytest.fixture(autouse=True)
def _reset_fakes():
    FakeClient.instances = []
    FakeClient.ports = []
    FakePool.spawned = []
    FakePool.closed = 0
    yield


PAIRS = [("e0", "p0"), ("e1", "p1"), ("e2", "p2"), ("e3", "p3")]
DECKS = [("a", "/tmp/a.dck"), ("b", "/tmp/b.dck")]


def test_results_are_in_input_order_when_later_pairs_finish_first(monkeypatch):
    monkeypatch.setattr(batch, "ForgeEnvClient", FakeClient)
    monkeypatch.setattr(batch, "run_witness", make_fake_run_witness(sleep_scale=0.05))

    records = batch.run_batch(PAIRS, workers=2, decks=DECKS)

    assert [r["engine"] for r in records] == ["e0", "e1", "e2", "e3"]
    assert [r["iterations"] for r in records] == [1, 2, 3, 4]


def test_no_client_is_used_by_two_threads_at_once(monkeypatch):
    monkeypatch.setattr(batch, "ForgeEnvClient", FakeClient)
    monkeypatch.setattr(batch, "run_witness", make_fake_run_witness(sleep_scale=0.03))

    pairs = [(f"e{i}", f"p{i}") for i in range(6)]
    records = batch.run_batch(pairs, workers=3, decks=DECKS)

    assert len(records) == 6
    assert [r["engine"] for r in records] == [f"e{i}" for i in range(6)]
    # One client per worker, each never concurrently in use.
    assert len(FakeClient.instances) == 3
    assert all(client.max_in_use <= 1 for client in FakeClient.instances)
    assert FakeClient.ports == [50051, 50052, 50053]


def test_serial_and_parallel_records_match(monkeypatch):
    monkeypatch.setattr(batch, "ForgeEnvClient", FakeClient)
    monkeypatch.setattr(batch, "run_witness", make_fake_run_witness())

    serial = batch.run_batch(PAIRS, workers=1, decks=DECKS)
    parallel = batch.run_batch(PAIRS, workers=4, decks=DECKS)

    assert serial == parallel
    assert len(FakeClient.instances) == 1 + 4  # one serial client, four parallel


def test_spawn_wires_worker_pool_and_stops_it(monkeypatch):
    monkeypatch.setattr(batch, "ForgeEnvClient", FakeClient)
    monkeypatch.setattr(batch, "WorkerPool", FakePool)
    monkeypatch.setattr(batch, "run_witness", make_fake_run_witness())

    records = batch.run_batch(
        PAIRS, workers=2, base_port=50500, spawn=True, decks=DECKS
    )

    assert len(records) == 4
    assert FakePool.spawned == [(batch.DEFAULT_SPAWN_CMD, 2, 50500, "localhost")]
    assert FakePool.closed == 1
    assert FakeClient.ports == [50500, 50501]


def test_empty_pairs_returns_empty(monkeypatch):
    monkeypatch.setattr(batch, "ForgeEnvClient", FakeClient)
    assert batch.run_batch([], workers=4, decks=DECKS) == []


# ---------------------------------------------------------------------------
# Live narration streams from the worker that owns the run
# ---------------------------------------------------------------------------


KIKI = "Kiki-Jiki, Mirror Breaker"
DECEIVER = "Deceiver Exarch"


class FakeNarratingClient:
    """A leased client that emits one Kiki-Jiki loop turn on poll."""

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.game_id = port
        self.mana = 0
        self._decisions = 0
        self._polls = 0
        self._emitted = False

    def connect(self):
        return None

    def close(self):
        return None

    def start_game(self, decks, seed, player_types=None, **kwargs):
        return self.game_id

    def setup_scenario(self, game_id, scenario):
        return "H0", 3

    def get_decision(self, game_id):
        self._decisions += 1
        return pb.DecisionRequest(
            game_id=game_id,
            decision_id=self._decisions,
            player=0,
            turn=1,
            phase="MAIN1",
            decision_type=pb.DECISION_TYPE_PRIORITY,
            options=[pb.Option(id=0, kind="activate", card_name=KIKI)],
        )

    def submit_decision(self, game_id, decision_id, answer):
        self.mana += 1
        return pb.StepResult()

    def is_game_over(self, game_id):
        return pb.GameOver(over=False)

    def get_state(self, game_id, view_as_player=0):
        state = pb.FullState(
            game_id=1, turn=1, phase="MAIN1", active_player=0,
            state_hash=f"H{self.mana}",
        )
        state.life.extend([20, 20])
        state.typed_mana_pools.add(colorless=self.mana)
        state.battlefield_cards.append(pb.Permanent(id=0, card_name=KIKI))
        state.battlefield.add().permanents.append(0)
        return state

    def poll_events(self, game_id, cursor=0):
        self._polls += 1
        if self._polls == 1 or self._emitted:
            return pb.EventBatch(next_cursor=cursor)
        self._emitted = True
        return pb.EventBatch(
            events=[
                pb.GameEvent(
                    seq=1, game_id=game_id, type="SpellResolved", card_name=KIKI,
                    player=0, turn=1, phase="MAIN1",
                    detail_raw=(
                        f"{KIKI} (125) - A creates a token that's a copy of "
                        f"{DECEIVER} (126)."
                    ),
                ),
                pb.GameEvent(
                    seq=2, game_id=game_id, type="CardTapped", card_name=KIKI,
                    player=0, turn=1, phase="MAIN1", extra="tapped=false",
                ),
            ],
            next_cursor=2,
        )

    def stop_game(self, game_id):
        return None


def test_batch_persist_streams_witness_events(monkeypatch, tmp_path):
    path = tmp_path / "batch.sqlite"
    ExperimentStore(path).close()  # create the schema
    monkeypatch.setattr(batch, "ForgeEnvClient", FakeNarratingClient)
    monkeypatch.setattr(batch, "_load_card_meta", lambda db: {})

    records = batch.run_batch(
        [(KIKI, DECEIVER)],
        workers=1,
        decks=DECKS,
        db=path,
        persist=True,
        max_iterations=2,
        max_decisions=4,
    )

    assert records[0]["engine"] == KIKI
    store = ExperimentStore(path)
    runs = store.witness_runs()
    events = store.witness_events(runs[0]["id"])
    store.close()
    kinds = [e["kind"] for e in events]
    assert "copy" in kinds
    assert "untap" in kinds
    assert runs[0]["candidate_key"] == f"{KIKI}+{DECEIVER}"


# ---------------------------------------------------------------------------
# Stale-game recovery: a start refusal is retried, not recorded as an error
# ---------------------------------------------------------------------------

REFUSAL = (
    "FAILED_PRECONDITION: previous game thread (game_id=57) did not terminate; "
    "refusing to start a new game"
)


def _force_flags(kwargs) -> bool:
    return bool(kwargs.get("start_game_kwargs", {}).get("force_stop_active"))


def test_start_refusal_is_retried_and_records_real_verdict(monkeypatch):
    """First attempt hits the 'previous game did not terminate' refusal; the
    forced-stop retry succeeds, so the task records its real verdict."""
    monkeypatch.setattr(batch, "ForgeEnvClient", FakeClient)
    calls = {"n": 0}
    forces: list[bool] = []

    def fake_run_witness(client, scenario, policy, **kwargs):
        calls["n"] += 1
        forces.append(_force_flags(kwargs))
        if calls["n"] == 1:
            raise GameNotActiveError(REFUSAL)
        return WitnessResult(
            verdict="loops",
            scenario=scenario,
            iterations=2,
            evidence={"diagnostics": {"executed_actions": 2}},
        )

    monkeypatch.setattr(batch, "run_witness", fake_run_witness)
    stats: dict = {}

    records = batch.run_batch([("e0", "p0")], workers=1, decks=DECKS, stats=stats)

    assert records[0]["verdict"] == "loops"
    assert not records[0]["error"]
    # First attempt normal, retry with force_stop_active=True.
    assert forces == [False, True]
    assert stats["recovered"] == 1
    assert stats["unrecovered"] == 0


def test_error_verdict_refusal_is_retried(monkeypatch):
    """The witness driver swallows a start refusal into an error verdict; that
    shape is detected and retried too."""
    monkeypatch.setattr(batch, "ForgeEnvClient", FakeClient)
    calls = {"n": 0}

    def fake_run_witness(client, scenario, policy, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return WitnessResult(
                verdict="error",
                scenario=scenario,
                iterations=0,
                error=REFUSAL,
                evidence={"error": f"GameNotActiveError: {REFUSAL}"},
            )
        return WitnessResult(
            verdict="no_loop",
            scenario=scenario,
            iterations=1,
            evidence={"diagnostics": {"executed_actions": 1}},
        )

    monkeypatch.setattr(batch, "run_witness", fake_run_witness)
    stats: dict = {}

    records = batch.run_batch([("e0", "p0")], workers=1, decks=DECKS, stats=stats)

    assert records[0]["verdict"] == "no_loop"
    assert calls["n"] == 2
    assert stats["recovered"] == 1


def test_bad_port_retired_and_queue_continues(monkeypatch):
    """A port that keeps refusing is retired; every task still gets a verdict
    from a live port, so one stuck harness cannot consume the rest of the queue."""
    monkeypatch.setattr(batch, "ForgeEnvClient", FakeClient)
    by_port: Counter = Counter()

    def fake_run_witness(client, scenario, policy, **kwargs):
        by_port[client.port] += 1
        if client.port == 50051:
            raise GameNotActiveError(REFUSAL)
        return WitnessResult(
            verdict="loops",
            scenario=scenario,
            iterations=1,
            evidence={"diagnostics": {"executed_actions": 1}},
        )

    monkeypatch.setattr(batch, "run_witness", fake_run_witness)
    stats: dict = {}
    pairs = [(f"e{i}", f"p{i}") for i in range(6)]

    records = batch.run_batch(pairs, workers=2, decks=DECKS, stats=stats)

    assert [r["verdict"] for r in records] == ["loops"] * 6
    assert stats["retired_ports"] == [50051]
    assert stats["unrecovered"] == 0
    # The poisoned port is used only for its bounded attempts, never the queue.
    assert by_port[50051] <= batch.MAX_ATTEMPTS_PER_PORT


def test_all_ports_bad_records_errors_without_hanging(monkeypatch):
    """When every port is poisoned the batch stops (bounded), recording errors
    rather than spinning forever."""
    monkeypatch.setattr(batch, "ForgeEnvClient", FakeClient)

    def fake_run_witness(client, scenario, policy, **kwargs):
        raise GameNotActiveError(REFUSAL)

    monkeypatch.setattr(batch, "run_witness", fake_run_witness)
    stats: dict = {}
    pairs = [(f"e{i}", f"p{i}") for i in range(4)]

    records = batch.run_batch(pairs, workers=2, decks=DECKS, stats=stats)

    assert len(records) == 4
    assert all(r["verdict"] == "error" for r in records)
    assert stats["unrecovered"] == 4


def test_recovery_stats_shape(monkeypatch):
    monkeypatch.setattr(batch, "ForgeEnvClient", FakeClient)
    monkeypatch.setattr(batch, "run_witness", make_fake_run_witness())
    stats: dict = {}

    batch.run_batch(PAIRS, workers=2, decks=DECKS, stats=stats)

    assert stats == {
        "recovered": 0,
        "recovery_attempts": 0,
        "recovery_failures": 0,
        "unrecovered": 0,
        "retired_ports": [],
    }


def test_recovered_task_persists_real_verdict_not_error(monkeypatch, tmp_path):
    """A recovered task leaves exactly one run row with its real verdict — no
    orphan error row from the refused first attempt."""
    path = tmp_path / "recovered.sqlite"
    ExperimentStore(path).close()  # create the schema
    monkeypatch.setattr(batch, "ForgeEnvClient", FakeClient)
    monkeypatch.setattr(batch, "_load_card_meta", lambda db: {})
    calls = {"n": 0}

    def fake_run_witness(client, scenario, policy, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise GameNotActiveError(REFUSAL)
        return WitnessResult(
            verdict="no_loop",
            scenario=scenario,
            iterations=1,
            evidence={"diagnostics": {"executed_actions": 1}},
        )

    monkeypatch.setattr(batch, "run_witness", fake_run_witness)

    records = batch.run_batch(
        [("e0", "p0")], workers=1, decks=DECKS, db=path, persist=True
    )

    assert records[0]["verdict"] == "no_loop"
    store = ExperimentStore(path)
    runs = store.witness_runs()
    results = store.witness_results(runs[0]["id"])
    store.close()
    assert len(runs) == 1
    assert [r["verdict"] for r in results] == ["no_loop"]


def test_retry_does_not_change_verdict(monkeypatch):
    """Determinism: a retried pair gets the same verdict a clean first attempt
    would have produced."""
    monkeypatch.setattr(batch, "ForgeEnvClient", FakeClient)

    def fake_run_witness(client, scenario, policy, **kwargs):
        index = _pair_index(client, kwargs)
        return WitnessResult(
            verdict="loops" if index % 2 == 0 else "no_loop",
            scenario=scenario,
            iterations=index + 1,
            evidence={"diagnostics": {"executed_actions": index}},
        )

    monkeypatch.setattr(batch, "run_witness", fake_run_witness)
    clean = batch.run_batch(PAIRS, workers=2, decks=DECKS)

    refusals = {"n": 0}

    def flaky_run_witness(client, scenario, policy, **kwargs):
        # Refuse once per client, then fall through to the deterministic verdict.
        if _force_flags(kwargs):
            return fake_run_witness(client, scenario, policy, **kwargs)
        refusals["n"] += 1
        raise GameNotActiveError(REFUSAL)

    monkeypatch.setattr(batch, "run_witness", flaky_run_witness)
    recovered = batch.run_batch(PAIRS, workers=2, decks=DECKS)

    assert [r["verdict"] for r in recovered] == [r["verdict"] for r in clean]
    assert refusals["n"] == 4  # one refused first attempt per pair

