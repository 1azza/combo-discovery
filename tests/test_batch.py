"""Parallel batch runner: ordering, client exclusivity, and serial parity.

The Java harness is faked (``batch.ForgeEnvClient``) and the witness call is
faked (``batch.run_witness``), so these tests exercise the concurrency and
ordering machinery without touching a server.
"""

from __future__ import annotations

import threading
import time

import pytest

from combo_discovery import batch
from combo_discovery.witness import WitnessResult


class FakeClient:
    """A leased client that records concurrent use; never shared in tests."""

    instances: list["FakeClient"] = []
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
