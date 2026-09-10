"""Activity view — a live tail of experiment events plus worker health."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, RichLog, Static

from .. import theme as pal
from ..data import StoreBinding, fmt_ms, pool_snapshot, short_id

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..app import ComboDiscoveryApp

# events carry a normalized type; tint a few of the interesting ones
_EVENT_COLORS = {
    "GameOver": pal.OK,
    "SpellCast": pal.ACCENT,
    "SpellResolved": pal.ACCENT,
    "PermanentEntered": pal.MUTED,
    "LifeChanged": pal.WARN,
    "PlayerDamaged": pal.ERR,
    "CardDrawn": pal.FAINT,
    "TurnStarted": pal.FAINT,
}

_MAX_LINES = 2000


class ActivityView(Vertical):
    HINTS = [("r", "reload feed"), ("c", "clear")]
    BINDINGS = [
        Binding("r", "reload_data", "reload", show=False),
        Binding("c", "clear_feed", "clear", show=False),
    ]

    def __init__(self, data: StoreBinding, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.data = data
        self._last_event_id = 0
        self._last_log_index = 0
        self._seeded = False

    def compose(self) -> ComposeResult:
        with Horizontal(id="activity-body"):
            with Vertical(id="activity-feed", classes="panel"):
                yield Static("Experiment events", classes="panel-title")
                yield RichLog(
                    id="activity-log",
                    highlight=False,
                    markup=False,
                    wrap=True,
                    max_lines=_MAX_LINES,
                )
            with Vertical(id="activity-health", classes="panel"):
                yield Static("Worker health", classes="panel-title")
                yield DataTable(id="activity-workers", show_header=True, zebra_stripes=True)
                yield Static("", id="activity-run", classes="dim")
                yield Static("", id="activity-games", classes="dim")

    def on_mount(self) -> None:
        table = self.query_one("#activity-workers", DataTable)
        table.add_columns("port", "state", "fails")
        table.cursor_type = "none"
        self._reload_feed()

    def focus_primary(self) -> None:
        self.query_one("#activity-log", RichLog).focus()

    # -- ticking ------------------------------------------------------------

    def on_tick(self) -> None:
        self._drain_new_events()
        self._drain_logs()
        self._refresh_health()

    def _active_run(self):
        app = cast("ComboDiscoveryApp", self.app)
        return app.active_run

    def reload_data(self) -> None:
        self._reload_feed()

    def _reload_feed(self) -> None:
        log = self.query_one("#activity-log", RichLog)
        log.clear()
        rows = self.data.event_tail(limit=200)
        for row in rows:
            log.write(self._event_text(row))
        self._last_event_id = int(rows[-1]["id"]) if rows else 0
        self._seeded = True
        self._last_log_index = 0
        self._refresh_health()

    def action_clear_feed(self) -> None:
        self.query_one("#activity-log", RichLog).clear()

    def _drain_new_events(self) -> None:
        if not self._seeded:
            return
        rows = self.data.event_tail(since_id=self._last_event_id, limit=200)
        if not rows:
            return
        log = self.query_one("#activity-log", RichLog)
        for row in rows:
            log.write(self._event_text(row))
            self._last_event_id = int(row["id"])

    def _drain_logs(self) -> None:
        app = cast("ComboDiscoveryApp", self.app)
        logs = app.drain_logs()
        if not logs:
            return
        log = self.query_one("#activity-log", RichLog)
        for levelno, message in logs:
            style = pal.ERR if levelno >= 40 else (pal.WARN if levelno >= 30 else pal.FAINT)
            text = Text()
            text.append("log ", style=f"bold {style}")
            text.append(message, style=style)
            log.write(text)

    # -- rendering ----------------------------------------------------------

    @staticmethod
    def _event_text(row: dict[str, Any]) -> Text:
        event_type = str(row.get("type") or "?")
        color = _EVENT_COLORS.get(event_type, pal.MUTED)
        text = Text(no_wrap=False)
        text.append(f"{short_id(row.get('run_id'))}  ", style=pal.FAINT)
        text.append(f"seed {row.get('seed', '—')}  ", style=pal.MUTED)
        text.append(f"T{row.get('turn', '—')}  ", style=pal.FAINT)
        text.append(event_type, style=color)
        if row.get("card_name"):
            text.append(f"  {row['card_name']}", style=pal.TEXT)
        detail = str(row.get("detail_raw") or "").strip()
        if detail:
            text.append(f"  ·  {detail}", style=pal.FAINT)
        return text

    def _refresh_health(self) -> None:
        app = cast("ComboDiscoveryApp", self.app)
        table = self.query_one("#activity-workers", DataTable)

        rows = pool_snapshot(app.pool)
        table.clear()
        if rows:
            for row in rows:
                state = "live" if row["healthy"] else "excluded"
                color = pal.OK if row["healthy"] else pal.ERR
                table.add_row(
                    str(row["port"]),
                    Text(state, style=color),
                    str(row["failures"]),
                )
        else:
            table.add_row("—", Text("no active pool", style=pal.FAINT), "—")

        probe = app.probe
        if probe is not None and not rows:
            reachable = ", ".join(str(p) for p in probe.reachable) or "none"
            probe_line = f"last check  {probe.ok_count}/{probe.total} reachable  ({reachable})"
        elif probe is not None:
            probe_line = f"probe  {probe.ok_count}/{probe.total} reachable"
        else:
            probe_line = "probe  not run yet  (ctrl+t on Experiments)"

        run = app.active_run
        if run is not None:
            done = len(self.data.completed_seeds(run.run_id) & set(run.config.seeds))
            state = "finished" if run.finished else ("cancelled" if run.cancelled else "running")
            if run.error:
                state = "error"
            run_line = f"run {short_id(run.run_id)}  ·  {state}  ·  {done}/{run.config.total} games"
        else:
            run_line = "no active run"
        self.query_one("#activity-run", Static).update(Text(run_line, style=pal.MUTED))

        recent = self.data.recent_games(limit=4)
        if recent:
            lines = []
            for game in recent:
                outcome = str(game.get("outcome") or "—").replace("OUTCOME_", "")
                lines.append(
                    f"seed {game.get('seed')}  ·  {outcome}  ·  "
                    f"{game.get('turn_count')}t  ·  {fmt_ms(game.get('duration_ms'))}"
                )
            self.query_one("#activity-games", Static).update(
                Text("\n".join(lines), style=pal.FAINT)
            )
        else:
            self.query_one("#activity-games", Static).update(
                Text(probe_line, style=pal.FAINT)
            )


__all__ = ["ActivityView"]
