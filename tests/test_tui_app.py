"""Textual smoke/mount + run-wiring tests.

These run the real app headlessly through `App.run_test`; no harness is
required because worker probing and pool construction are stubbed.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from textual.widgets import (
    Button,
    ContentSwitcher,
    DataTable,
    Input,
    OptionList,
    Select,
    Static,
    Tabs,
)

from combo_discovery.generated import forge_env_pb2 as pb
from combo_discovery.runner import GameResult
from combo_discovery.tui.app import ComboDiscoveryApp
from combo_discovery.tui.data import (
    ActiveRun,
    WorkerProbe,
    build_run_config,
    read_scratch_deck,
    scratch_deck_path,
)
from combo_discovery.tui.views import experiments as exp
from combo_discovery.tui.widgets import EmptyState

pytestmark = pytest.mark.asyncio


def _fake_probe(host, base_port, n, *, timeout=1.0):
    return WorkerProbe(
        host=host,
        base_port=base_port,
        results=[(base_port + i, True, "reachable") for i in range(n)],
    )


def _dead_probe(host, base_port, n, *, timeout=1.0):
    return WorkerProbe(
        host=host,
        base_port=base_port,
        results=[(base_port + i, False, "unreachable") for i in range(n)],
    )


def _decks_dir(tmp_path: Path) -> Path:
    decks = tmp_path / "decks"
    decks.mkdir(exist_ok=True)
    (decks / "goldfish_A.dck").write_text("// deck a\n")
    (decks / "goldfish_B.dck").write_text("// deck b\n")
    return decks


def _make_app(tmp_path: Path) -> tuple[ComboDiscoveryApp, Path, Path]:
    db = tmp_path / "experiments.sqlite"
    decks = _decks_dir(tmp_path)
    app = ComboDiscoveryApp(db_path=db, decks_dir=decks, config_path=tmp_path / "absent.toml")
    return app, db, decks


async def test_mounts_and_renders(tmp_path, monkeypatch):
    monkeypatch.setattr(exp, "probe_workers", _fake_probe)
    app, _db, _decks = _make_app(tmp_path)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.pause()

        assert app.theme == "omarchy"
        assert app.query_one(Tabs).active == "view-corpus"
        assert app.query_one(ContentSwitcher).current == "view-corpus"
        for view_id in ("view-corpus", "view-experiments", "view-candidates", "view-activity"):
            assert app.query_one(f"#{view_id}") is not None

        # Empty store renders the corpus/candidates empty states, not an error.
        assert app.query_one("#corpus-empty", EmptyState).display is True
        assert app.query_one("#cand-empty", EmptyState).display is True
        for view_id in (
            "view-corpus", "view-experiments", "view-candidates",
            "view-cardlab", "view-activity",
        ):
            assert app.query_one(f"#{view_id}") is not None

        # Render check: the SVG export contains the app chrome.
        svg = app.export_screenshot()
        assert "combo-discovery" in svg
        assert len(svg) > 1000


async def test_keyboard_navigation_and_help(tmp_path, monkeypatch):
    monkeypatch.setattr(exp, "probe_workers", _fake_probe)
    app, _db, _decks = _make_app(tmp_path)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.pause()

        await pilot.press("2")
        await pilot.pause()
        assert app.query_one(ContentSwitcher).current == "view-experiments"

        await pilot.press("right_square_bracket")
        await pilot.pause()
        assert app.query_one(ContentSwitcher).current == "view-candidates"

        await pilot.press("4")
        await pilot.pause()
        assert app.query_one(ContentSwitcher).current == "view-cardlab"

        await pilot.press("5")
        await pilot.pause()
        assert app.query_one(ContentSwitcher).current == "view-activity"

        await pilot.press("1")
        await pilot.pause()
        assert app.query_one(ContentSwitcher).current == "view-corpus"

        await pilot.press("question_mark")
        await pilot.pause()
        assert len(app.screen_stack) == 2
        await pilot.press("escape")
        await pilot.pause()
        assert len(app.screen_stack) == 1


async def test_corpus_lists_seeded_cards(tmp_path, monkeypatch):
    monkeypatch.setattr(exp, "probe_workers", _fake_probe)
    app, db, _decks = _make_app(tmp_path)
    # Create the importer-owned cards table after the app ensured the schema.
    from combo_discovery.store import ExperimentStore

    ExperimentStore(db).close()
    conn = sqlite3.connect(db)
    # Schema v2 already created a real cards table; swap in the loose legacy
    # shape the corpus view is designed to tolerate.
    conn.execute("DROP TABLE IF EXISTS cards")
    conn.execute(
        "CREATE TABLE cards (id INTEGER PRIMARY KEY, name TEXT, type_line TEXT,"
        " mana_cost TEXT, oracle_text TEXT, effects_json TEXT)"
    )
    conn.execute(
        "INSERT INTO cards (name, type_line, mana_cost, oracle_text, effects_json)"
        " VALUES (?,?,?,?,?)",
        ("Brainstorm", "Instant", "{U}", "Draw three.", json.dumps([{}, {}])),
    )
    conn.commit()
    conn.close()

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.pause()
        listing = app.query_one("#corpus-list", OptionList)
        assert listing.display is True
        assert listing.option_count == 1
        assert app.query_one("#corpus-empty", EmptyState).display is False

        await pilot.press("j")
        await pilot.pause()


async def test_no_harness_note_is_unclipped_and_warn(tmp_path, monkeypatch):
    monkeypatch.setattr(exp, "probe_workers", _dead_probe)
    app, _db, _decks = _make_app(tmp_path)
    async with app.run_test(size=(112, 34)) as pilot:
        await pilot.pause()
        await pilot.pause()
        await pilot.press("2")
        await pilot.pause()

        note = app.query_one("#exp-form-note", Static)
        wanted = "no live harness — connection guidance below"
        assert wanted in str(note.render())
        # The note owns a full-width row below the buttons, wide enough that the
        # whole line fits without being clipped by the panel border.
        assert note.region.width >= len(wanted)
        assert note.region.y > app.query_one("#exp-start", Button).region.y

        # Render check: the word survives to the rendered SVG (not ellipsised).
        svg = app.export_screenshot()
        assert "guidance" in svg
        assert app.query_one("#exp-guidance").display is True

        # A dead harness while idle is a warning, not an error...
        assert app.worker_status() == ("0/8 workers reachable", "warn")
        # ...but zero live workers during an active run is an error.
        config, _errors = build_run_config(
            ("A", "/a.dck"), ("B", "/b.dck"), "default", 1, 1, 8, 50060
        )
        assert config is not None
        app.active_run = ActiveRun(run_id="x", config=config, started_at=0.0)
        assert app.worker_status()[1] == "error"


async def test_long_form_note_wraps_not_clipped(tmp_path, monkeypatch):
    monkeypatch.setattr(exp, "probe_workers", _fake_probe)
    app, _db, _decks = _make_app(tmp_path)
    async with app.run_test(size=(112, 34)) as pilot:
        await pilot.pause()
        await pilot.pause()
        await pilot.press("2")
        await pilot.pause()

        view = app.query_one("#view-experiments")
        long_note = (
            "pick a deck for seat A  ·  pick a deck for seat B  ·  "
            "seed count must be >= 1  ·  workers must be >= 1"
        )
        view._set_form_note(long_note, "err")
        await pilot.pause()
        await pilot.pause()

        note = app.query_one("#exp-form-note", Static)
        assert str(note.render()) == long_note
        # It cannot fit on one row at this width, so it must have wrapped
        # (grown) rather than being clipped.
        assert note.region.width < len(long_note)
        assert note.region.height >= 2


class _FakePool:
    """Stands in for WorkerPool: records what the view passes, then stops."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.closed = False

    def map_games(self, deck_pair, seeds, policy=None, max_turns=0, timeout_seconds=0,
                  store=None, run_id=None):
        self.calls.append(
            {
                "deck_pair": deck_pair,
                "seeds": list(seeds),
                "policy": policy,
                "max_turns": max_turns,
                "store": store,
                "run_id": run_id,
            }
        )
        result = GameResult(
            game_id=1, seed=seeds[0], winner=0, turns=3, n_events=1,
            duration_s=0.1, outcome=pb.OUTCOME_WIN, reason="lethal",
        )
        store.record_game(
            run_id, result, list(deck_pair), seeds[0],
            [pb.PLAYER_TYPE_REMOTE, pb.PLAYER_TYPE_REMOTE], max_turns,
        )
        return [result]

    def close(self) -> None:
        self.closed = True


async def test_start_run_records_through_store(tmp_path, monkeypatch):
    monkeypatch.setattr(exp, "probe_workers", _fake_probe)
    fake_pool = _FakePool()
    monkeypatch.setattr(exp, "build_pool", lambda probe, **kw: fake_pool)

    app, _db, _decks = _make_app(tmp_path)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.pause()
        # The on-mount harness check should have populated a reachable probe.
        assert app.probe is not None and app.probe.ok_count > 0

        view = app.query_one("#view-experiments")
        view.action_start_run()

        for _ in range(100):
            if app.active_run is not None and app.active_run.finished:
                break
            await pilot.pause(0.05)

        assert app.active_run is not None
        assert app.active_run.error is None
        assert app.active_run.finished
        assert fake_pool.closed

        # map_games was driven with the real store + run id from start_experiment.
        assert fake_pool.calls, "map_games was not called"
        call = fake_pool.calls[0]
        assert call["store"] is app.store
        assert call["run_id"] == app.active_run.run_id
        assert call["seeds"] == app.active_run.config.seeds

        # The store binding sees the recorded experiment + game.
        assert len(app.data.list_experiments()) == 1
        assert len(app.data.games_for_run(app.active_run.run_id)) == 1
        assert app.query_one("#exp-results", DataTable).row_count == 1


# ---------------------------------------------------------------------------
# card loop: corpus -> interactions / deck -> experiments / candidates
# ---------------------------------------------------------------------------


def _app_for_db(tmp_path: Path, db: Path):
    decks = _decks_dir(tmp_path)
    app = ComboDiscoveryApp(db_path=db, decks_dir=decks, config_path=tmp_path / "absent.toml")
    return app, decks


def _wait_option(pilot, listing, timeout: float = 6.0):
    """Pause until an OptionList has options (worker threads populate async)."""

    async def _inner():
        waited = 0.0
        while waited < timeout:
            if listing.option_count:
                return
            await pilot.pause(0.05)
            waited += 0.05

    return _inner()


async def test_corpus_interactions_modal_populates(ontology_db, tmp_path, monkeypatch):
    monkeypatch.setattr(exp, "probe_workers", _fake_probe)
    app, _decks = _app_for_db(tmp_path, ontology_db)
    async with app.run_test(size=(150, 44)) as pilot:
        await pilot.pause()
        await pilot.pause()

        app.open_card(1, "Kiki-Jiki, Mirror Breaker")
        await pilot.pause()
        await pilot.pause()
        await pilot.press("i")
        await pilot.pause()

        screen = app.screen_stack[-1]
        listing = screen.query_one("#interactions-list", OptionList)
        await _wait_option(pilot, listing)
        # Only hypothesis #1 (Kiki + Pestermite) is non-refuted and involves Kiki.
        assert listing.option_count == 1
        labels = str(listing.get_option_at_index(0).prompt)
        assert "Pestermite" in labels
        assert "infinite_etb_loop" in labels


async def test_add_to_deck_increments_and_is_offered(ontology_db, tmp_path, monkeypatch):
    monkeypatch.setattr(exp, "probe_workers", _fake_probe)
    app, decks = _app_for_db(tmp_path, ontology_db)
    async with app.run_test(size=(150, 44)) as pilot:
        await pilot.pause()
        await pilot.pause()

        app.open_card(1, "Kiki-Jiki, Mirror Breaker")
        await pilot.pause()
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()

        path = scratch_deck_path(decks)
        assert path.exists()
        _name, entries = read_scratch_deck(path)
        assert entries == [(2, "Kiki-Jiki, Mirror Breaker")]
        assert "[Main]" in path.read_text(encoding="utf-8")

        # The scratch deck is offered by the Experiments deck selects.
        await pilot.press("2")
        await pilot.pause()
        select = app.query_one("#exp-deck-a", Select)
        scratch = str(path)
        select.value = scratch  # raises if the option is not present
        assert select.value == scratch


async def test_candidates_list_and_verdict_cycle(ontology_db, tmp_path, monkeypatch):
    monkeypatch.setattr(exp, "probe_workers", _fake_probe)
    app, _decks = _app_for_db(tmp_path, ontology_db)
    async with app.run_test(size=(150, 44)) as pilot:
        await pilot.pause()
        await pilot.pause()
        await pilot.press("3")
        await pilot.pause()

        view = app.query_one("#view-candidates")
        listing = view.query_one("#cand-list", OptionList)
        await _wait_option(pilot, listing)
        assert listing.option_count == 4
        assert int(listing.get_option_at_index(0).id) == 1  # highest score

        await pilot.press("v")
        await pilot.pause()
        meta = ""
        for _ in range(60):
            await pilot.pause(0.05)
            meta = str(view.query_one("#cand-meta", Static).render())
            if "verified" in meta:
                break

        verdicts = app.data.adjudications(1)
        assert verdicts and verdicts[-1]["verdict"] == "verified"
        assert verdicts[-1]["reviewer"] == "tui"
        assert "verified" in meta


REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_DB = REPO_ROOT / "research.db"
REAL_DECKS = REPO_ROOT / "decks"


@pytest.mark.skipif(not REAL_DB.exists(), reason="live research.db not present")
async def test_live_research_db_read_only(monkeypatch):
    """Populate Candidates and Kiki interactions from the real DB (no writes)."""
    monkeypatch.setattr(exp, "probe_workers", _fake_probe)
    app = ComboDiscoveryApp(
        db_path=REAL_DB, decks_dir=REAL_DECKS, config_path=REPO_ROOT / "research.toml"
    )
    async with app.run_test(size=(150, 44)) as pilot:
        await pilot.pause()
        await pilot.pause()

        await pilot.press("3")
        await pilot.pause()
        view = app.query_one("#view-candidates")
        listing = view.query_one("#cand-list", OptionList)
        await _wait_option(pilot, listing, timeout=15.0)
        assert listing.option_count > 0

        matches = app.data.list_cards("Kiki-Jiki", limit=5)
        kiki = next(c for c in matches if c["name"] == "Kiki-Jiki, Mirror Breaker")
        app.open_card(int(kiki["id"]), kiki["name"])
        await pilot.pause()
        await pilot.pause()
        await pilot.press("i")
        await pilot.pause()

        screen = app.screen_stack[-1]
        interactions = screen.query_one("#interactions-list", OptionList)
        await _wait_option(pilot, interactions, timeout=15.0)
        assert interactions.option_count > 0


async def test_interactions_row_jumps_to_partner(ontology_db, tmp_path, monkeypatch):
    """Enter on an interaction closes the modal and opens the partner card."""
    monkeypatch.setattr(exp, "probe_workers", _fake_probe)
    app, _decks = _app_for_db(tmp_path, ontology_db)
    async with app.run_test(size=(150, 44)) as pilot:
        await pilot.pause()
        await pilot.pause()

        app.open_card(1, "Kiki-Jiki, Mirror Breaker")
        await pilot.pause()
        await pilot.pause()
        await pilot.press("i")
        await pilot.pause()
        screen = app.screen_stack[-1]
        listing = screen.query_one("#interactions-list", OptionList)
        await _wait_option(pilot, listing)
        assert listing.option_count == 1

        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()

        assert app.screen_stack[-1] is not screen  # modal dismissed
        assert "Pestermite" in app.query_one("#corpus-search", Input).value
        corpus_list = app.query_one("#corpus-list", OptionList)
        assert corpus_list.highlighted is not None
        assert int(corpus_list.get_option_at_index(corpus_list.highlighted).id) == 2


# ---------------------------------------------------------------------------
# Card Lab: ground truth, proposals, missed, metrics, diagnostics, persistence
# ---------------------------------------------------------------------------


async def _wait_text(pilot, static: Static, needle: str, timeout: float = 25.0) -> bool:
    waited = 0.0
    while waited < timeout:
        if needle in str(static.render()):
            return True
        await pilot.pause(0.1)
        waited += 0.1
    return needle in str(static.render())


def _labels(listing: OptionList) -> list[str]:
    return [str(listing.get_option_at_index(i).prompt) for i in range(listing.option_count)]


async def test_card_lab_populates_from_ground_truth(lab_db, tmp_path, monkeypatch):
    monkeypatch.setattr(exp, "probe_workers", _fake_probe)
    app, _decks = _app_for_db(tmp_path, lab_db)
    async with app.run_test(size=(160, 48)) as pilot:
        await pilot.pause()
        await pilot.pause()
        await pilot.press("4")
        await pilot.pause()

        view = app.query_one("#view-cardlab")
        known = view.query_one("#lab-known-list", OptionList)
        await _wait_option(pilot, known)
        assert known.option_count == 3
        assert "showing 3 of 3" in str(view.query_one("#lab-known-count", Static).render())

        proposals = view.query_one("#lab-proposed-list", OptionList)
        labels = " | ".join(_labels(proposals)).lower()
        assert proposals.option_count == 3
        assert "known" in labels and "candidate" in labels and "contained" in labels
        assert "novel" not in labels

        metrics = view.query_one("#lab-metrics-text", Static)
        assert await _wait_text(pilot, metrics, "by pattern")
        metrics_text = str(metrics.render())
        assert "this card" in metrics_text and "by pattern" in metrics_text
        # Kiki-scoped: 1 known / 1 contained / 1 candidate / 1 missed -> P 0.333
        assert "known 1" in metrics_text and "candidate 1" in metrics_text

        diagnostics = view.query_one("#lab-diagnostics-text", Static)
        diag_text = str(diagnostics.render())
        assert "false positives" in diag_text and "misses" in diag_text
        assert "TAPS_COST" in diag_text  # FP cluster from proposal #2 evidence

        missed = view.query_one("#lab-missed-list", OptionList)
        await _wait_option(pilot, missed)
        assert missed.option_count == 1
        assert "Fear of Missing Out" in _labels(missed)[0]


async def test_card_lab_filter_toggle(lab_db, tmp_path, monkeypatch):
    monkeypatch.setattr(exp, "probe_workers", _fake_probe)
    app, _decks = _app_for_db(tmp_path, lab_db)
    async with app.run_test(size=(160, 48)) as pilot:
        await pilot.pause()
        await pilot.pause()
        await pilot.press("4")
        await pilot.pause()
        view = app.query_one("#view-cardlab")
        known = view.query_one("#lab-known-list", OptionList)
        await _wait_option(pilot, known)
        assert "showing 3 of 3" in str(view.query_one("#lab-known-count", Static).render())

        await pilot.press("f")
        await pilot.pause()
        assert known.option_count == 2
        assert "showing 2 of 2" in str(view.query_one("#lab-known-count", Static).render())
        assert "exact 2-card only" in str(view.query_one("#lab-known-filter", Static).render())


async def test_card_lab_evaluate_persists(lab_db, tmp_path, monkeypatch):
    monkeypatch.setattr(exp, "probe_workers", _fake_probe)
    app, _decks = _app_for_db(tmp_path, lab_db)
    async with app.run_test(size=(160, 48)) as pilot:
        await pilot.pause()
        await pilot.pause()
        await pilot.press("4")
        await pilot.pause()
        view = app.query_one("#view-cardlab")
        await _wait_option(pilot, view.query_one("#lab-known-list", OptionList))
        metrics = view.query_one("#lab-metrics-text", Static)
        await _wait_text(pilot, metrics, "by pattern")

        def count(table: str) -> int:
            with sqlite3.connect(lab_db) as conn:
                return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

        before = {t: count(t) for t in ("interactions", "combo_hypotheses", "known_combos")}
        assert count("evaluation_runs") == 0

        await pilot.press("r")
        for _ in range(100):
            if count("evaluation_runs") >= 1:
                break
            await pilot.pause(0.1)

        assert count("evaluation_runs") == 1
        assert count("evaluation_results") > 0
        with sqlite3.connect(lab_db) as conn:
            row = conn.execute(
                "SELECT card_filter, known_import_id, ontology_import_id"
                " FROM evaluation_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        assert row[0] == "Kiki-Jiki, Mirror Breaker"
        assert row[1] == "sb1" and row[2] == "imp1"
        # Reads never mutate the source tables.
        for table, value in before.items():
            assert count(table) == value


async def test_candidates_show_ground_truth_badges(lab_db, tmp_path, monkeypatch):
    monkeypatch.setattr(exp, "probe_workers", _fake_probe)
    app, _decks = _app_for_db(tmp_path, lab_db)
    async with app.run_test(size=(150, 44)) as pilot:
        await pilot.pause()
        await pilot.pause()
        await pilot.press("3")
        await pilot.pause()
        view = app.query_one("#view-candidates")
        listing = view.query_one("#cand-list", OptionList)
        await _wait_option(pilot, listing)
        labels = " | ".join(_labels(listing)).lower()
        assert "known" in labels
        assert "candidate" in labels
        assert "contained" in labels
        assert "novel" not in labels


REAL_DB_CARD_LAB = REPO_ROOT / "research.db"
REAL_DECKS_CARD_LAB = REPO_ROOT / "decks"


@pytest.mark.skipif(not REAL_DB_CARD_LAB.exists(), reason="live research.db not present")
async def test_live_card_lab_read_only(monkeypatch):
    monkeypatch.setattr(exp, "probe_workers", _fake_probe)
    app = ComboDiscoveryApp(
        db_path=REAL_DB_CARD_LAB, decks_dir=REAL_DECKS_CARD_LAB,
        config_path=REPO_ROOT / "research.toml",
    )
    with sqlite3.connect(REAL_DB_CARD_LAB) as conn:
        runs_before = conn.execute("SELECT COUNT(*) FROM evaluation_runs").fetchone()[0]

    async with app.run_test(size=(160, 48)) as pilot:
        await pilot.pause()
        await pilot.pause()
        await pilot.press("4")
        await pilot.pause()

        view = app.query_one("#view-cardlab")
        known = view.query_one("#lab-known-list", OptionList)
        await _wait_option(pilot, known, timeout=25.0)
        assert "showing 200 of 791" in str(view.query_one("#lab-known-count", Static).render())

        proposals = view.query_one("#lab-proposed-list", OptionList)
        assert proposals.option_count == 14
        labels = " | ".join(_labels(proposals)).lower()
        assert "known" in labels and "candidate" in labels and "novel" not in labels

        missed = view.query_one("#lab-missed-list", OptionList)
        await _wait_option(pilot, missed, timeout=30.0)
        assert missed.option_count == 66
        assert any("fear of missing out" in label.lower() for label in _labels(missed))

        metrics = view.query_one("#lab-metrics-text", Static)
        assert await _wait_text(pilot, metrics, "by pattern", timeout=30.0)
        # Kiki: precision 0.857 (12 known / 14 proposed), recall 0.154, F1 0.261
        assert "P 0.857" in str(metrics.render())
        diagnostics = view.query_one("#lab-diagnostics-text", Static)
        assert "misses" in str(diagnostics.render())

    with sqlite3.connect(REAL_DB_CARD_LAB) as conn:
        runs_after = conn.execute("SELECT COUNT(*) FROM evaluation_runs").fetchone()[0]
    assert runs_after == runs_before  # mount never persists
