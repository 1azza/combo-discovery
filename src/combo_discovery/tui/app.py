"""Combo-discovery research console.

A keyboard-first Textual TUI that drives the existing library: it browses the
SQLite card/experiment store, launches game batches through
:class:`~combo_discovery.pool.WorkerPool`, and tails recorded events and worker
health. Long-running work runs in Textual workers (threads), never on the UI
thread.
"""

from __future__ import annotations

import argparse
import logging
import threading
import time
from collections import deque
from contextlib import suppress
from pathlib import Path
from typing import Any

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.widgets import ContentSwitcher, Tabs
from textual.widgets._tabs import Tab

from . import theme as pal
from .data import ActiveRun, StoreBinding, fmt_duration, pool_snapshot, short_id
from ..research_config import DEFAULT_CONFIG_PATH, load_config
from .screens import HelpScreen
from ..store import ExperimentStore
from .views import ActivityView, CandidatesView, CorpusView, ExperimentsView
from .widgets import HeaderBar, StatusLine

# (view id, number key, tab label)
VIEWS: tuple[tuple[str, str, str], ...] = (
    ("view-corpus", "1", "Corpus"),
    ("view-experiments", "2", "Experiments"),
    ("view-candidates", "3", "Candidates"),
    ("view-activity", "4", "Activity"),
)

# Textual's default system command palette binding; shown in the statusline.
_PALETTE_KEY = "ctrl+p"

_LOGGER_NAME = "combo_discovery"


class LogBridge(logging.Handler):
    """Collect library log records for the Activity feed without blocking."""

    def __init__(self, maxlen: int = 500) -> None:
        super().__init__()
        self._records: deque[tuple[int, str]] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - logging must never raise
            message = str(record.msg)
        if record.exc_info:
            exc_type = record.exc_info[0]
            label = exc_type.__name__ if exc_type is not None else "Exception"
            message = f"{message} ({label})"
        with self._lock:
            self._records.append((record.levelno, f"{record.name}: {message}"))

    def drain(self) -> list[tuple[int, str]]:
        with self._lock:
            items = list(self._records)
            self._records.clear()
        return items


def default_decks_dir() -> Path:
    """Prefer ``./decks`` from the working directory, else the repo's ``decks``."""
    cwd_decks = Path.cwd() / "decks"
    if cwd_decks.is_dir():
        return cwd_decks
    # src/combo_discovery/tui/app.py -> repository root
    return Path(__file__).resolve().parents[3] / "decks"


class ComboDiscoveryApp(App[None]):
    """The research console application."""

    CSS_PATH = "app.tcss"
    TITLE = "combo-discovery"
    SUB_TITLE = "research console"
    ENABLE_COMMAND_PALETTE = True
    HORIZONTAL_BREAKPOINTS = [(0, "-narrow"), (112, "-wide")]

    BINDINGS = [
        Binding("q", "quit_app", "quit"),
        Binding("question_mark", "help", "keys"),
        Binding("f1", "help", "keys", show=False),
        Binding("1", "goto('view-corpus')", "corpus", show=False),
        Binding("2", "goto('view-experiments')", "experiments", show=False),
        Binding("3", "goto('view-candidates')", "candidates", show=False),
        Binding("4", "goto('view-activity')", "activity", show=False),
        Binding("left_square_bracket", "prev_view", "prev", show=False),
        Binding("right_square_bracket", "next_view", "next", show=False),
        Binding("r", "reload_view", "reload", show=False),
    ]

    def __init__(
        self,
        *,
        db_path: str | Path = "experiments.sqlite",
        config_path: str | Path = DEFAULT_CONFIG_PATH,
        decks_dir: str | Path | None = None,
        store: ExperimentStore | None = None,
        data: StoreBinding | None = None,
        research=None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        # Register + activate the theme *before* the stylesheet is parsed so the
        # custom palette variables resolve in app.tcss.
        self.register_theme(pal.OMARCHY_THEME)
        self.theme = pal.OMARCHY_THEME.name
        with suppress(Exception):
            self.stylesheet.set_variables(self.get_css_variables())
        self.animation_level = "none"

        self.db_path = Path(db_path)
        self.research = research if research is not None else load_config(config_path)
        self.decks_dir = Path(decks_dir) if decks_dir else default_decks_dir()
        self.store = store if store is not None else ExperimentStore(self.db_path)
        self.data = data if data is not None else StoreBinding(self.db_path)

        self.probe = None
        self.pool = None
        self.active_run: ActiveRun | None = None
        self.cancel_event = threading.Event()
        self._pool_lock = threading.Lock()
        self._quit_armed = False
        self._stats_cache = ""
        self._stats_at = 0.0
        self._log_bridge: LogBridge | None = None

    # -- layout -------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield HeaderBar(id="header-bar")
        yield Tabs(
            *[Tab(label, id=view_id) for view_id, _, label in VIEWS],
            active=VIEWS[0][0],
            id="view-tabs",
        )
        with ContentSwitcher(initial=VIEWS[0][0], id="view-switcher"):
            yield CorpusView(self.data, id="view-corpus")
            yield ExperimentsView(
                self.data, self.store, self.research, self.decks_dir,
                id="view-experiments",
            )
            yield CandidatesView(self.data, id="view-candidates")
            yield ActivityView(self.data, id="view-activity")
        yield StatusLine(id="status-line")

    def on_mount(self) -> None:
        self._install_log_bridge()
        self.set_interval(0.5, self._tick)
        self._refresh_chrome()
        self._focus_view(VIEWS[0][0])

    def on_unmount(self) -> None:
        if self._log_bridge is not None:
            logging.getLogger(_LOGGER_NAME).removeHandler(self._log_bridge)
        with suppress(Exception):
            self.data.close()
        with suppress(Exception):
            self.store.close()

    def _install_log_bridge(self) -> None:
        self._log_bridge = LogBridge()
        self._log_bridge.setLevel(logging.INFO)
        logger = logging.getLogger(_LOGGER_NAME)
        logger.addHandler(self._log_bridge)
        if logger.level == logging.NOTSET:
            logger.setLevel(logging.INFO)

    def drain_logs(self) -> list[tuple[int, str]]:
        return self._log_bridge.drain() if self._log_bridge is not None else []

    # -- run-state helpers used by the Experiments view ---------------------

    def set_pool(self, pool) -> None:
        with self._pool_lock:
            self.pool = pool

    def worker_status(self) -> tuple[str, str]:
        pool = self.pool
        if pool is not None:
            rows = pool_snapshot(pool)
            total = len(rows)
            live = sum(1 for row in rows if row["healthy"])
            if total and live == total:
                return f"{live}/{total} workers live", "ok"
            if live:
                return f"{live}/{total} workers live", "degraded"
            return f"0/{total} workers live", "error"
        probe = self.probe
        if probe is not None:
            if probe.ok_count == 0:
                return f"0/{probe.total} workers reachable", "error"
            if probe.ok_count == probe.total:
                return f"{probe.ok_count}/{probe.total} workers reachable", "ok"
            return f"{probe.ok_count}/{probe.total} workers reachable", "degraded"
        return "workers unchecked", "idle"

    # -- view navigation ----------------------------------------------------

    @on(Tabs.TabActivated, "#view-tabs")
    def _on_tab_activated(self, event: Tabs.TabActivated) -> None:
        view_id = event.tab.id
        if view_id is None:
            return
        with suppress(NoMatches):
            self.query_one("#view-switcher", ContentSwitcher).current = view_id
        self._focus_view(view_id)
        self._refresh_chrome()

    def _focus_view(self, view_id: str) -> None:
        """Move keyboard focus into the newly activated view's primary pane."""

        def _focus() -> None:
            with suppress(NoMatches, AttributeError):
                view = self.query_one(f"#{view_id}")
                focus_primary = getattr(view, "focus_primary", None)
                if callable(focus_primary):
                    focus_primary()

        self.call_after_refresh(_focus)

    def action_goto(self, view_id: str) -> None:
        with suppress(NoMatches):
            self.query_one("#view-tabs", Tabs).active = view_id

    def action_prev_view(self) -> None:
        self._cycle_view(-1)

    def action_next_view(self) -> None:
        self._cycle_view(1)

    def _cycle_view(self, delta: int) -> None:
        ids = [view_id for view_id, _, _ in VIEWS]
        try:
            current = self.query_one("#view-switcher", ContentSwitcher).current
        except NoMatches:
            current = ids[0]
        index = ids.index(current) if current in ids else 0
        self.action_goto(ids[(index + delta) % len(ids)])

    def action_reload_view(self) -> None:
        current = self._current_view_id()
        if current is None:
            return
        with suppress(NoMatches, AttributeError):
            view = self.query_one(f"#{current}", Vertical)
            reload_data = getattr(view, "reload_data", None)
            if callable(reload_data):
                reload_data()

    def _current_view_id(self) -> str | None:
        try:
            return self.query_one("#view-switcher", ContentSwitcher).current
        except NoMatches:
            return None

    # -- help / quit --------------------------------------------------------

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_quit_app(self) -> None:
        run = self.active_run
        if run is not None and not run.finished and not self._quit_armed:
            self._quit_armed = True
            self.notify(
                "run in progress — ctrl+k to cancel, press q again to quit",
                severity="warning",
            )
            return
        self.exit()

    # -- chrome + tick ------------------------------------------------------

    def _tick(self) -> None:
        if not self.is_running:
            return
        for view in self.query(
            "CorpusView, ExperimentsView, CandidatesView, ActivityView"
        ):
            tick = getattr(view, "on_tick", None)
            if callable(tick):
                with suppress(Exception):
                    tick()
        # A tick can race widget teardown; chrome refresh is best-effort.
        with suppress(NoMatches):
            self._refresh_chrome()

    def _refresh_chrome(self) -> None:
        try:
            header = self.query_one(HeaderBar)
            status = self.query_one(StatusLine)
        except NoMatches:
            return
        run = self.active_run

        if run is None:
            run_label, run_state = "no active run", "idle"
        else:
            run_label = short_id(run.run_id)
            if run.error:
                run_state = "error"
            elif run.cancelled:
                run_state = "cancelled"
            elif run.finished:
                run_state = "finished"
            else:
                run_state = "running"

        worker_label, worker_state = self.worker_status()
        header.update_header(
            run_label=run_label,
            run_state=run_state,
            worker_label=worker_label,
            worker_state=worker_state,
        )

        hints: list[tuple[str, str]] = []
        current = self._current_view_id()
        if current is not None:
            with suppress(NoMatches):
                hints = list(getattr(self.query_one(f"#{current}"), "HINTS", []))
        hints += [("?", "keys"), (_PALETTE_KEY, "palette"), ("q", "quit")]
        status.set_content(hints, self._stats_line())

    def _stats_line(self) -> str:
        now = time.monotonic()
        if now - self._stats_at > 2.5:
            counts = self.data.counts()
            cards = self.data.card_count()
            self._stats_cache = (
                f"{cards} cards  ·  {counts['experiments']} runs  ·  "
                f"{counts['games']} games  ·  {counts['candidates']} candidates"
            )
            self._stats_at = now
        line = self._stats_cache
        run = self.active_run
        if run is not None and not run.finished:
            completed = self.data.completed_seeds(run.run_id)
            done = len(completed & set(run.config.seeds))
            elapsed = fmt_duration(time.monotonic() - run.started_at)
            line += f"  ·  {done}/{run.config.total} games  ·  {elapsed}"
        return line


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="combo-tui",
        description="MTG combo-discovery research console (Textual TUI).",
    )
    parser.add_argument(
        "--db", default="experiments.sqlite", help="SQLite experiment store (created on demand)"
    )
    parser.add_argument(
        "--config", default=DEFAULT_CONFIG_PATH, help="research.toml path"
    )
    parser.add_argument(
        "--decks-dir", default=None, help="directory containing .dck decks"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    app = ComboDiscoveryApp(
        db_path=args.db, config_path=args.config, decks_dir=args.decks_dir
    )
    app.run()


__all__ = ["ComboDiscoveryApp", "LogBridge", "default_decks_dir", "main"]
