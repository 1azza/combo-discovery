"""Modal screens: the keybinding help card."""

from __future__ import annotations

from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from . import theme as pal

KEY_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    (
        "Navigate",
        [
            ("1 2 3 4", "jump to Corpus / Experiments / Candidates / Activity"),
            ("[  ]", "previous / next view"),
            ("tab  shift+tab", "move focus between panes"),
            ("?  f1", "open this key map"),
            ("ctrl+p", "command palette"),
            ("q", "quit (press twice during a run)"),
        ],
    ),
    (
        "Lists",
        [
            ("j  k", "move down / up"),
            ("enter", "open / inspect the highlighted row"),
            ("/", "focus the corpus search box"),
            ("r", "reload the current view from the store"),
            ("esc", "clear search / dismiss"),
        ],
    ),
    (
        "Experiments",
        [
            ("ctrl+r", "start the configured batch"),
            ("ctrl+k", "cancel the active run"),
            ("ctrl+t", "re-check worker ports"),
        ],
    ),
]


class HelpScreen(ModalScreen[None]):
    """A quiet key map, dismissed with esc / ? / q."""

    BINDINGS = [
        Binding("escape", "dismiss_help", "close", show=False),
        Binding("question_mark", "dismiss_help", "close", show=False),
        Binding("q", "dismiss_help", "close", show=False),
        Binding("f1", "dismiss_help", "close", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="help-card"):
            yield Static("Key map", id="help-title")
            yield Static(
                "keyboard-first · no mouse required", id="help-sub"
            )
            with VerticalScroll(id="help-body"):
                yield Static(self._render_keys(), id="help-keys")
            yield Static("esc to close", id="help-foot")

    @staticmethod
    def _render_keys() -> Table:
        table = Table.grid(padding=(0, 2))
        table.add_column(justify="right", no_wrap=True)
        table.add_column(justify="left")
        for group, items in KEY_GROUPS:
            table.add_row(Text(group.upper(), style=f"bold {pal.FAINT}"), Text(""))
            for key, description in items:
                table.add_row(
                    Text(key, style=f"bold {pal.ACCENT}"),
                    Text(description, style=pal.TEXT),
                )
            table.add_row(Text(""), Text(""))
        return table

    def action_dismiss_help(self) -> None:
        self.app.pop_screen()


__all__ = ["HelpScreen", "KEY_GROUPS"]
