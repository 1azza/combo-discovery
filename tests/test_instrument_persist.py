"""The batch instruments feed the witness tables only when ``--persist`` is set.

Both scripts are driven through their real ``main`` with a fake harness and a
temp DB, so the test covers the actual wiring (pre-run row -> live recorder ->
result/evidence) rather than the helpers in isolation.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

from test_witness import FakeWitnessClient

from combo_discovery import cards as cards_mod
from combo_discovery.generated import forge_env_pb2 as pb
from combo_discovery.store import ExperimentStore

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

    def __enter__(self) -> _FakeHarness:
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
    monkeypatch.setattr(cards_mod, "_LEGALITY", _Permissive())
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
    monkeypatch.setattr(cards_mod, "_LEGALITY", _Permissive())
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


class _StubWitnessResult:
    """Minimal result object for stubbing the serial ``run_witness`` call."""

    verdict = "no_loop"
    iterations = 0
    error = None
    evidence: dict = {"kind": "stub"}


def test_analogue_parallel_candidate_matches_serial(tmp_path, monkeypatch):
    """The ``--workers>1`` path must stage the exact same Candidate as serial.

    A mismatch here is the parity hazard: extra board state (e.g. oracle texts
    or different keys) would make a worker verdict disagree with the serial one.
    """
    db = tmp_path / "research.db"
    ExperimentStore(db).close()
    _seed_cards(db)
    monkeypatch.setattr(cards_mod, "_LEGALITY", _Permissive())

    # Serial: capture every Candidate the script stages for verification.
    serial_combos: list = []
    monkeypatch.setattr(
        at.W, "build_scenario",
        lambda combo: serial_combos.append(combo) or object(),
    )
    monkeypatch.setattr(at.W, "run_witness", lambda *a, **k: _StubWitnessResult())
    monkeypatch.setattr(at, "ForgeEnvClient", _FakeHarness)
    serial_out = tmp_path / "serial.json"
    assert at.main(["--db", str(db), "--out", str(serial_out)]) == 0
    serial_by_pair = {(c.cards[0], c.cards[1]): c for c in serial_combos}
    assert serial_by_pair

    # Parallel: capture the candidate builder and rebuild the same pair.
    captured: dict = {}

    def _fake_run_batch(pairs, **kwargs):
        captured["pairs"] = list(pairs)
        captured["builder"] = kwargs["candidate_builder"]
        return [
            {"verdict": "no_loop", "executed": 0, "reason": "stub"} for _ in pairs
        ]

    monkeypatch.setattr(at.B, "run_batch", _fake_run_batch)
    parallel_out = tmp_path / "parallel.json"
    assert at.main(
        ["--db", str(db), "--out", str(parallel_out), "--workers", "4"]
    ) == 0

    assert set(captured["pairs"]) == set(serial_by_pair)
    for engine, partner in captured["pairs"]:
        built = captured["builder"](engine, partner)
        assert built == serial_by_pair[(engine, partner)]
        # The parity hazard: the parallel path must not stage oracle texts.
        assert built.oracle_texts == ()


def _seed_known_combo(db) -> None:
    conn = sqlite3.connect(db)
    for normalized in ("kiki-jiki, mirror breaker", "zealous conscripts"):
        conn.execute(
            "INSERT INTO known_combo_cards (combo_id, role, raw_name,"
            " normalized_name, import_id) VALUES (1, 'use', ?, ?, 'imp1')",
            (normalized, normalized),
        )
    conn.commit()
    conn.close()


def test_known_recall_manifest_in_json_and_run_notes(tmp_path, monkeypatch):
    """The corpus manifest is recoverable from the JSON *and* the run rows."""
    db = tmp_path / "research.db"
    ExperimentStore(db).close()
    _seed_cards(db)
    _seed_known_combo(db)
    monkeypatch.setattr(kr, "ForgeEnvClient", _FakeHarness)
    out = tmp_path / "recall_manifest.json"

    rc = kr.main(
        ["--db", str(db), "--mode", "home", "--start", "0", "--count", "3",
         "--out", str(out), "--persist", "--note", "unit-test"]
    )
    assert rc == 0

    payload = json.loads(out.read_text())
    manifest = payload["manifest"]
    assert manifest["mode"] == "home"
    assert manifest["start"] == 0
    assert manifest["count"] == 1  # only one known combo was seeded
    assert manifest["total_available"] == 1
    assert manifest["note"] == "unit-test"
    assert manifest["wall_seconds"] is not None
    assert manifest["run_rows_annotated"] == 1
    assert manifest["recall"]["n"] == 1
    assert payload["results"]

    store = ExperimentStore(db)
    try:
        runs = store.witness_runs()
    finally:
        store.close()
    assert len(runs) == 1
    # The run row's notes now carry the same manifest (minus the annotation count).
    notes = json.loads(runs[0]["notes"])
    assert notes["mode"] == "home"
    assert notes["count"] == 1
    assert notes["note"] == "unit-test"
    assert "run_rows_annotated" not in notes


def test_annotate_run_notes_only_touches_new_empty_rows(tmp_path):
    db = tmp_path / "research.db"
    store = ExperimentStore(db)
    try:
        old = store.start_witness_run(candidate_key="1", notes="keep me")
        target = store.start_witness_run(candidate_key="1", notes="")
        already = store.start_witness_run(candidate_key="1", notes="already set")
        other = store.start_witness_run(candidate_key="2", notes="")
    finally:
        store.close()

    updated = kr._annotate_run_notes(str(db), ["1"], "MANIFEST", min_run_id=old)
    assert updated == 1  # only the empty, newer row with the matching key

    store = ExperimentStore(db)
    try:
        runs = {r["id"]: r for r in store.witness_runs()}
    finally:
        store.close()
    assert runs[old]["notes"] == "keep me"
    assert runs[target]["notes"] == "MANIFEST"
    assert runs[already]["notes"] == "already set"
    assert runs[other]["notes"] == ""

