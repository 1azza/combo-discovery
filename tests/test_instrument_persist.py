"""The batch instruments feed the witness tables only when ``--persist`` is set.

Both scripts are driven through their real ``main`` with a fake harness and a
temp DB, so the test covers the actual wiring (pre-run row -> live recorder ->
result/evidence) rather than the helpers in isolation.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

from combo_discovery.generated import forge_env_pb2 as pb
from combo_discovery.store import ExperimentStore
from test_witness import FakeWitnessClient

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str):
    """Import a ``scripts/<name>.py`` module by path (not an installed package)."""
    path = _REPO_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"combo_script_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


at = _load_script("analogue_transfer")
kr = _load_script("known_recall")


class _Permissive:
    def is_legal(self, name: str) -> bool:  # noqa: ARG002 - stub
        return True


class _FakeHarness:
    """Context-manager fake harness around :class:`FakeWitnessClient`."""

    def __init__(self, *args, **kwargs):  # noqa: ARG002 - accepts host/port
        self._client = FakeWitnessClient()

    def __enter__(self) -> "_FakeHarness":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def connect(self) -> pb.Pong:
        return pb.Pong(protocol_version=7)

    def __getattr__(self, name):
        return getattr(self._client, name)


ENGINE = "Kiki-Jiki, Mirror Breaker"
PARTNER = "Zealous Conscripts"


def _seed_cards(db) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO import_runs (import_id, started_at, scryfall_source)"
        " VALUES ('imp1', '2026-01-01T00:00:00+00:00', 'forge_script')"
    )
    cards = [
        (
            "kiki-jiki, mirror breaker",
            ENGINE,
            "Legendary Creature — Goblin",
            "{t}: create a token that's a copy of target nonlegendary creature "
            "you control.",
        ),
        (
            "zealous conscripts",
            PARTNER,
            "Creature — Human Warrior",
            "haste\nwhen zealous conscripts enters, gain control of target "
            "permanent until end of turn. untap that permanent.",
        ),
    ]
    for normalized, name, type_line, oracle in cards:
        conn.execute(
            "INSERT INTO cards (import_id, file_sha256, name, normalized_name,"
            " type_line, oracle_text) VALUES ('imp1', 'x', ?, ?, ?, ?)",
            (name, normalized, type_line, oracle),
        )
    conn.commit()
    conn.close()


def _assert_persisted(db) -> None:
    store = ExperimentStore(db)
    try:
        runs = store.witness_runs()
        results = store.witness_results()
        observations = store.witness_observations()
    finally:
        store.close()
    assert len(runs) == 1
    assert runs[0]["engine_commit"]  # research.toml / default metadata
    assert len(results) == 1
    assert results[0]["evidence_json"] is not None
    assert len(observations) >= 1
    assert {o["run_id"] for o in observations} == {runs[0]["id"]}
    assert all(o["signature"] for o in observations)


def test_analogue_transfer_persist_writes_run_result_and_observations(
    tmp_path, monkeypatch
):
    db = tmp_path / "research.db"
    ExperimentStore(db).close()
    _seed_cards(db)
    monkeypatch.setattr(at, "_load_vintage", lambda names: _Permissive())
    monkeypatch.setattr(at, "ForgeEnvClient", _FakeHarness)
    out = tmp_path / "analogue.json"

    rc = at.main(
        ["--db", str(db), "--engines", ENGINE, "--out", str(out), "--persist"]
    )
    assert rc == 0
    assert out.exists()
    _assert_persisted(db)


def test_known_recall_persist_writes_run_result_and_observations(
    tmp_path, monkeypatch
):
    db = tmp_path / "research.db"
    ExperimentStore(db).close()
    _seed_cards(db)
    conn = sqlite3.connect(db)
    for normalized in ("kiki-jiki, mirror breaker", "zealous conscripts"):
        conn.execute(
            "INSERT INTO known_combo_cards (combo_id, role, raw_name,"
            " normalized_name, import_id) VALUES (1, 'use', ?, ?, 'imp1')",
            (normalized, normalized),
        )
    conn.commit()
    conn.close()
    monkeypatch.setattr(kr, "ForgeEnvClient", _FakeHarness)
    out = tmp_path / "recall.json"

    rc = kr.main(
        ["--db", str(db), "--mode", "home", "--start", "0", "--count", "3",
         "--out", str(out), "--persist"]
    )
    assert rc == 0
    assert out.exists()
    _assert_persisted(db)


def test_instruments_default_path_writes_nothing(tmp_path, monkeypatch):
    db = tmp_path / "research.db"
    ExperimentStore(db).close()
    _seed_cards(db)
    monkeypatch.setattr(at, "_load_vintage", lambda names: _Permissive())
    monkeypatch.setattr(at, "ForgeEnvClient", _FakeHarness)
    out = tmp_path / "analogue_default.json"

    rc = at.main(["--db", str(db), "--engines", ENGINE, "--out", str(out)])
    assert rc == 0

    store = ExperimentStore(db)
    try:
        assert store.witness_runs() == []
        assert store.witness_results() == []
        assert store.witness_observations() == []
    finally:
        store.close()
