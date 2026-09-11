"""Experiments view — configure a batch, run it, watch it live."""

from __future__ import annotations

import time
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, DataTable, Input, Label, ProgressBar, Select, Static

from .. import theme as pal
from ..data import (
    POLICY_LABELS,
    ActiveRun,
    CancellablePolicy,
    RunConfig,
    RunCancelled,
    StoreBinding,
    build_pool,
    build_run_config,
    discover_decks,
    fmt_duration,
    fmt_ms,
    no_harness_message,
    pool_snapshot,
    probe_workers,
    resolve_policy,
    scratch_deck_path,
    short_id,
    write_scratch_deck,
)
from ..widgets import EmptyState

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..app import ComboDiscoveryApp

# Probe caps: enough to cover a normal pool without spending forever on a wrong
# base port (each unreachable port costs one short connect timeout).
_PROBE_MAX_WORKERS = 64

_RESULT_COLUMNS = ("seed", "outcome", "winner", "turns", "events", "duration", "reason")


def _outcome_style(outcome: str) -> str:
    if "WIN" in outcome:
        return pal.OK
    if "TIMEOUT" in outcome or "DRAW" in outcome:
        return pal.WARN
    if "ERROR" in outcome or "LOSS" in outcome:
        return pal.ERR
    return pal.MUTED


class ExperimentsView(Vertical):
    HINTS = [("ctrl+r", "run"), ("ctrl+k", "cancel"), ("ctrl+t", "check"), ("n", "new deck")]
    BINDINGS = [
        Binding("ctrl+r", "start_run", "run", show=False),
        Binding("ctrl+k", "cancel_run", "cancel", show=False),
        Binding("ctrl+t", "check_harness", "check", show=False),
        Binding("n", "new_scratch_deck", "new deck", show=False),
        Binding("r", "reload_data", "reload", show=False),
    ]

    def __init__(
        self,
        data: StoreBinding,
        store,
        research,
        decks_dir: Path,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.data = data
        self.store = store
        self.research = research
        self.decks_dir = Path(decks_dir)
        self._decks: list[tuple[str, str]] = []

    # -- layout -------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Horizontal(id="exp-top"):
            with Vertical(id="exp-form", classes="panel"):
                yield Static("Run configuration", classes="panel-title")
                with Horizontal(classes="form-row"):
                    with Vertical(classes="field"):
                        yield Label("Deck A")
                        yield Select([], prompt="deck A", id="exp-deck-a")
                    with Vertical(classes="field"):
                        yield Label("Deck B")
                        yield Select([], prompt="deck B", id="exp-deck-b")
                    with Vertical(classes="field"):
                        yield Label("Policy")
                        yield Select([], prompt="policy", id="exp-policy")
                with Horizontal(classes="form-row"):
                    with Vertical(classes="field small"):
                        yield Label("Seed start")
                        yield Input(value="1", id="exp-seed-start")
                    with Vertical(classes="field small"):
                        yield Label("Seeds")
                        yield Input(value="20", id="exp-seed-count")
                    with Vertical(classes="field small"):
                        yield Label("Workers")
                        yield Input(value="8", id="exp-workers")
                    with Vertical(classes="field small"):
                        yield Label("Base port")
                        yield Input(value="50060", id="exp-base-port")
                    with Vertical(classes="field small"):
                        yield Label("Max turns")
                        yield Input(value="0", id="exp-max-turns")
                    with Vertical(classes="field small"):
                        yield Label("Timeout s")
                        yield Input(value="0", id="exp-timeout")
                with Horizontal(classes="form-row actions"):
                    yield Button("Start run", id="exp-start", variant="primary")
                    yield Button("Cancel", id="exp-cancel", variant="error", disabled=True)
                    yield Button("Check harness", id="exp-check")
                    yield Button("New scratch deck", id="exp-new-scratch")
                yield Static("", id="exp-form-note", classes="form-note")
            with Vertical(id="exp-progress", classes="panel"):
                yield Static("Idle", id="exp-progress-title", classes="panel-title")
                yield ProgressBar(total=100, show_eta=False, id="exp-bar")
                yield Static("", id="exp-progress-stats", classes="dim")
                yield Static("", id="exp-progress-workers", classes="dim")
                with VerticalScroll(id="exp-worker-list", classes="worker-list"):
                    yield Static("", id="exp-worker-list-text", classes="dim")
        with Vertical(id="exp-guidance", classes="panel"):
            yield Static("", id="exp-guidance-text")
        with Horizontal(classes="results-head"):
            yield Static("Results", classes="panel-title")
            yield Static("", id="exp-results-count", classes="dim")
        yield DataTable(id="exp-results", zebra_stripes=True)
        yield EmptyState(
            glyph="◷",
            title="No results yet",
            message="Configure a batch above and press ctrl+r to record games here.",
            id="exp-empty",
        )

    def on_mount(self) -> None:
        self.query_one("#exp-guidance").display = False
        table = self.query_one("#exp-results", DataTable)
        table.add_columns(*_RESULT_COLUMNS)
        table.cursor_type = "row"
        self._populate_form()
        self._load_latest_results()
        self.action_check_harness()

    def focus_primary(self) -> None:
        self.query_one("#exp-start", Button).focus()

    def _populate_form(self) -> None:
        policy = self.query_one("#exp-policy", Select)
        policy.set_options([(label, key) for key, label in POLICY_LABELS.items()])
        policy.value = "default"

        self.query_one("#exp-workers", Input).value = str(self.research.n_workers)
        self.query_one("#exp-base-port", Input).value = str(self.research.base_port)
        self.query_one("#exp-max-turns", Input).value = str(self.research.max_turns)
        self.query_one("#exp-timeout", Input).value = str(self.research.timeout_seconds)
        self.refresh_decks()

    def _deck_value(self, seat: str) -> str | None:
        value = self.query_one(f"#exp-deck-{seat}", Select).value
        return value if isinstance(value, str) and value else None

    def refresh_decks(self) -> None:
        """Re-scan the decks dir (e.g. after the scratch deck changes)."""
        previous = {"a": self._deck_value("a"), "b": self._deck_value("b")}
        self._decks = discover_decks(self.decks_dir)
        available = [path for _, path in self._decks]
        options = [(name, path) for name, path in self._decks]
        for seat in ("a", "b"):
            self.query_one(f"#exp-deck-{seat}", Select).set_options(options)

        if not self._decks:
            self._set_form_note(f"No .dck decks found in {self.decks_dir}", "err")
            return

        first = available[0]
        second = available[1] if len(available) > 1 else first
        deck_a = previous["a"] if previous["a"] in available else first
        deck_b = previous["b"] if previous["b"] in available else second
        self.query_one("#exp-deck-a", Select).value = deck_a
        self.query_one("#exp-deck-b", Select).value = deck_b

    def action_new_scratch_deck(self) -> None:
        path = scratch_deck_path(self.decks_dir)
        if path.exists():
            self.notify("research_scratch.dck already exists", severity="warning")
        else:
            write_scratch_deck(path, [])
            self.notify("created research_scratch.dck", severity="information")
        self.refresh_decks()

    @on(Button.Pressed, "#exp-start")
    def _on_start_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_start_run()

    @on(Button.Pressed, "#exp-cancel")
    def _on_cancel_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_cancel_run()

    @on(Button.Pressed, "#exp-check")
    def _on_check_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_check_harness()

    @on(Button.Pressed, "#exp-new-scratch")
    def _on_new_scratch_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_new_scratch_deck()

    # -- form helpers -------------------------------------------------------

    _NOTE_TONES = {
        "err": pal.ERR,
        "warn": pal.WARN,
        "muted": pal.MUTED,
        "faint": pal.FAINT,
        "ok": pal.OK,
    }

    def _set_form_note(self, message: str, tone: str = "muted") -> None:
        """Set the full-width note under the action buttons (wraps, never clips)."""
        widget = self.query_one("#exp-form-note", Static)
        widget.update(Text(message, style=self._NOTE_TONES.get(tone, pal.MUTED)))

    def _show_guidance(self, message: str, *, title: str | None = None) -> None:
        panel = self.query_one("#exp-guidance")
        widget = self.query_one("#exp-guidance-text", Static)
        if message:
            body = Text()
            if title:
                body.append(f"{title}\n\n", style=f"bold {pal.WARN}")
            body.append(message, style=pal.MUTED)
            widget.update(body)
            panel.display = True
        else:
            widget.update("")
            panel.display = False

    def _deck(self, seat: str) -> tuple[str, str] | None:
        value = self.query_one(f"#exp-deck-{seat}", Select).value
        if not isinstance(value, str) or not value:
            return None
        return (Path(value).stem, value)

    def _read_config(self) -> tuple[RunConfig | None, list[str]]:
        policy = self.query_one("#exp-policy", Select).value
        policy_name = policy if isinstance(policy, str) and policy else "default"
        return build_run_config(
            self._deck("a"),
            self._deck("b"),
            policy_name,
            self.query_one("#exp-seed-start", Input).value,
            self.query_one("#exp-seed-count", Input).value,
            self.query_one("#exp-workers", Input).value,
            self.query_one("#exp-base-port", Input).value,
            host="localhost",
            max_turns=self.query_one("#exp-max-turns", Input).value,
            timeout_seconds=self.query_one("#exp-timeout", Input).value,
        )

    # -- results ------------------------------------------------------------

    def _load_latest_results(self) -> None:
        experiments = self.data.list_experiments(limit=1)
        if not experiments:
            self._toggle_results_empty(True)
            self.query_one("#exp-results-count", Static).update(
                Text("no runs recorded", style=pal.FAINT)
            )
            return
        run_id = str(experiments[0]["id"])
        self._fill_results(run_id)

    def _fill_results(self, run_id: str) -> None:
        rows = self.data.games_for_run(run_id)
        table = self.query_one("#exp-results", DataTable)
        table.clear()
        for row in rows:
            outcome = str(row.get("outcome") or "—")
            table.add_row(
                str(row.get("seed", "—")),
                Text(outcome, style=_outcome_style(outcome)),
                str(row.get("winner", "—")),
                str(row.get("turn_count", "—")),
                str(row.get("event_count", "—")),
                fmt_ms(row.get("duration_ms")),
                str(row.get("reason") or ""),
            )
        count = len(rows)
        self.query_one("#exp-results-count", Static).update(
            Text(f"run {short_id(run_id)} · {count} game{'s' if count != 1 else ''}", style=pal.FAINT)
        )
        self._toggle_results_empty(count == 0)

    def _toggle_results_empty(self, empty: bool) -> None:
        self.query_one("#exp-results", DataTable).display = not empty
        self.query_one("#exp-empty", EmptyState).display = empty

    # -- probing ------------------------------------------------------------

    def action_check_harness(self) -> None:
        try:
            base_port = int(self.query_one("#exp-base-port", Input).value)
            workers = int(self.query_one("#exp-workers", Input).value)
        except (TypeError, ValueError):
            base_port = self.research.base_port
            workers = self.research.n_workers
        workers = max(1, min(workers, _PROBE_MAX_WORKERS))
        self._set_form_note("checking harness…", "faint")
        self._probe_workers("localhost", base_port, workers)

    @work(thread=True, group="probe", exclusive=True)
    def _probe_workers(self, host: str, base_port: int, workers: int) -> None:
        result = probe_workers(host, base_port, workers)
        app = cast("ComboDiscoveryApp", self.app)
        app.call_from_thread(self._probe_done, result)

    def _probe_done(self, probe) -> None:
        app = cast("ComboDiscoveryApp", self.app)
        app.probe = probe
        if probe.ok_count == 0:
            self._set_form_note("no live harness — connection guidance below", "warn")
            self._show_guidance(no_harness_message(probe), title="No live harness")
            self.notify("no live harness found", severity="warning")
        else:
            self._set_form_note("")
            self._show_guidance("")
            self.notify(
                f"{probe.ok_count}/{probe.total} worker ports reachable",
                severity="information" if probe.ok_count == probe.total else "warning",
            )
        self._update_progress()

    # -- running ------------------------------------------------------------

    def action_start_run(self) -> None:
        app = cast("ComboDiscoveryApp", self.app)
        if app.active_run is not None and not app.active_run.finished:
            self.notify("a run is already in progress", severity="warning")
            return
        config, errors = self._read_config()
        if config is None:
            self._set_form_note("  ·  ".join(errors), "err")
            self.notify("check the run configuration", severity="error")
            return
        probe = app.probe
        if probe is None:
            self.action_check_harness()
            self._set_form_note("checking harness — press ctrl+r again in a moment", "muted")
            return
        if probe.ok_count == 0:
            self._set_form_note("no live harness — connection guidance below", "warn")
            self._show_guidance(no_harness_message(probe), title="No live harness")
            return

        self._set_form_note("")
        self._show_guidance("")
        run_id = self.store.start_experiment(
            **self.research.experiment_meta(), config=config.experiment_config()
        )
        app.active_run = ActiveRun(
            run_id=run_id, config=config, started_at=time.monotonic()
        )
        app.cancel_event.clear()
        self.query_one("#exp-start", Button).disabled = True
        self.query_one("#exp-cancel", Button).disabled = False
        self._fill_results(run_id)
        self._update_progress()
        self.notify(f"started run {short_id(run_id)}", severity="information")
        self._execute_run(config, run_id)

    @work(thread=True, group="experiment", exclusive=True)
    def _execute_run(self, config: RunConfig, run_id: str) -> None:
        app = cast("ComboDiscoveryApp", self.app)
        try:
            pool = build_pool(app.probe)
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator
            app.call_from_thread(self._run_failed, f"{type(exc).__name__}: {exc}")
            return
        app.set_pool(pool)
        policy = CancellablePolicy(resolve_policy(config.policy_name), app.cancel_event)
        try:
            pool.map_games(
                config.deck_pair,
                config.seeds,
                policy=policy,
                max_turns=config.max_turns,
                timeout_seconds=config.timeout_seconds,
                store=app.store,
                run_id=run_id,
            )
        except RunCancelled:
            app.call_from_thread(self._run_cancelled, run_id)
        except BaseException as exc:  # noqa: BLE001 - any pool failure is surfaced
            app.call_from_thread(self._run_failed, f"{type(exc).__name__}: {exc}")
        else:
            app.call_from_thread(self._run_finished, run_id)
        finally:
            app.set_pool(None)
            with suppress(Exception):
                pool.close()

    def _run_finished(self, run_id: str) -> None:
        app = cast("ComboDiscoveryApp", self.app)
        if app.active_run is not None:
            if app.cancel_event.is_set():
                app.active_run.cancelled = True
            app.active_run.finished = True
        self._finish_ui(run_id)
        self.notify("run complete", severity="information")

    def _run_cancelled(self, run_id: str) -> None:
        app = cast("ComboDiscoveryApp", self.app)
        if app.active_run is not None:
            app.active_run.cancelled = True
            app.active_run.finished = True
        self._finish_ui(run_id)
        self.notify("run cancelled", severity="warning")

    def _run_failed(self, message: str) -> None:
        app = cast("ComboDiscoveryApp", self.app)
        if app.active_run is not None:
            app.active_run.error = message
            app.active_run.finished = True
        self._show_guidance(message, title="Run failed")
        self._finish_ui(app.active_run.run_id if app.active_run else "")
        self.notify(f"run failed: {message}", severity="error")

    def _finish_ui(self, run_id: str) -> None:
        self.query_one("#exp-start", Button).disabled = False
        self.query_one("#exp-cancel", Button).disabled = True
        if run_id:
            self._fill_results(run_id)
        self._update_progress()

    def action_cancel_run(self) -> None:
        app = cast("ComboDiscoveryApp", self.app)
        run = app.active_run
        if run is None or run.finished:
            return
        app.cancel_event.set()
        run.cancelled = True
        self.query_one("#exp-cancel", Button).disabled = True
        self.notify("cancelling after the current decision…", severity="warning")

    def action_reload_data(self) -> None:
        self._load_latest_results()
        self.action_check_harness()

    # -- live tick ----------------------------------------------------------

    def on_tick(self) -> None:
        with suppress(Exception):
            self._update_progress()

    @staticmethod
    def _worker_rows_text(app) -> Text:
        """One row per worker port, for the capped scroll area.

        Unreachable ports from a probe are amber: with no run active, "no
        harness" is an expected idle state, not a failure. Red is reserved for
        a worker excluded during a run (i.e. one that actually failed).
        """
        lines: list[Text] = []
        rows = pool_snapshot(app.pool)
        if rows:
            for row in rows:
                line = Text()
                line.append(f"{row['port']}  ", style=pal.MUTED)
                if row["healthy"]:
                    line.append("live", style=pal.OK)
                else:
                    line.append("excluded", style=pal.ERR)
                if row["failures"]:
                    line.append(f"  fails {row['failures']}", style=pal.FAINT)
                lines.append(line)
        else:
            probe = app.probe
            if probe is not None:
                for port, ok, detail in probe.results:
                    line = Text()
                    line.append(f"{port}  ", style=pal.MUTED)
                    line.append("ok" if ok else detail, style=pal.OK if ok else pal.WARN)
                    lines.append(line)
            else:
                return Text("unchecked — press ctrl+t", style=pal.FAINT)

        if not lines:
            return Text("unchecked — press ctrl+t", style=pal.FAINT)
        combined = lines[0]
        for line in lines[1:]:
            combined.append("\n")
            combined.append_text(line)
        return combined

    def _update_progress(self) -> None:
        app = cast("ComboDiscoveryApp", self.app)
        run = app.active_run

        title = self.query_one("#exp-progress-title", Static)
        bar = self.query_one("#exp-bar", ProgressBar)
        stats = self.query_one("#exp-progress-stats", Static)
        workers = self.query_one("#exp-progress-workers", Static)
        worker_rows = self.query_one("#exp-worker-list-text", Static)

        worker_label, worker_state = app.worker_status()
        worker_color = {
            "ok": pal.OK,
            "degraded": pal.WARN,
            "warn": pal.WARN,
            "error": pal.ERR,
        }.get(worker_state, pal.FAINT)
        workers.update(Text(worker_label, style=f"bold {worker_color}"))
        worker_rows.update(self._worker_rows_text(app))

        if run is None:
            return

        completed = self.data.completed_seeds(run.run_id)
        done = len(completed & set(run.config.seeds))
        total = run.config.total
        bar.update(total=total, progress=done)

        if run.error:
            state, color = "failed", pal.ERR
        elif run.cancelled:
            state, color = "cancelled", pal.WARN
        elif run.finished:
            state, color = "finished", pal.OK
        else:
            state, color = "running", pal.ACCENT
        title.update(
            Text(f"{state}  ·  run {short_id(run.run_id)}", style=f"bold {color}")
        )

        if run.finished or run.cancelled:
            elapsed = time.monotonic() - run.started_at
        else:
            elapsed = time.monotonic() - run.started_at
        pending = [seed for seed in run.config.seeds if seed not in completed][:8]
        next_line = "  ".join(str(seed) for seed in pending) if pending else "—"
        deck_a = run.config.deck_a[0]
        deck_b = run.config.deck_b[0]
        stats.update(
            Text(
                f"{done}/{total} games  ·  {fmt_duration(elapsed)} elapsed  ·  "
                f"policy {POLICY_LABELS[run.config.policy_name]}  ·  "
                f"{deck_a} vs {deck_b}  ·  next seeds  {next_line}",
                style=pal.MUTED,
            )
        )


__all__ = ["ExperimentsView"]
