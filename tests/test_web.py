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
from combo_discovery.web.gallery import (
    PAGE_SIZE,
    Filters,
    card_colours,
    card_role,
    pairing_idea,
)
from combo_discovery.web.images import warm_images
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
]
# 15 queued hypotheses, enough to need a second page.
for _i in range(15):
    HYPOTHESES.append(
        (200 + _i, 15, [1, 2], 0.40 - _i / 100, "Kiki-Jiki copies Pestermite.")
    )

PLAYING_SCENARIO = json.dumps(
    {
        "active_player": 0,
        "turn": 1,
        "players": [
            {
                "battlefield": [
                    {"name": "Splinter Twin"},
                    {"name": "Deceiver Exarch"},
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


def _record_run(
    store: ExperimentStore,
    *,
    verdict: str,
    candidate_key: str,
    card_names: list[str],
    iterations: int = 2,
    observations: int = 0,
) -> int:
    run = store.start_witness_run(
        proto_version=8,
        policy_version="witness-v4",
        scenario_json=PLAYING_SCENARIO,
        seeds=[1],
        candidate_key=candidate_key,
        card_names=card_names,
    )
    for index in range(observations):
        store.record_witness_observation(
            run,
            index,
            turn=1,
            phase="MAIN1",
            signature=SIG_A if index % 2 == 0 else SIG_B,
            resources={"tokens": index},
        )
    store.record_witness_result(
        run,
        candidate_kind="pair",
        candidate_key=candidate_key,
        card_names=card_names,
        verdict=verdict,
        iterations=iterations,
        signature=[SIG_A, SIG_B, SIG_A],
        resource_deltas=[{"tokens": 0}, {"tokens": 1}, {"tokens": 0}],
        trace=[[1, 11, ["targets", [[126], []]]], [2, 1, ["option_id", 0]]],
        evidence={"kind": "existing_signature", "reason": "signature recurred"},
    )
    return run


def _seed_db(db_path: Path) -> dict[str, int]:
    """Schema + cards + hypotheses + the witness scenarios the UI shows."""
    ExperimentStore(db_path).close()
    conn = sqlite3.connect(db_path)
    try:
        _seed_schema_words(conn)
    finally:
        conn.close()

    store = ExperimentStore(db_path)
    ids: dict[str, int] = {}
    try:
        # Same pairing, two attempts that disagree: loops then no_loop.
        ids["loops"] = _record_run(
            store, verdict="loops", candidate_key="42",
            card_names=["Kiki-Jiki, Mirror Breaker", "Pestermite"],
            iterations=3, observations=4,
        )
        ids["mixed"] = _record_run(
            store, verdict="no_loop", candidate_key="42",
            card_names=["Kiki-Jiki, Mirror Breaker", "Pestermite"],
            iterations=2,
        )
        # A single-attempt refutation.
        ids["refuted"] = _record_run(
            store, verdict="no_loop", candidate_key="7",
            card_names=["Splinter Twin", "Little Bear"],
        )
        # A single-attempt inconclusive.
        ids["indecided"] = _record_run(
            store, verdict="inconclusive", candidate_key="9",
            card_names=["Deceiver Exarch", "Little Bear"], observations=2,
        )
        # A loop whose hypothesis row does not exist (name-fallback path).
        ids["orphan"] = _record_run(
            store, verdict="loops", candidate_key="999",
            card_names=["Kiki-Jiki, Mirror Breaker", "Deceiver Exarch"],
        )

        # An in-progress run: no result row, identified by schema v8 pair.
        run = store.start_witness_run(
            proto_version=8,
            policy_version="witness-v4",
            scenario_json=PLAYING_SCENARIO,
            seeds=[1],
            candidate_key="",
            card_names=["Splinter Twin", "Deceiver Exarch"],
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
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status, response.headers.get("Content-Type", ""), response.read().decode()


def _payload(port: int, path: str = "/api/gallery") -> dict:
    return json.loads(_get(port, path)[2])


def _tiles(column: dict) -> list[str]:
    import re

    return re.findall(r'<article class="tile.*?</article>', column["html"], re.S)


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
    for heading in ("Queued", "Playing", "Loop found", "Refuted", "Couldn't decide"):
        assert heading in text, heading
    assert "Kiki-Jiki, Mirror Breaker" in text
    assert "scryfall.com/search" in text
    # Shell: both destinations, Goldfish cue survives everywhere.
    assert "The Goldfish" in text
    assert "coming soon" in text
    assert "/api/gallery" in text
    # Narrow-width status tabs and their GET links.
    assert 'class="status-tabs"' in text
    assert "?tab=loops" in text


def test_gallery_api_is_valid_json(server) -> None:
    srv, _, _ = server
    payload = _payload(srv.server_address[1])
    assert [c["key"] for c in payload["columns"]] == [
        "queued", "playing", "loops", "refuted", "indecided",
    ]
    for column in payload["columns"]:
        assert isinstance(column["count"], int)
        assert isinstance(column["html"], str)
        assert "page" in column and "pages" in column


def test_gallery_maps_verdicts_to_columns(server) -> None:
    srv, _, _ = server
    payload = _payload(srv.server_address[1])
    by_key = {column["key"]: column for column in payload["columns"]}
    text = {key: html.unescape(column["html"]) for key, column in by_key.items()}
    assert "Loop found" in text["loops"]
    assert "Refuted" in text["refuted"]
    assert "Couldn't decide" in text["indecided"]
    assert "Splinter Twin" in text["playing"]
    assert by_key["queued"]["showing"] >= 1


# ---------------------------------------------------------------------------
# defect 1: one tile per pairing, with attempt history
# ---------------------------------------------------------------------------


def test_gallery_dedupes_pairing_and_shows_attempts(server) -> None:
    srv, ids, _ = server
    payload = _payload(srv.server_address[1])
    by_key = {column["key"]: column for column in payload["columns"]}
    # The Kiki-Jiki + Pestermite pairing was tested twice; one tile, not two.
    refuted = by_key["refuted"]["html"]
    assert refuted.count("Kiki-Jiki, Mirror Breaker</a> + <a") == 1
    assert refuted.count("2 attempts") == 1
    # Disagreement is surfaced, not hidden.
    assert "mixed results" in refuted
    # Both attempts link to their runs.
    assert f"/run/{ids['loops']}" in refuted
    assert f"/run/{ids['mixed']}" in refuted
    # Headline verdict is the latest attempt.
    assert "Refuted" in refuted


def test_gallery_orphan_result_uses_card_names(server) -> None:
    srv, ids, _ = server
    payload = _payload(srv.server_address[1])
    loops = next(c for c in payload["columns"] if c["key"] == "loops")
    assert "Deceiver Exarch" in loops["html"]
    assert "Kiki-Jiki, Mirror Breaker" in loops["html"]
    assert f"/run/{ids['orphan']}" in loops["html"]


# ---------------------------------------------------------------------------
# defect 2: honest queued counts + SQL paging
# ---------------------------------------------------------------------------


def test_gallery_pages_queued_in_sql(server) -> None:
    srv, _, _ = server
    port = srv.server_address[1]
    page1 = _payload(port)
    queued = next(c for c in page1["columns"] if c["key"] == "queued")
    assert queued["count"] == 15
    assert queued["showing"] == PAGE_SIZE
    assert queued["page"] == 1 and queued["pages"] == 2
    assert queued["start"] == 1 and queued["end"] == PAGE_SIZE
    assert queued["next_href"].endswith("page=2")
    assert "showing 1\u201312 of 15" in queued["html"].replace(",", "")

    page2 = _payload(port, "/api/gallery?page=2")
    queued2 = next(c for c in page2["columns"] if c["key"] == "queued")
    assert page2["page"] == 2
    assert queued2["showing"] == 3
    assert queued2["start"] == 13 and queued2["end"] == 15
    assert queued2["prev_href"].endswith("page=1")
    # A different page really is different content.
    assert queued["html"] != queued2["html"]


def test_gallery_pager_is_server_rendered(server) -> None:
    srv, _, _ = server
    port = srv.server_address[1]
    _, _, body = _get(port, "/?page=2")
    text = html.unescape(body)
    assert 'data-active="playing"' in text  # default tab, page preserved in links
    assert "showing 13" in text
    # Paging is a plain link, so it survives with JS disabled.
    assert 'href="/?tab=playing&amp;page=1"' in text or 'page=1' in text


# ---------------------------------------------------------------------------
# defect 4: status tabs + default tab
# ---------------------------------------------------------------------------


def test_gallery_default_tab_prefers_playing(server) -> None:
    srv, _, _ = server
    payload = _payload(srv.server_address[1])
    assert payload["tab"] == "playing"
    assert payload["counts"]["playing"] == 1
    active = [t for t in payload["tabs"] if t["active"]]
    assert len(active) == 1 and active[0]["key"] == "playing"


def test_gallery_tabs_are_get_links_with_counts(server) -> None:
    srv, _, _ = server
    payload = _payload(srv.server_address[1])
    tabs = {t["key"]: t for t in payload["tabs"]}
    assert set(tabs) == {"queued", "playing", "loops", "refuted", "indecided"}
    assert tabs["loops"]["count"] == 1
    assert tabs["loops"]["href"] == "/?tab=loops"
    assert 'data-tab-count="loops"' in _get(srv.server_address[1], "/")[2]


# ---------------------------------------------------------------------------
# defect 5: chips, ideas, no dead controls
# ---------------------------------------------------------------------------


def test_gallery_has_one_chip_per_status(server) -> None:
    srv, _, _ = server
    body = _get(srv.server_address[1], "/")[2]
    # Every tile footer uses the same chip component, one per status present.
    for status in ("loops", "refuted", "indecided", "queued", "playing"):
        assert f"chip chip-{status}" in body


def test_gallery_has_no_dead_filter_controls(server) -> None:
    srv, _, _ = server
    text = html.unescape(_get(srv.server_address[1], "/")[2])
    # Colour and type are derived honestly; mana value and set/year are not
    # offered because they cannot be derived, so no control is shown.
    assert "Colour: any" in text and "Card type: any" in text
    assert "Mana value" not in text
    assert "Set: any" not in text
    assert "Year" not in text


def test_gallery_filters_are_pushed_to_sql(server) -> None:
    srv, _, _ = server
    port = srv.server_address[1]
    # White does not appear in any seeded card, so Queued must empty out: the
    # filter runs in SQL over the real set, not over an already-fetched sample.
    payload = _payload(port, "/api/gallery?color=W&type=Creature")
    assert payload["filters"] == {"colours": ["W"], "type": "Creature"}
    assert payload["counts"]["queued"] == 0
    assert payload["counts"]["queued"] < _payload(port)["counts"]["queued"]


def test_pairing_idea_is_plain_words() -> None:
    assert pairing_idea("q:infinite_etb_loop") == "free copy + enters-the-battlefield untapper"
    assert pairing_idea("q:any_cycle") == "repeating board state"
    assert pairing_idea("", "Copy engine plus an enters-the-battlefield untapper.") == (
        "Copy engine plus an enters-the-battlefield untapper"
    )
    assert pairing_idea("unknown_pattern") == "idea not recorded"


def test_card_role_is_specific() -> None:
    kiki = {"oracle_text": "{T}: Create a token that's a copy of target creature.",
            "type_line": "Legendary Creature Goblin"}
    untapper = {"oracle_text": "When this enters, untap target permanent.",
                "type_line": "Creature Cleric"}
    combat = {"oracle_text": "After this phase, there is an additional combat phase.",
              "type_line": "Enchantment Aura"}
    blank = {"oracle_text": "Flying.", "type_line": "Creature Bear"}
    assert card_role(kiki) == "copy engine"
    assert card_role(untapper) == "enters-the-battlefield untapper"
    assert card_role(combat) == "extra combats"
    assert card_role(blank) == ""


def test_card_colours() -> None:
    assert card_colours("U R") == "UR"
    assert card_colours("2 G") == "G"
    assert card_colours("no cost") == ""


def test_filters_parse_and_match() -> None:
    assert Filters.from_query({}).active is False
    assert Filters.from_query({"color": ["U"]}).colours == ("U",)
    assert Filters.from_query({"type": ["Creature"]}).type == "Creature"
    assert Filters.from_query({"type": ["nonsense"]}).type == ""

    card = {"colours": "U", "types": {"Creature"}}
    assert Filters.from_query({}).matches([card]) is True
    assert Filters.from_query({"color": ["U"]}).matches([card]) is True
    assert Filters.from_query({"color": ["R"]}).matches([card]) is False
    assert Filters.from_query({"type": ["Creature"]}).matches([card]) is True
    assert Filters.from_query({"type": ["Land"]}).matches([card]) is False


# ---------------------------------------------------------------------------
# images: always render something, enlarge on demand
# ---------------------------------------------------------------------------


def test_images_render_fallback_when_none(server, monkeypatch) -> None:
    monkeypatch.setattr("combo_discovery.web.gallery.image_url", lambda *a, **k: None)
    srv, _, _ = server
    body = _get(srv.server_address[1], "/")[2]
    assert "thumb-fallback" in body
    assert '<img src="http' not in body
    assert 'data-large=' not in body
    assert "Kiki-Jiki, Mirror Breaker" in body


def test_images_render_when_available(server, monkeypatch) -> None:
    def fake(name, face=0, size="normal"):
        return f"https://img.test/{size}/{name.replace(' ', '-')}.jpg"

    monkeypatch.setattr("combo_discovery.web.gallery.image_url", fake)
    srv, _, _ = server
    body = _get(srv.server_address[1], "/")[2]
    assert '<img src="https://img.test/small/' in body
    assert 'loading="lazy"' in body
    assert 'width="122" height="170"' in body
    # The enlarged card is the large size, reachable by keyboard button.
    assert 'data-large="https://img.test/large/' in body
    assert "Click to see the full card" in body
    assert 'id="lightbox"' in body


def test_warm_images_returns_seconds() -> None:
    assert warm_images() >= 0.0


# ---------------------------------------------------------------------------
# deleted + retained routes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/candidates", "/api/feed", "/api/candidates",
                                  "/api/candidate/42"])
def test_deleted_routes_are_gone(server, path: str) -> None:
    srv, _, _ = server
    with pytest.raises(urllib.error.HTTPError) as error:
        _get(srv.server_address[1], path)
    assert error.value.code == 404


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_non_get_is_405(server, method: str) -> None:
    srv, _, _ = server
    request = urllib.request.Request(
        f"http://127.0.0.1:{srv.server_address[1]}/", data=b"x", method=method
    )
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request, timeout=10)
    assert error.value.code == 405


def test_run_detail_bridge(server) -> None:
    srv, ids, _ = server
    status, _, body = _get(srv.server_address[1], f"/run/{ids['loops']}")
    assert status == 200
    assert "Loop found." in body
    assert CYCLE_MARKER in body


def test_candidate_bridge(server) -> None:
    srv, _, _ = server
    status, _, body = _get(srv.server_address[1], "/candidate/42")
    assert status == 200
    assert "What we think this does" in body
    assert "Kiki-Jiki, Mirror Breaker" in body


def test_api_run_still_returns_observations(server) -> None:
    srv, ids, _ = server
    status, _, body = _get(srv.server_address[1], f"/api/run/{ids['loops']}")
    assert status == 200
    payload = json.loads(body)
    assert payload["result"]["verdict"] == "loops"


def test_unknown_path_404(server) -> None:
    srv, _, _ = server
    with pytest.raises(urllib.error.HTTPError) as error:
        _get(srv.server_address[1], "/nope")
    assert error.value.code == 404


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
