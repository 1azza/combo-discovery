"""Textual smoke/mount + run-wiring tests.

These run the real app headlessly through `App.run_test`; no harness is
required because worker probing and pool construction are stubbed.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from textual.widgets import ContentSwitcher, DataTable, OptionList, Tabs

from combo_discovery.generated import forge_env_pb2 as pb
from combo_discovery.runner import GameResult
from combo_discovery.tui.app import ComboDiscoveryApp
from combo_discovery.tui.data import WorkerProbe
from combo_discovery.tui.views import experiments as exp
from combo_discovery.tui.widgets import EmptyState

pytestmark = pytest.mark.asyncio


def _fake_probe(host, base_port, n, *, timeout=1.0):
    return WorkerProbe(
        host=host,
        base_port=base_port,
        results=[(base_port + i, True, "reachable") for i in range(n)],
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
