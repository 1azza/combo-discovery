"""Modal screens: the key map, card interactions, and the scratch deck.

Hypothesis ids are used as the ``adjudications.candidate_id`` until the
``combo_hypotheses`` and ``candidates`` tables are linked (see
:meth:`combo_discovery.store.ExperimentStore.record_adjudication`).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from rich.table import Table
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from . import theme as pal
from .data import (
    effective_status,
    read_scratch_deck,
    scratch_deck_summary,
    status_color,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .app import ComboDiscoveryApp

KEY_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    (
        "Navigate",
        [
            ("1 2 3 4 5", "Corpus / Experiments / Candidates / Card Lab / Activity"),
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
        "Card loop",
        [
            ("i", "interactions for the selected card (Corpus)"),
            ("d", "add card to the research scratch deck"),
            ("D", "view or clear the scratch deck"),
            ("v", "cycle verdict: proposed → verified → refuted → inconclusive"),
        ],
    ),
    (
        "Experiments",
        [
            ("ctrl+r", "start the configured batch"),
            ("ctrl+k", "cancel the active run"),
            ("ctrl+t", "re-check worker ports"),
            ("n", "create an empty research_scratch.dck"),
        ],
    ),
    (
        "Card Lab",
        [
            ("/", "pick a card (search the corpus)"),
            ("f", "known filter: exact 2-card only / all combos"),
            ("r", "re-run this card's evaluation and persist it"),
            ("d", "add the card to the research scratch deck"),
            ("g", "show the proposal's pattern module (display only)"),
            ("enter", "jump to a partner card in Corpus"),
        ],
    ),
]


def _score_text(score: Any) -> str:
    return f"{float(score):.2f}" if isinstance(score, (int, float)) else "—"


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
            yield Static("keyboard-first · no mouse required", id="help-sub")
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


class InteractionsScreen(ModalScreen[None]):
    """Top hypotheses involving one card, with a jump to the partner card."""

    BINDINGS = [
        Binding("escape", "dismiss_screen", "close", show=False),
        Binding("q", "dismiss_screen", "close", show=False),
    ]

    def __init__(self, card: dict[str, Any], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.card = card
        self._by_id: dict[int, dict[str, Any]] = {}
        self._current_id = int(card.get("id") or 0)

    def compose(self) -> ComposeResult:
        with Vertical(id="interactions-card"):
            yield Static(f"Interactions · {self.card.get('name')}", id="interactions-title")
            yield Static("loading top hypotheses…", id="interactions-sub")
            with Horizontal(id="interactions-body"):
                yield OptionList(id="interactions-list")
                with VerticalScroll(id="interactions-detail"):
                    yield Static("", id="interactions-mechanism")
            yield Static(
                "enter jump to partner card   ·   esc close", id="interactions-foot"
            )

    def on_mount(self) -> None:
        self._load(self._current_id)

    @work(thread=True, group="interactions", exclusive=True)
    def _load(self, card_id: int) -> None:
        app = cast("ComboDiscoveryApp", self.app)
        rows = app.data.hypotheses_for_card(card_id, limit=200)
        app.call_from_thread(self._populate, rows)

    def _populate(self, rows: list[dict[str, Any]]) -> None:
        self._by_id = {int(row["id"]): row for row in rows}
        listing = self.query_one("#interactions-list", OptionList)
        listing.clear_options()
        sub = self.query_one("#interactions-sub", Static)
        if not rows:
            listing.display = False
            sub.update(
                Text(
                    "no interactions recorded for this card — the ontology may not "
                    "link it yet",
                    style=pal.FAINT,
                )
            )
            self._show_mechanism(None)
            return
        listing.display = True
        listing.add_options(
            Option(self._row_label(row), id=str(row["id"])) for row in rows
        )
        sub.update(
            Text(f"{len(rows)} hypotheses · sorted by score", style=pal.FAINT)
        )
        listing.highlighted = 0
        self._show_mechanism(rows[0])

    def _partners(self, row: dict[str, Any]) -> list[dict[str, Any]]:
        return [card for card in row.get("cards") or [] if int(card["id"]) != self._current_id]

    def _row_label(self, row: dict[str, Any]) -> Text:
        text = Text()
        text.append(f"{_score_text(row.get('score'))}  ", style=pal.ACCENT)
        text.append(str(row.get("pattern") or "—"), style=pal.MUTED)
        partners = self._partners(row)
        text.append("   ", style=pal.FAINT)
        text.append(
            " + ".join(str(card["name"]) for card in partners) or "—",
            style=pal.TEXT,
        )
        return text

    def _show_mechanism(self, row: dict[str, Any] | None) -> None:
        widget = self.query_one("#interactions-mechanism", Static)
        if row is None:
            widget.update(
                Text("Select a hypothesis to read its mechanism.", style=pal.FAINT)
            )
            return
        mechanism = Text()
        mechanism.append(
            str(row.get("mechanism") or "No mechanism text recorded."), style=pal.TEXT
        )
        partners = self._partners(row)
        if partners:
            mechanism.append("\n\npartners  ", style=pal.FAINT)
            mechanism.append(
                " + ".join(str(card["name"]) for card in partners), style=pal.ACCENT
            )
        mechanism.append("\n\nstatus  ", style=pal.FAINT)
        status = effective_status(row)
        mechanism.append(status, style=status_color(status))
        widget.update(mechanism)

    @on(OptionList.OptionHighlighted, "#interactions-list")
    def _on_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option.id is not None:
            self._show_mechanism(self._by_id.get(int(event.option.id)))

    @on(OptionList.OptionSelected, "#interactions-list")
    def _on_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id is None:
            return
        row = self._by_id.get(int(event.option.id))
        if not row:
            return
        partners = self._partners(row)
        if not partners:
            return
        partner = partners[0]
        app = cast("ComboDiscoveryApp", self.app)
        self.dismiss()
        app.open_card(int(partner["id"]), str(partner["name"]))

    def action_dismiss_screen(self) -> None:
        self.app.pop_screen()


class ScratchDeckScreen(ModalScreen[None]):
    """Contents of ``research_scratch.dck``, with a clear action."""

    BINDINGS = [
        Binding("escape", "dismiss_screen", "close", show=False),
        Binding("q", "dismiss_screen", "close", show=False),
        Binding("x", "clear_deck", "clear", show=False),
    ]

    def __init__(self, path: str | Path, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.path = Path(path)

    def compose(self) -> ComposeResult:
        with Vertical(id="scratch-card"):
            yield Static("research_scratch.dck", id="scratch-title")
            yield Static("", id="scratch-sub")
            with VerticalScroll(id="scratch-body"):
                yield Static("", id="scratch-list")
            yield Static("x clear deck   ·   esc close", id="scratch-foot")

    def on_mount(self) -> None:
        self.reload()

    def reload(self) -> None:
        distinct, total = scratch_deck_summary(self.path)
        _, entries = read_scratch_deck(self.path)
        self.query_one("#scratch-sub", Static).update(
            Text(
                f"{total} card{'s' if total != 1 else ''} · "
                f"{distinct} distinct · {self.path.parent.name}/{self.path.name}",
                style=pal.FAINT,
            )
        )
        body = Text()
        if entries:
            for count, name in entries:
                body.append(f"{count:>2}  ", style=pal.ACCENT)
                body.append(f"{name}\n", style=pal.TEXT)
        else:
            body.append(
                "Deck is empty — highlight a card in Corpus and press d to add it.",
                style=pal.MUTED,
            )
        self.query_one("#scratch-list", Static).update(body)

    def action_clear_deck(self) -> None:
        from .data import clear_scratch_deck

        clear_scratch_deck(self.path)
        self.reload()
        app = cast("ComboDiscoveryApp", self.app)
        app.refresh_decks()
        self.notify("scratch deck cleared", severity="warning")

    def action_dismiss_screen(self) -> None:
        self.app.pop_screen()


class CardPickerScreen(ModalScreen[dict | None]):
    """Search the corpus and return the chosen card (or ``None``)."""

    BINDINGS = [
        Binding("escape", "dismiss_picker", "close", show=False),
        Binding("q", "dismiss_picker", "close", show=False),
    ]

    def __init__(self, data: Any, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.data = data
        self._timer = None

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-card"):
            yield Static("Pick a card", id="picker-title")
            yield Input(placeholder="Search cards…   (type to filter)", id="picker-search")
            yield Static("", id="picker-count", classes="dim")
            yield OptionList(id="picker-list")
            yield Static("enter open   ·   esc close", id="picker-foot")

    def on_mount(self) -> None:
        self.query_one("#picker-search", Input).focus()
        self._query("")

    @on(Input.Changed, "#picker-search")
    def _on_changed(self, event: Input.Changed) -> None:
        event.stop()
        if self._timer is not None:
            self._timer.stop()
        self._timer = self.set_timer(0.15, lambda value=event.value: self._query(value))

    @on(Input.Submitted, "#picker-search")
    def _on_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        listing = self.query_one("#picker-list", OptionList)
        option = listing.highlighted
        if option is None and listing.option_count:
            option = 0
        if option is not None:
            self._choose(int(listing.get_option_at_index(option).id))

    @on(OptionList.OptionSelected, "#picker-list")
    def _on_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id is not None:
            self._choose(int(event.option.id))

    def _query(self, text: str) -> None:
        cards = self.data.list_cards(text, limit=50)
        listing = self.query_one("#picker-list", OptionList)
        listing.clear_options()
        for card in cards:
            label = Text(str(card.get("name") or "—"), style=pal.TEXT)
            if card.get("type_line"):
                label.append(f"   {card['type_line']}", style=pal.FAINT)
            listing.add_option(Option(label, id=str(card["id"])))
        if cards:
            listing.highlighted = 0
        count = self.data.card_count(text)
        self.query_one("#picker-count", Static).update(
            Text(f"showing {len(cards)} of {count}", style=pal.FAINT)
        )

    def _choose(self, card_id: int) -> None:
        self.dismiss(self.data.card(card_id))

    def action_dismiss_picker(self) -> None:
        self.dismiss(None)


__all__ = [
    "CardPickerScreen",
    "HelpScreen",
    "InteractionsScreen",
    "KEY_GROUPS",
    "ScratchDeckScreen",
]
