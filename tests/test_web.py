"""Tests for the read-only witness web console.

The server is started on an ephemeral port against a temporary SQLite database
built through the real :class:`ExperimentStore`, then driven with ``urllib``.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from combo_discovery.store import ExperimentStore
from combo_discovery.web.app import create_server
from combo_discovery.web.db import ReadOnlyStore, open_read_only
from combo_discovery.web.render import CYCLE_MARKER
from combo_discovery.web import theme as web_theme

SIG_A = "a" * 64
SIG_B = "b" * 64


def _seed_witness_db(db_path: Path) -> int:
    """One run + result + observations whose signature recurs (a cycle)."""
    store = ExperimentStore(db_path)
    try:
        run_id = store.start_witness_run(
            engine_commit="deadbeef",
            proto_version=7,
            policy_version="witness-v4",
            scenario_json="{}",
            seeds=[1],
            params={"max_iterations": 4, "max_decisions": 256},
        )
        samples = [
            (0, 1, "MAIN1", SIG_A, {"tokens": 0, "permanents": 2, "mana": 8, "life": 20,
                                     "graveyard": 0, "library": 40, "hand": 2}),
            (1, 1, "MAIN1", SIG_B, {"tokens": 1, "permanents": 3, "mana": 8, "life": 20,
                                     "graveyard": 0, "library": 40, "hand": 2}),
            (2, 1, "MAIN1", SIG_A, {"tokens": 1, "permanents": 3, "mana": 8, "life": 20,
                                     "graveyard": 0, "library": 40, "hand": 2}),
            (3, 1, "MAIN1", SIG_A, {"tokens": 3, "permanents": 5, "mana": 8, "life": 20,
                                     "graveyard": 0, "library": 40, "hand": 2}),
        ]
        for iteration, turn, phase, signature, resources in samples:
            store.record_witness_observation(
                run_id,
                iteration,
                turn=turn,
                phase=phase,
                signature=signature,
                resources=resources,
            )
        store.record_witness_result(
            run_id,
            candidate_kind="pair",
            candidate_key="42",
            card_names=["Kiki-Jiki, Mirror Breaker", "Pestermite"],
            verdict="loops",
            infinite=False,
            iterations=3,
            signature=[SIG_A, SIG_B, SIG_A, SIG_A],
            resource_deltas=[
                {"tokens": 0, "permanents": 2, "mana": 8, "life": 20},
                {"tokens": 1, "permanents": 1, "mana": 0, "life": 0},
                {"tokens": 0, "permanents": 0, "mana": 0, "life": 0},
                {"tokens": 2, "permanents": 2, "mana": 0, "life": 0},
            ],
            trace=[[1, 11, ["targets", [[126], []]]], [2, 1, ["option_id", 0]]],
            evidence={"kind": "existing_signature", "reason": "signature recurred",
                      "grown": {"tokens": 2}},
            diagnostics={
                "links": ["Kiki-Jiki, Mirror Breaker -> Pestermite [synthetic]"],
                "link_hits": [1],
                "link_misses": [0],
                "trigger_hits": [1],
                "executed_actions": 2,
                "decisions": 3,
            },
        )
    finally:
        store.close()
    return run_id


def _seed_fallback_db(db_path: Path) -> int:
    """A legacy run with no per-step observations (result columns only)."""
    store = ExperimentStore(db_path)
    try:
        run_id = store.start_witness_run(
            proto_version=7,
            policy_version="witness-v2",
            scenario_json="{}",
            seeds=[1],
        )
        store.record_witness_result(
            run_id,
            candidate_kind="pair",
            candidate_key="7",
            card_names=["Kiki-Jiki, Mirror Breaker", "Deceiver Exarch"],
            verdict="no_loop",
            iterations=2,
            signature=[SIG_A, SIG_B, SIG_A],
            resource_deltas=[
                {"tokens": 0, "permanents": 2},
                {"tokens": 1, "permanents": 1},
                {"tokens": 0, "permanents": 0},
            ],
            evidence={"reason": "signature recurred but no tracked resource grew"},
        )
    finally:
        store.close()
    return run_id


@pytest.fixture
def server(tmp_path: Path):
    db_path = tmp_path / "research.db"
    run_id = _seed_witness_db(db_path)
    srv = create_server(str(db_path), host="127.0.0.1", port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv, run_id, db_path
    finally:
        srv.shutdown()
        srv.server_close()
        srv.store.close()


def _get(port: int, path: str) -> tuple[int, str, str]:
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, response.headers.get("Content-Type", ""), response.read().decode()


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def test_api_feed_returns_run(server) -> None:
    srv, run_id, _ = server
    port = srv.server_address[1]
    status, content_type, body = _get(port, "/api/feed")
    assert status == 200
    assert "application/json" in content_type
    payload = json.loads(body)
    assert payload["count"] >= 1
    ids = {row["run_id"] for row in payload["results"]}
    assert run_id in ids
    top = payload["results"][0]
    assert top["verdict"] == "loops"
    assert top["observation_count"] == 4
    assert "Kiki-Jiki, Mirror Breaker" in top["cards"]


def test_api_run_returns_observations(server) -> None:
    srv, run_id, _ = server
    port = srv.server_address[1]
    for path in (f"/api/run/{run_id}", f"/api/runs/{run_id}"):
        status, _, body = _get(port, path)
        assert status == 200
        payload = json.loads(body)
        assert payload["run"]["id"] == run_id
        assert len(payload["observations"]) == 4
        assert payload["observations"][0]["signature"] == SIG_A
        assert payload["result"]["verdict"] == "loops"
        assert payload["evidence"]["reason"] == "signature recurred"
        assert payload["cycles"], "a recurring signature must yield a cycle"


def test_api_candidates_reachable(server) -> None:
    srv, _, _ = server
    port = srv.server_address[1]
    status, _, body = _get(port, "/api/candidates?limit=5")
    assert status == 200
    assert "candidates" in json.loads(body)


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------


def test_run_page_has_verdict_and_cycle(server) -> None:
    srv, run_id, _ = server
    port = srv.server_address[1]
    status, content_type, body = _get(port, f"/run/{run_id}")
    assert status == 200
    assert "text/html" in content_type
    # The verdict text and the highlighted cycle marker must both be present.
    assert "loops" in body
    assert CYCLE_MARKER in body
    assert "cycle-back-edge" in body
    # The reason travels with the cycle, never alone.
    assert "signature recurred" in body
    # Graph + observation table + timeline rendered.
    assert "baseline" in body
    assert "CHOOSE_TARGETS" in body


def test_run_page_fallback_without_observations(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    run_id = _seed_fallback_db(db_path)
    srv = create_server(str(db_path), host="127.0.0.1", port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        port = srv.server_address[1]
        status, _, body = _get(port, f"/run/{run_id}")
        assert status == 200
        assert "--persist" in body
        assert CYCLE_MARKER in body
        assert "no loop" in body
    finally:
        srv.shutdown()
        srv.server_close()
        srv.store.close()


def test_feed_page_has_live_hook(server) -> None:
    srv, _, _ = server
    port = srv.server_address[1]
    status, _, body = _get(port, "/")
    assert status == 200
    assert 'id="feed-body"' in body
    assert "/api/feed" in body


def test_unknown_path_404(server) -> None:
    srv, _, _ = server
    port = srv.server_address[1]
    with pytest.raises(urllib.error.HTTPError) as error:
        _get(port, "/nope")
    assert error.value.code == 404


# ---------------------------------------------------------------------------
# read-only guarantees
# ---------------------------------------------------------------------------


def test_open_read_only_rejects_writes(tmp_path: Path) -> None:
    db_path = tmp_path / "research.db"
    _seed_witness_db(db_path)
    conn = open_read_only(db_path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE forbidden (x INTEGER)")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO witness_runs (started_at) VALUES ('now')")
    finally:
        conn.close()


def test_server_connection_is_query_only(server) -> None:
    srv, _, _ = server
    row = srv.store.one("PRAGMA query_only")
    assert row == {"query_only": 1}
    # And the URI itself was opened mode=ro, so a write raises.
    with pytest.raises(sqlite3.OperationalError):
        srv.store._conn.execute("DELETE FROM witness_runs")  # noqa: SLF001


def test_readonly_store_hides_missing_tables(tmp_path: Path) -> None:
    # An empty database is a graceful first-run state, not a crash.
    db_path = tmp_path / "empty.db"
    ExperimentStore(db_path).close()
    store = ReadOnlyStore(db_path)
    try:
        assert store.feed() == []
        assert store.hypothesis(999) is None
    finally:
        store.close()


# ---------------------------------------------------------------------------
# visual identity
# ---------------------------------------------------------------------------


def test_palette_matches_tui_theme() -> None:
    """The web palette is the same "omarchy" palette as the Textual console."""
    from combo_discovery.tui import theme as tui_theme

    for name in web_theme.SHARED_NAMES:
        assert getattr(web_theme, name) == getattr(tui_theme, name), name
