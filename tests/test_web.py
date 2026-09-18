"""Tests for the read-only Gallery web console.

The server is started on an ephemeral port against a temporary SQLite database
built through the real :class:`ExperimentStore`, then driven with ``urllib``.
"""

from __future__ import annotations

import html
import json
import sqlite3
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from combo_discovery.store import ExperimentStore
from combo_discovery.web import theme as web_theme
from combo_discovery.web.app import create_server
from combo_discovery.web.db import ReadOnlyStore, open_read_only
from combo_discovery.web.gallery import Filters, card_colours, mana_value, pairing_idea
from combo_discovery.web.render import CYCLE_MARKER

SIG_A = "a" * 64
SIG_B = "b" * 64

# (id, name, mana_cost, type_line, oracle_text)
CARDS = [
    (1, "Kiki-Jiki, Mirror Breaker", "2 R R R", "Legendary Creature Goblin",
     "{T}: Create a token that's a copy of target nonlegendary creature you control."),
    (2, "Pestermite", "2 U", "Creature Faerie Wizard",
     "Flash. Flying. When Pestermite enters, tap or untap target permanent."),
    (3, "Splinter Twin", "2 R R", "Enchantment Aura",
     'Enchanted creature has "{T}: Create a token that\'s a copy of this creature."'),
    (4, "Little Bear", "1 G", "Creature Bear", ""),
    (5, "Deceiver Exarch", "2 U", "Creature Cleric",
     "When Deceiver Exarch enters, untap target permanent."),
]

# (id, pattern_id, card_ids, score, mechanism)
HYPOTHESES = [
    (42, 15, [1, 2], 0.97, "Kiki-Jiki copies Pestermite (COPIES_CREATURE~ETB_TRIGGER)."),
    (7, 15, [3, 4], 0.90, "Splinter Twin copies Little Bear (COPIES_CREATURE)."),
    (9, 16, [5, 4], 0.50, "Deceiver Exarch sacrifices Little Bear (SACRIFICE)."),
    (100, 15, [1, 4], 0.80, "Kiki-Jiki copies Little Bear (COPIES_CREATURE)."),
    (101, 15, [3, 2], 0.75, "Splinter Twin copies Pestermite (COPIES_CREATURE)."),
    (102, 16, [5, 2], 0.70, "Little Bear recursion (RECURSION)."),
]

PLAYING_SCENARIO = json.dumps(
    {
        "active_player": 0,
        "turn": 1,
        "players": [
            {
                "battlefield": [
                    {"name": "Kiki-Jiki, Mirror Breaker"},
                    {"name": "Pestermite"},
                ],
                "library": [],
            }
        ],
    }
)


def _seed_schema_words(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO import_runs (import_id, started_at, scryfall_source)"
        " VALUES ('imp1', '2026-01-01T00:00:00+00:00', 'forge_script')"
    )
    conn.executemany(
        "INSERT INTO cards (id, import_id, file_sha256, name, normalized_name,"
        " mana_cost, type_line, oracle_text, effect_count, set_code, rarity)"
        " VALUES (?, 'imp1', 'sha', ?, ?, ?, ?, ?, 0, NULL, NULL)",
        [
            (cid, name, name.lower(), mana, type_line, oracle)
            for cid, name, mana, type_line, oracle in CARDS
        ],
    )
    conn.executemany(
        "INSERT INTO patterns (id, name, description, pattern_json, version)"
        " VALUES (?, ?, ?, '{}', 1)",
        [
            (15, "infinite_etb_loop",
             "Copy engine plus an enters-the-battlefield untapper."),
            (16, "sacrifice_recursion", "Sacrifice outlet plus graveyard recursion."),
        ],
    )
    conn.executemany(
        "INSERT INTO combo_hypotheses (id, import_id, pattern_id, card_ids_json,"
        " mechanism, score, status, created_at)"
        " VALUES (?, 'imp1', ?, ?, ?, ?, 'proposed', '2026-01-02T00:00:00+00:00')",
        [
            (hid, pattern, json.dumps(card_ids), mechanism, score)
            for hid, pattern, card_ids, score, mechanism in HYPOTHESES
        ],
    )
    conn.commit()


def _seed_db(db_path: Path) -> dict[str, int]:
    """Schema + cards + hypotheses + the five witness scenarios the UI shows."""
    ExperimentStore(db_path).close()
    conn = sqlite3.connect(db_path)
    try:
        _seed_schema_words(conn)
    finally:
        conn.close()

    store = ExperimentStore(db_path)
    ids: dict[str, int] = {}
    try:
        # loops run, with per-step observations + evidence + trace
        run = store.start_witness_run(
            engine_commit="deadbeef",
            proto_version=7,
            policy_version="witness-v4",
            scenario_json=PLAYING_SCENARIO,
            seeds=[1],
            params={"max_iterations": 4, "max_decisions": 256},
        )
        ids["loops"] = run
        for iteration, turn, signature, resources in (
            (0, 1, SIG_A, {"tokens": 0, "permanents": 2, "mana": 8, "life": 20,
                           "graveyard": 0, "library": 40, "hand": 2}),
            (1, 1, SIG_B, {"tokens": 1, "permanents": 3, "mana": 8, "life": 20,
                           "graveyard": 0, "library": 40, "hand": 2}),
            (2, 1, SIG_A, {"tokens": 1, "permanents": 3, "mana": 8, "life": 20,
                           "graveyard": 0, "library": 40, "hand": 2}),
            (3, 1, SIG_A, {"tokens": 3, "permanents": 5, "mana": 8, "life": 20,
                           "graveyard": 0, "library": 40, "hand": 2}),
        ):
            store.record_witness_observation(
                run, iteration, turn=turn, phase="MAIN1",
                signature=signature, resources=resources,
            )
        store.record_witness_result(
            run,
            candidate_kind="pair",
            candidate_key="42",
            card_names=["Kiki-Jiki, Mirror Breaker", "Pestermite"],
            verdict="loops",
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
                "link_hits": [1], "link_misses": [0], "trigger_hits": [1],
                "executed_actions": 2, "decisions": 3,
            },
        )

        # no_loop run with no per-step observations (fallback path)
        run = store.start_witness_run(
            proto_version=7, policy_version="witness-v2",
            scenario_json="{}", seeds=[1],
        )
        ids["refuted"] = run
        store.record_witness_result(
            run,
            candidate_kind="pair",
            candidate_key="7",
            card_names=["Splinter Twin", "Little Bear"],
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

        # inconclusive run
        run = store.start_witness_run(
            proto_version=7, policy_version="witness-v4",
            scenario_json="{}", seeds=[1],
        )
        ids["indecided"] = run
        for iteration, signature in ((0, SIG_A), (1, SIG_B)):
            store.record_witness_observation(
                run, iteration, turn=1, phase="MAIN1",
                signature=signature, resources={"tokens": 0},
            )
        store.record_witness_result(
            run,
            candidate_kind="pair",
            candidate_key="9",
            card_names=["Deceiver Exarch", "Little Bear"],
            verdict="inconclusive",
            iterations=2,
            signature=[SIG_A, SIG_B],
            resource_deltas=[{"tokens": 0}, {"tokens": 0}],
            evidence={
                "reason": "policy never matched an offered option to a link source; "
                "no loop action was executed"
            },
        )

        # loop result whose hypothesis row does NOT exist (name fallback path)
        run = store.start_witness_run(
            proto_version=7, policy_version="witness-v4",
            scenario_json="{}", seeds=[1],
        )
        ids["orphan"] = run
        store.record_witness_result(
            run,
            candidate_kind="pair",
            candidate_key="999",
            card_names=["Kiki-Jiki, Mirror Breaker", "Deceiver Exarch"],
            verdict="loops",
            iterations=2,
            signature=[SIG_A, SIG_A],
            resource_deltas=[{"tokens": 0}, {"tokens": 2}],
            evidence={"kind": "degenerate"},
        )

        # an in-progress run: no result row, so it shows as Playing
        run = store.start_witness_run(
            proto_version=7, policy_version="witness-v4",
            scenario_json=PLAYING_SCENARIO, seeds=[1],
        )
        ids["playing"] = run
        store.record_witness_observation(
            run, 0, turn=1, phase="MAIN1", signature=SIG_A, resources={"tokens": 0}
        )
    finally:
        store.close()
    return ids


def _start_server(db_path: Path):
    srv = create_server(str(db_path), host="127.0.0.1", port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    return srv


@pytest.fixture
def server(tmp_path: Path):
    db_path = tmp_path / "research.db"
    ids = _seed_db(db_path)
    srv = _start_server(db_path)
    try:
        yield srv, ids, db_path
    finally:
        srv.shutdown()
        srv.server_close()
        srv.store.close()


def _get(port: int, path: str) -> tuple[int, str, str]:
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, response.headers.get("Content-Type", ""), response.read().decode()


# ---------------------------------------------------------------------------
# The Gallery (home)
# ---------------------------------------------------------------------------


def test_gallery_home_renders_board(server) -> None:
    srv, _, _ = server
    port = srv.server_address[1]
    status, content_type, body = _get(port, "/")
    assert status == 200
    assert "text/html" in content_type
    text = html.unescape(body)
    assert "The Gallery" in text
    assert 'id="gallery-board"' in text
    # Five columns, in player words.
    for heading in ("Queued", "Playing", "Loop found", "Refuted", "Couldn't decide"):
        assert heading in text, heading
    # Cards carry the tiles, and link out to Scryfall.
    assert "Kiki-Jiki, Mirror Breaker" in text
    assert "scryfall.com/search" in text
    # Shell: both destinations, Goldfish not yet shipped.
    assert "The Goldfish" in text
    assert "coming soon" in text
    # Poll endpoint is wired in.
    assert "/api/gallery" in text


def test_gallery_api_is_valid_json(server) -> None:
    srv, _, _ = server
    port = srv.server_address[1]
    status, content_type, body = _get(port, "/api/gallery")
    assert status == 200
    assert "application/json" in content_type
    payload = json.loads(body)
    keys = [column["key"] for column in payload["columns"]]
    assert keys == ["queued", "playing", "loops", "refuted", "indecided"]
    for column in payload["columns"]:
        assert isinstance(column["count"], int)
        assert isinstance(column["html"], str)
    assert payload["counts"]["loops"] >= 1


def test_gallery_maps_verdicts_to_columns(server) -> None:
    srv, _, _ = server
    port = srv.server_address[1]
    payload = json.loads(_get(port, "/api/gallery")[2])
    by_key = {column["key"]: column for column in payload["columns"]}
    text = {key: html.unescape(column["html"]) for key, column in by_key.items()}
    assert "Loop found" in text["loops"]
    assert "Refuted" in text["refuted"]
    assert "Couldn't decide" in text["indecided"]
    # The in-progress run shows in Playing.
    assert "Pestermite" in text["playing"]
    # Queued is populated from hypotheses with no result.
    assert by_key["queued"]["showing"] >= 1


def test_gallery_orphan_result_uses_card_names(server) -> None:
    """A result whose hypothesis row is gone still gets a tile, via card names."""
    srv, ids, _ = server
    port = srv.server_address[1]
    payload = json.loads(_get(port, "/api/gallery")[2])
    by_key = {column["key"]: column for column in payload["columns"]}
    assert "Deceiver Exarch" in by_key["loops"]["html"]
    assert "Kiki-Jiki, Mirror Breaker" in by_key["loops"]["html"]
    assert f"/run/{ids['orphan']}" in by_key["loops"]["html"]


def test_gallery_endpoint_accepts_filters(server) -> None:
    srv, _, _ = server
    port = srv.server_address[1]
    status, _, body = _get(port, "/api/gallery?color=U&type=Creature&mv=2")
    assert status == 200
    payload = json.loads(body)
    assert payload["filters"] == {"colours": ["U"], "mv": "2", "type": "Creature"}


def test_gallery_filters_are_in_magic_vocabulary(server) -> None:
    srv, _, _ = server
    port = srv.server_address[1]
    _, _, body = _get(port, "/")
    assert "Colour" in body and "Mana value" in body and "Card type" in body
    assert "White" in body and "Blue" in body and "Creature" in body


# ---------------------------------------------------------------------------
# images: always render something
# ---------------------------------------------------------------------------


def test_images_render_fallback_when_none(server, monkeypatch) -> None:
    monkeypatch.setattr("combo_discovery.web.gallery.image_url", lambda *a, **k: None)
    srv, _, _ = server
    port = srv.server_address[1]
    _, _, body = _get(port, "/")
    assert "thumb-fallback" in body
    assert '<img src="http' not in body
    assert 'data-large=' not in body
    assert "Kiki-Jiki, Mirror Breaker" in body  # name still legible


def test_images_render_when_available(server, monkeypatch) -> None:
    def fake(name, face=0, size="normal"):
        return f"https://img.test/{size}/{name.replace(' ', '-')}.jpg"

    monkeypatch.setattr("combo_discovery.web.gallery.image_url", fake)
    srv, _, _ = server
    port = srv.server_address[1]
    _, _, body = _get(port, "/")
    assert '<img src="https://img.test/small/' in body
    assert 'loading="lazy"' in body
    assert 'width="122" height="170"' in body
    assert 'data-large="https://img.test/large/' in body


# ---------------------------------------------------------------------------
# deleted + retained routes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/candidates", "/api/feed", "/api/candidates",
                                  "/api/candidate/42"])
def test_deleted_routes_are_gone(server, path: str) -> None:
    srv, _, _ = server
    port = srv.server_address[1]
    with pytest.raises(urllib.error.HTTPError) as error:
        _get(port, path)
    assert error.value.code == 404


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_non_get_is_405(server, method: str) -> None:
    srv, _, _ = server
    port = srv.server_address[1]
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/", data=b"x", method=method
    )
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request, timeout=5)
    assert error.value.code == 405


def test_run_detail_bridge(server) -> None:
    srv, ids, _ = server
    port = srv.server_address[1]
    status, _, body = _get(port, f"/run/{ids['loops']}")
    assert status == 200
    assert "Loop found." in body
    assert CYCLE_MARKER in body


def test_candidate_bridge(server) -> None:
    srv, _, _ = server
    port = srv.server_address[1]
    status, _, body = _get(port, "/candidate/42")
    assert status == 200
    assert "What we think this does" in body
    assert "Kiki-Jiki, Mirror Breaker" in body


def test_api_run_still_returns_observations(server) -> None:
    srv, ids, _ = server
    port = srv.server_address[1]
    status, _, body = _get(port, f"/api/run/{ids['loops']}")
    assert status == 200
    payload = json.loads(body)
    assert len(payload["observations"]) == 4
    assert payload["result"]["verdict"] == "loops"


def test_unknown_path_404(server) -> None:
    srv, _, _ = server
    port = srv.server_address[1]
    with pytest.raises(urllib.error.HTTPError) as error:
        _get(port, "/nope")
    assert error.value.code == 404


# ---------------------------------------------------------------------------
# helpers: plain words, colours, mana value
# ---------------------------------------------------------------------------


def test_pairing_idea_is_plain_words() -> None:
    assert pairing_idea("q:infinite_etb_loop") == "free copy + enters-the-battlefield untapper"
    assert pairing_idea("q:any_cycle") == "repeating board state"
    assert pairing_idea("", "Copy engine plus an enters-the-battlefield untapper.") == (
        "Copy engine plus an enters-the-battlefield untapper"
    )
    assert pairing_idea("unknown_pattern") == "pairing to check"


def test_mana_value_and_colours() -> None:
    assert mana_value("2 G") == 3
    assert mana_value("U R") == 2
    assert mana_value("no cost") == 0
    assert mana_value("") == 0
    assert card_colours("U R") == "UR"
    assert card_colours("2 G") == "G"
    assert card_colours("no cost") == ""


def test_filters_parse_and_match() -> None:
    assert Filters.from_query({}).active is False
    assert Filters.from_query({"color": ["U"]}).colours == ("U",)
    assert Filters.from_query({"mv": ["2"]}).mv == "2"
    assert Filters.from_query({"type": ["Creature"]}).type == "Creature"
    assert Filters.from_query({"mv": ["nonsense"]}).mv == ""

    card = {"colours": "U", "mv_bucket": "2", "types": {"Creature"}}
    assert Filters.from_query({}).matches([card]) is True
    assert Filters.from_query({"color": ["U"]}).matches([card]) is True
    assert Filters.from_query({"color": ["R"]}).matches([card]) is False
    assert Filters.from_query({"mv": ["2"]}).matches([card]) is True
    assert Filters.from_query({"mv": ["3"]}).matches([card]) is False
    assert Filters.from_query({"type": ["Creature"]}).matches([card]) is True
    assert Filters.from_query({"type": ["Land"]}).matches([card]) is False


# ---------------------------------------------------------------------------
# read-only guarantees
# ---------------------------------------------------------------------------


def test_open_read_only_rejects_writes(tmp_path: Path) -> None:
    db_path = tmp_path / "research.db"
    _seed_db(db_path)
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
    assert srv.store.one("PRAGMA query_only") == {"query_only": 1}
    with pytest.raises(sqlite3.OperationalError):
        srv.store._conn.execute("DELETE FROM witness_runs")  # noqa: SLF001


def test_readonly_store_hides_missing_tables(tmp_path: Path) -> None:
    # An empty database is a graceful first-run state, not a crash.
    db_path = tmp_path / "empty.db"
    ExperimentStore(db_path).close()
    store = ReadOnlyStore(db_path)
    try:
        assert store.hypothesis(999) is None
        assert store.list_runs() == []
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
