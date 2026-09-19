"""Tests for the read-only Gallery web console.

The server is started on an ephemeral port against a temporary SQLite database
built through the real :class:`ExperimentStore`, then driven with ``urllib``.
"""

from __future__ import annotations

import html
import json
import re
import sqlite3
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from combo_discovery.corpus.names import normalize_card_name
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
from combo_discovery.web.goldfish import (
    build_goldfish,
    build_loop_graph,
    reconstruct_board,
    render_counters,
    render_play_by_play,
)
from combo_discovery.web.images import warm_images

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
            (cid, name, normalize_card_name(name), mana, type_line, oracle)
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


# -- goldfish seeds ---------------------------------------------------------

GF_KIKI = "Kiki-Jiki, Mirror Breaker"
GF_EXARCH = "Deceiver Exarch"


def _record_loop_run(store: ExperimentStore) -> int:
    """A finished Kiki-Jiki + Deceiver Exarch loop, events and observations."""
    run = store.start_witness_run(
        proto_version=8,
        policy_version="witness-v4",
        scenario_json=PLAYING_SCENARIO,
        seeds=[1],
        candidate_key="42",
        card_names=[GF_KIKI, GF_EXARCH],
    )
    store.record_witness_event(
        run, "other", f"{GF_KIKI} enters the battlefield", turn=1, phase="MAIN1",
        actor=GF_KIKI, detail={"from": "from=Hand;to=Battlefield"},
    )
    store.record_witness_event(
        run, "other", f"{GF_EXARCH} enters the battlefield", turn=1, phase="MAIN1",
        actor=GF_EXARCH, detail={"from": "from=Hand;to=Battlefield"},
    )
    store.record_witness_observation(
        run, 0, turn=1, phase="MAIN1", signature=SIG_A,
        resources={"tokens": 0, "permanents": 2, "mana": 48},
    )
    for index in (1, 2, 3):
        store.record_witness_event(
            run, "other", f"{GF_KIKI} becomes tapped", turn=1, phase="MAIN1",
            actor=GF_KIKI, detail={"tapped": True},
        )
        store.record_witness_event(
            run, "activate", f"{GF_KIKI} activates, targeting {GF_EXARCH}",
            turn=1, phase="MAIN1", actor=GF_KIKI, target=GF_EXARCH,
        )
        store.record_witness_event(
            run, "token", f"{GF_EXARCH} enters the battlefield", turn=1, phase="MAIN1",
            actor=GF_EXARCH, detail={"from": "from=null;to=Battlefield"},
        )
        store.record_witness_event(
            run, "copy", f"{GF_KIKI} copies {GF_EXARCH}", turn=1, phase="MAIN1",
            actor=GF_KIKI, target=GF_EXARCH,
        )
        store.record_witness_event(
            run, "trigger", f"{GF_EXARCH} triggers, targeting {GF_KIKI}",
            turn=1, phase="MAIN1", actor=GF_EXARCH, target=GF_KIKI,
        )
        store.record_witness_event(
            run, "untap", f"{GF_KIKI} untaps", turn=1, phase="MAIN1",
            actor=GF_KIKI, detail={"tapped": False},
        )
        store.record_witness_event(
            run, "iteration", f"Loop iteration {index}", turn=1, phase="MAIN1",
            detail={"iteration": index},
        )
        store.record_witness_observation(
            run, index, turn=1, phase="MAIN1", signature=SIG_A,
            resources={"tokens": index, "permanents": 2 + index, "mana": 48},
        )
    store.record_witness_event(
        run, "verdict", "Verdict: loops", turn=1, phase="MAIN1",
        detail={"verdict": "loops", "iterations": 3},
    )
    store.record_witness_result(
        run,
        candidate_kind="pair",
        candidate_key="42",
        card_names=[GF_KIKI, GF_EXARCH],
        verdict="loops",
        iterations=3,
        signature=[SIG_A, SIG_A, SIG_A, SIG_A],
        resource_deltas=[{"tokens": 0}] * 4,
        trace=[],
        evidence={"kind": "existing_signature", "reason": "signature recurred"},
    )
    return run


def _record_live_run(store: ExperimentStore) -> int:
    """A run still in flight: narration streams, no result row."""
    run = store.start_witness_run(
        proto_version=8,
        policy_version="witness-v4",
        scenario_json=PLAYING_SCENARIO,
        seeds=[7],
        candidate_key="7",
        card_names=["Splinter Twin", GF_EXARCH],
    )
    store.record_witness_event(
        run, "play_land", "Mountain enters the battlefield", turn=1, phase="MAIN1",
        actor="Mountain",
    )
    store.record_witness_event(
        run, "other", "Splinter Twin enters the battlefield", turn=1, phase="MAIN1",
        actor="Splinter Twin", detail={"from": "from=Hand;to=Battlefield"},
    )
    store.record_witness_observation(
        run, 0, turn=1, phase="MAIN1", signature=SIG_B,
        resources={"tokens": 0, "permanents": 2, "mana": 3},
    )
    return run



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


@pytest.fixture
def gf_server(tmp_path: Path):
    """The Gallery seed plus a finished and a live Kiki-Jiki loop."""
    db_path = tmp_path / "research.db"
    ids = _seed_db(db_path)
    store = ExperimentStore(db_path)
    try:
        ids["loop"] = _record_loop_run(store)
        ids["live"] = _record_live_run(store)
    finally:
        store.close()
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


def _text(body: str) -> str:
    """Visible text of a document: tags stripped, entities decoded."""
    return html.unescape(re.sub(r"<[^>]+>", "", body))


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
    assert "Combos" in text
    assert 'id="gallery-board"' in text
    for heading in ("Queued", "Playing", "Loop found", "No loop", "Couldn't decide"):
        assert heading in text, heading
    assert "Kiki-Jiki, Mirror Breaker" in text
    assert "scryfall.com/search" in text
    # Shell: both destinations; The Goldfish is now a real link, no badge.
    assert "The Goldfish" in text
    assert 'href="/goldfish"' in text
    assert "coming soon" not in text
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
    assert "No loop" in text["refuted"]
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
    # Disagreement is surfaced as an annotation, not a sixth status chip.
    assert "results disagree" in refuted
    assert "chip-mixed" not in refuted
    # Both attempts link to their runs, and the link verb is the unified one.
    assert f"/run/{ids['loops']}" in refuted
    assert f"/run/{ids['mixed']}" in refuted
    assert "Open run →" in refuted
    assert "Open latest run" not in refuted
    # Headline verdict is the latest attempt.
    assert "No loop" in refuted


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
    assert "1\u201312 of 15" in queued["html"]
    assert "col-head-page" in queued["html"]
    # The pager is in the column header, above the tiles.
    assert queued["html"].index("col-head-page") < queued["html"].index("board-body")

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
    # The pager sits in the column header, so it is visible without scrolling.
    queued_start = text.index('data-col="queued"')
    header_at = text.index("col-head-page", queued_start)
    body_at = text.index('data-body="queued"', queued_start)
    assert header_at < body_at
    assert "13\u201315 of 15" in text[header_at:body_at]
    assert 'data-active="playing"' in text
    # Paging is a plain link, so it survives with JS disabled.
    assert "page=1" in text


def test_pager_header_and_count_formatter() -> None:
    from combo_discovery.web.gallery import render_column, render_tabs

    column = {
        "key": "queued",
        "label": "Queued",
        "empty": "Waiting.",
        "count": 468040,
        "showing": 12,
        "page": 2,
        "pages": 39004,
        "start": 13,
        "end": 24,
        "body_html": "",
        "prev_href": "/?page=1",
        "next_href": "/?page=3",
    }
    html_out = render_column(column)
    assert html_out.index("col-head-page") < html_out.index("board-body")
    assert "13\u201324 of 468,040" in html_out
    assert "2/39,004" in html_out
    assert 'href="/?page=1"' in html_out and 'href="/?page=3"' in html_out

    # A single-page column reads "showing all N", like its siblings.
    one_page = dict(column, count=4, page=1, pages=1, start=1, end=4,
                    showing=4, prev_href="", next_href="")
    single = render_column(one_page)
    assert "showing all 4" in single
    assert "col-page-nav" not in single

    tabs = render_tabs(
        [
            {"key": "queued", "label": "Queued", "count": 468040,
             "href": "/?tab=queued", "active": True},
            {"key": "loops", "label": "Loop found", "count": 4,
             "href": "/?tab=loops", "active": False},
        ]
    )
    assert ">468,040</span>" in tabs
    assert ">4</span>" in tabs


def test_link_verbs_are_unified(server) -> None:
    srv, _, _ = server
    body = _get(srv.server_address[1], "/")[2]
    assert "Open run →" in body       # any tile with a run
    assert "Open pairing →" in body   # queued tiles with no run
    assert "Open latest run" not in body
    assert "Watch →" not in body


def test_every_tile_has_the_why_row(server) -> None:
    srv, _, _ = server
    body = _get(srv.server_address[1], "/")[2]
    tiles = body.count('<article class="tile')
    assert tiles > 0
    assert body.count("<summary>Why these two?</summary>") == tiles


def test_mixed_note_is_not_a_status_chip(server) -> None:
    srv, _, _ = server
    refuted = next(
        c for c in _payload(srv.server_address[1])["columns"] if c["key"] == "refuted"
    )["html"]
    assert "tested 2 times" in refuted and "results disagree" in refuted
    assert "chip-mixed" not in refuted
    # The status chip remains the only chip on the tile.
    assert "chip chip-refuted" in refuted


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
    # Plain symmetrical label; the internal key stays "refuted".
    assert tabs["refuted"]["label"] == "No loop"
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
    untap_another = {"oracle_text": "When this creature enters, untap another target creature.",
                     "type_line": "Creature Bear"}
    mana = {"oracle_text": "{T}: Add one mana of any color.",
            "type_line": "Creature Elf Druid"}
    combat = {"oracle_text": "After this phase, there is an additional combat phase.",
              "type_line": "Enchantment Aura"}
    blank = {"oracle_text": "Flying.", "type_line": "Creature Bear"}
    assert card_role(kiki) == "copy engine"
    assert card_role(untapper) == "enters-the-battlefield untapper"
    assert card_role(untap_another) == "enters-the-battlefield untapper"
    assert card_role(mana) == "mana engine"
    assert card_role(combat) == "extra combats"
    assert card_role(blank) == ""


def test_roles_idea_keeps_duplicate_roles_and_names_the_other() -> None:
    from combo_discovery.web.gallery import _roles_idea

    untapper = {"role": "enters-the-battlefield untapper", "type_line": "Creature"}
    untapper2 = {"role": "enters-the-battlefield untapper", "type_line": "Creature"}
    # Two of the same role still read as the pair, not "another piece".
    assert _roles_idea([untapper, untapper2]) == (
        "enters-the-battlefield untapper + enters-the-battlefield untapper"
    )
    # One engine role: name the other card honestly by its type.
    engine = {"role": "copy engine", "type_line": "Legendary Creature"}
    vanilla = {"role": "", "type_line": "Creature Bear"}
    assert _roles_idea([engine, vanilla]) == "copy engine + creature"
    # No roles at all: no invented idea.
    assert _roles_idea([vanilla, dict(vanilla)]) == ""


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
# The Goldfish: watch a combo being tested
# ---------------------------------------------------------------------------


def test_goldfish_page_renders_the_run(server) -> None:
    """A finished run on the base seed still renders the whole screen."""
    srv, ids, _ = server
    status, content_type, body = _get(srv.server_address[1], f"/run/{ids['loops']}")
    assert status == 200
    assert "text/html" in content_type
    text = html.unescape(body)
    assert "Loop found." in text
    assert "The Goldfish" in text
    assert f'data-run="{ids["loops"]}"' in text
    assert "The board, rebuilt" in text
    assert "How the loop turns" in text
    assert "Game Log" in text
    assert 'id="goldfish"' in text
    # The Goldfish nav item is the active one and has no "coming soon" badge.
    assert "coming soon" not in text
    active = text[text.index('class="nav-item active"'):]
    assert "The Goldfish" in active[:240]


def test_goldfish_loop_screen_shows_loop_and_growth(gf_server) -> None:
    srv, ids, _ = gf_server
    port = srv.server_address[1]
    status, _, body = _get(port, f"/run/{ids['loop']}")
    assert status == 200
    text = _text(body)
    # 1. the play-by-play, in Magic words.
    assert "Kiki-Jiki, Mirror Breaker copies Deceiver Exarch" in text
    assert "Pass 3" in text
    # 3. the hero pass counter and the growth sentence.
    assert '<span class="pass-num">3</span>' in body
    assert "Same board as pass 2, and 1 more permanent and 1 more token." in text
    # counters are named values, never deltas like "Tokens +1".
    assert 'data-resource="tokens"' in body
    assert "Tokens +1" not in text
    # 4. the verdict.
    assert "The board came back to the same state while" in text
    assert "piled up" in text
    # 5. the loop graph: cards as nodes, actions on the edges, engine at hub.
    assert "copies Deceiver Exarch" in text
    assert "untaps Kiki-Jiki, Mirror Breaker" in text
    assert "Kiki-Jiki, Mirror Breaker is the engine" in text
    assert 'class="lg-node lg-hub"' in body
    # 6. ended: no live flag, summary shown.
    assert 'data-live="false"' in body
    assert "This goldfish run" in text


def test_goldfish_board_is_reconstructed(gf_server) -> None:
    srv, ids, _ = gf_server
    port = srv.server_address[1]
    payload = build_goldfish(srv.store, ids["loop"])
    assert payload is not None
    board = payload["board"]
    # Kiki + the original Exarch + three Exarch copy tokens, grouped by type.
    assert board["permanents"] == 5
    creatures = next(g for g in board["groups"] if g["key"] == "creature")
    stacks = {p["name"]: p for p in creatures["perm"]}
    kiki = stacks[GF_KIKI]
    assert kiki["count"] == 1 and kiki["token"] is False
    # Tapped is reconstructed from "becomes tapped"/"untaps": Kiki ends untapped.
    assert kiki["tapped"] is False
    exarch = [p for p in creatures["perm"] if p["name"] == GF_EXARCH]
    assert sum(p["count"] for p in exarch) == 4
    assert any(p["count"] == 1 and not p["copy"] for p in exarch)
    assert any(p["count"] == 3 and p["token"] and p["copy"] for p in exarch)
    html_body = _get(port, f"/run/{ids['loop']}")[2]
    assert "chip-token" in html_body and "chip-copy" in html_body
    assert "Not a board snapshot" in _text(html_body)


def test_reconstruct_board_reads_other_by_its_text() -> None:
    events = [
        {"seq": 1, "kind": "play_land", "text": "Mountain enters the battlefield",
         "actor": "Mountain", "detail": {"from": "from=Hand;to=Battlefield"}},
        {"seq": 2, "kind": "token", "text": "Goblin enters the battlefield",
         "actor": "Goblin", "detail": {"from": "from=null;to=Battlefield"}},
        {"seq": 3, "kind": "copy", "text": "Kiki copies Goblin", "actor": "Kiki",
         "target": "Goblin", "detail": {}},
        {"seq": 4, "kind": "other", "text": "Mountain becomes tapped",
         "actor": "Mountain", "detail": {"tapped": True}},
        {"seq": 5, "kind": "other", "text": "Goblin gets counters",
         "actor": "Goblin", "detail": {"old": 0, "new": 1}},
        {"seq": 6, "kind": "untap", "text": "Mountain untaps", "actor": "Mountain",
         "detail": {"tapped": False}},
        {"seq": 7, "kind": "other", "text": "Goblin leaves the battlefield",
         "actor": "Goblin", "detail": {}},
    ]
    board = reconstruct_board(events[:-1])
    permanents = {p.name: p for p in board["permanents"]}
    assert set(permanents) == {"Mountain", "Goblin"}
    assert permanents["Mountain"].tapped is False
    assert permanents["Goblin"].token is True
    assert permanents["Goblin"].counters == 1
    assert board["graveyard"] == {}

    # A departure removes it and is recorded in the graveyard, but the stream
    # does not say it went there — that assumption is documented on screen.
    departed = reconstruct_board(events)
    assert {p.name for p in departed["permanents"]} == {"Mountain"}
    assert departed["graveyard"] == {"Goblin": 1}


def test_build_loop_graph_reads_the_last_pass() -> None:
    def action(kind, actor, target="", detail=None):
        return {"kind": kind, "actor": actor, "target": target,
                "text": f"{actor} {kind}s", "detail": detail or {}, "seq": 0}

    pass_events = [
        action("other", GF_KIKI), action("activate", GF_KIKI, GF_EXARCH),
        action("token", GF_EXARCH), action("copy", GF_KIKI, GF_EXARCH),
        action("trigger", GF_EXARCH, GF_KIKI), action("untap", GF_KIKI),
    ]
    events = [
        *pass_events, {"kind": "iteration", "detail": {"iteration": 1}, "seq": 7},
        *pass_events, {"kind": "iteration", "detail": {"iteration": 2}, "seq": 14},
    ]
    graph = build_loop_graph(events, {"resources": {"tokens": 9}})
    assert graph["hub"] == GF_KIKI
    assert graph["growth_node"] == GF_EXARCH
    labels = [edge["label"] for edge in graph["edges"]]
    assert labels == [
        "copies Deceiver Exarch",
        "triggers, targeting Kiki-Jiki, Mirror Breaker",
        "untaps Kiki-Jiki, Mirror Breaker",
    ]
    # Never the internal vocabulary.
    for edge in graph["edges"]:
        for banned in ("signature", "Step ", "Tokens +", "back to the same board"):
            assert banned not in edge["label"]


def test_goldfish_live_run_says_live_and_polls(gf_server) -> None:
    srv, ids, _ = gf_server
    port = srv.server_address[1]
    status, _, body = _get(port, f"/run/{ids['live']}")
    assert status == 200
    text = html.unescape(body)
    assert 'data-live="true"' in text
    assert "Still testing." in text
    assert "Splinter Twin" in text
    # The poll cursors are seeded so the first fetch is incremental.
    assert 'data-seq="' in text and 'data-obs="' in text
    assert "/events?after=" in body  # the JS poll target
    assert "/board?events_after=" in body


def test_run_events_endpoint_is_cursor_incremental(gf_server) -> None:
    srv, ids, _ = gf_server
    port = srv.server_address[1]
    run = ids["loop"]
    whole = _payload(port, f"/api/run/{run}/events?after=0")
    assert whole["after"] == 0
    assert whole["count"] == whole["total"] == 24
    assert whole["cursor"] == whole["events"][-1]["seq"]
    assert "copies" in whole["rows_html"]
    assert "Deceiver Exarch" in whole["rows_html"]

    # Past the cursor: nothing new.
    empty = _payload(port, f"/api/run/{run}/events?after={whole['cursor']}")
    assert empty["count"] == 0
    assert empty["cursor"] == whole["cursor"]
    assert empty["rows_html"] == ""

    # A mid cursor returns only the rows after it.
    mid = _payload(port, f"/api/run/{run}/events?after=5")
    assert mid["count"] == 24 - 5
    assert mid["events"][0]["seq"] == 6
    assert mid["cursor"] == 24


def test_run_observations_endpoint_is_cursor_incremental(gf_server) -> None:
    srv, ids, _ = gf_server
    port = srv.server_address[1]
    run = ids["loop"]
    whole = _payload(port, f"/api/run/{run}/observations?after=0")
    assert whole["count"] == 4
    assert whole["cursor"] == whole["observations"][-1]["id"]
    assert whole["observations"][-1]["resources"]["tokens"] == 3
    empty = _payload(port, f"/api/run/{run}/observations?after={whole['cursor']}")
    assert empty["count"] == 0


def test_run_board_endpoint_returns_fragments(gf_server) -> None:
    srv, ids, _ = gf_server
    port = srv.server_address[1]
    payload = _payload(port, f"/api/run/{ids['loop']}/board")
    assert payload["live"] is False
    assert payload["seq"] == 24
    for key in ("board", "graph", "loop", "counters", "verdict", "status", "summary"):
        assert payload["html"][key]
        assert key in payload["sig"]

    # The cursors make the board call incremental too: past them, nothing new.
    tail = _payload(port, f"/api/run/{ids['loop']}/board?events_after=24&obs_after=9999")
    assert tail["new_events"] == 0 and tail["new_observations"] == 0
    fresh = _payload(port, f"/api/run/{ids['loop']}/board?events_after=0&obs_after=0")
    assert fresh["new_events"] == 24 and fresh["new_observations"] == 4


def test_run_events_endpoint_404s_for_unknown_run(gf_server) -> None:
    srv, _, _ = gf_server
    with pytest.raises(urllib.error.HTTPError) as error:
        _get(srv.server_address[1], "/api/run/999999/events")
    assert error.value.code == 404


def test_runs_api_includes_in_progress_with_names(gf_server) -> None:
    srv, ids, _ = gf_server
    payload = _payload(srv.server_address[1], "/api/runs")
    by_id = {run["id"]: run for run in payload["runs"]}
    live = by_id[ids["live"]]
    assert live["live"] is True
    assert live["verdict"] is None
    assert live["cards"] == ["Splinter Twin", GF_EXARCH]
    finished = by_id[ids["loop"]]
    assert finished["live"] is False
    assert finished["verdict"] == "loops"
    assert finished["cards"] == [GF_KIKI, GF_EXARCH]


def test_goldfish_index_lists_live_games(gf_server) -> None:
    srv, ids, _ = gf_server
    status, _, body = _get(srv.server_address[1], "/goldfish")
    assert status == 200
    text = html.unescape(body)
    assert "Happening now" in text
    assert "live now" in text
    assert f"/run/{ids['live']}" in text
    assert "Recently finished" in text
    assert f"/run/{ids['loop']}" in text


def test_goldfish_css_honours_reduced_motion() -> None:
    from combo_discovery.web.assets import CSS

    assert "@media (prefers-reduced-motion: reduce)" in CSS
    # The loop graph's animation is a named keyframe the media query can stop.
    assert "@keyframes flow" in CSS
    assert "@keyframes halo" in CSS
    assert "flex-wrap: nowrap" in CSS  # the pager never wraps


def test_graph_numbers_each_edge_and_closes_the_loop(gf_server) -> None:
    srv, ids, _ = gf_server
    payload = build_goldfish(srv.store, ids["loop"])
    assert payload is not None
    graph = payload["graph"]
    assert [step["number"] for step in graph["steps"]] == [1, 2, 3]
    assert graph["chain"].startswith("1 copies Deceiver Exarch")

    body = _get(srv.server_address[1], f"/run/{ids['loop']}")[2]
    text = _text(body)
    # Each action is labelled on its own arc, numbered in order, and the
    # caption matches that order; the cycle is explicitly closed.
    assert 'class="lg-num"' in body
    assert "1 copies Deceiver Exarch" in text
    assert "2 triggers, targeting Kiki-Jiki, Mirror Breaker" in text
    assert "3 untaps Kiki-Jiki, Mirror Breaker" in text
    assert "then it repeats" in text
    # The narrow-width form is a numbered cycle that returns to step 1.
    assert 'class="looplist"' in body
    assert "back to step 1" in text
    assert 'class="looplist-wrap"' in body


def test_graph_mobile_fallback_is_swapped_by_css() -> None:
    from combo_discovery.web.assets import CSS

    assert ".looplist-wrap { display: none; }" in CSS
    assert "@media (max-width: 620px)" in CSS
    narrow = CSS.split("@media (max-width: 620px)", 1)[1][:400]
    assert ".loopgraph-wrap { display: none; }" in narrow
    assert ".looplist-wrap { display: block; }" in narrow


def test_counters_are_board_first_and_label_the_engine_tallies() -> None:
    growth = {
        "resources": {
            "tokens": 38, "permanents": 40, "casts": 76,
            "spells_resolved": 76, "mana": 48, "life": 40,
        }
    }
    html_out = render_counters(growth)
    board = html_out.split('<div class="counters counters-engine">', 1)[0]
    # Tokens and permanents are the board-tracked growth story, shown plainly.
    assert 'data-resource="tokens"' in board
    assert 'data-resource="permanents"' in board
    assert 'data-resource="casts"' not in board
    assert 'data-resource="mana"' not in board
    # The engine's own tallies are labelled as such and hidden behind a
    # disclosure, never presented as read off the board.
    assert '<details class="counter-raw">' in html_out
    assert "Also reported by the Rules Engine — not read off the board" in html_out
    engine = html_out.split('<div class="counters counters-engine">', 1)[1]
    assert 'data-resource="casts"' in engine
    assert 'data-resource="mana"' in engine


def test_board_shows_tapped_unmistakably() -> None:
    from combo_discovery.web.goldfish import _permanent_html as permanent_html

    tap = {
        "name": "Kiki-Jiki, Mirror Breaker", "count": 1, "tapped": True,
        "token": False, "copy": False, "counters": 0, "type_line": "Creature",
    }
    html_out = permanent_html(tap)
    assert "is-tapped" in html_out
    assert "chip-tapped" in html_out and "tapped" in html_out
    assert "is-tapped" not in permanent_html({**tap, "tapped": False})


def test_tap_styling_is_visible() -> None:
    from combo_discovery.web.assets import CSS

    assert ".perm.is-tapped::after" in CSS
    assert 'content: "TAPPED"' in CSS
    assert "filter: grayscale(1)" in CSS


def test_play_by_play_marks_and_shades_passes() -> None:
    events = [
        {"kind": "iteration", "detail": {"iteration": 1}, "seq": 1},
        {"kind": "copy", "text": "Kiki copies Exarch", "seq": 2,
         "turn": 1, "phase": "MAIN1"},
        {"kind": "iteration", "detail": {"iteration": 2}, "seq": 3},
        {"kind": "copy", "text": "Kiki copies Exarch", "seq": 4,
         "turn": 1, "phase": "MAIN1"},
    ]
    html_out = render_play_by_play(events, ["Kiki", "Exarch"])
    assert html_out.count('class="pb-pass"') == 2
    assert "Pass 1" in html_out and "Pass 2" in html_out
    assert "pb-even" in html_out  # alternate passes are shaded


def test_goldfish_log_opens_on_the_newest_move(gf_server) -> None:
    srv, ids, _ = gf_server
    status, _, body = _get(srv.server_address[1], f"/run/{ids['loop']}")
    assert status == 200
    from combo_discovery.web.assets import JS

    assert 'id="pb-jump"' in body
    assert "scrollTop = gfList.scrollHeight" in JS  # opens at the tail
    assert "pb-jump" in JS and "updateJump" in JS   # way back to the live tail


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
    assert 'id="goldfish"' in body


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
