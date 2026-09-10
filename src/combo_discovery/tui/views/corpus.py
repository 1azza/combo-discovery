"""Corpus view — browse the card store (gracefully empty until import lands)."""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from .. import theme as pal
from ..data import StoreBinding
from ..widgets import CardDetail, EmptyState

CARD_LIMIT = 400

_EMPTY_TABLE = (
    "Cards arrive with the layer-2 importer.\n"
    "Experiments, candidates and the event log keep working without them."
)
_EMPTY_TABLE_HINT = "The store is created on demand; this is a normal first-run state."
_NO_SELECTION = (
    "Highlight a card to inspect its type line, mana cost, oracle text and\n"
    "extracted effect count here."
)


class CorpusView(Vertical):
    HINTS = [("/", "search"), ("j k", "move"), ("enter", "inspect"), ("r", "reload")]
    BINDINGS = [
        Binding("slash", "focus_search", "search", show=False),
        Binding("j", "cursor_down", "down", show=False),
        Binding("k", "cursor_up", "up", show=False),
        Binding("r", "reload_data", "reload", show=False),
    ]

    def __init__(self, data: StoreBinding, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.data = data
        self._search_timer = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="corpus-body"):
            with Vertical(id="corpus-sidebar", classes="panel"):
                yield Input(placeholder="Search cards…   (press /)", id="corpus-search")
                yield Static("", id="corpus-count", classes="dim")
                yield OptionList(id="corpus-list")
                yield EmptyState(
                    glyph="❖",
                    title="Card corpus is empty",
                    message=_EMPTY_TABLE,
                    hint=_EMPTY_TABLE_HINT,
                    id="corpus-empty",
                )
            with VerticalScroll(id="corpus-detail", classes="panel"):
                yield CardDetail(id="card-detail")
                yield EmptyState(
                    glyph="◌",
                    title="No card selected",
                    message=_NO_SELECTION,
                    id="corpus-detail-empty",
                )

    def on_mount(self) -> None:
        self.reload_data()

    def focus_primary(self) -> None:
        self.query_one("#corpus-list", OptionList).focus()

    # -- data ---------------------------------------------------------------

    def reload_data(self) -> None:
        self._apply_filter(self.query_one("#corpus-search", Input).value)

    def _apply_filter(self, query: str) -> None:
        schema = self.data.card_schema()
        list_widget = self.query_one("#corpus-list", OptionList)
        list_empty = self.query_one("#corpus-empty", EmptyState)

        if schema is None:
            list_widget.display = False
            list_empty.display = True
            list_empty.set_content(
                glyph="❖", title="Card corpus not imported yet",
                message=_EMPTY_TABLE, hint=_EMPTY_TABLE_HINT,
            )
            self.query_one("#corpus-count", Static).update(
                Text("0 cards", style=pal.FAINT)
            )
            self._show_detail(None)
            return

        cards = self.data.list_cards(query, limit=CARD_LIMIT)
        total = self.data.card_count(query)
        list_widget.clear_options()
        if cards:
            list_widget.add_options(
                [Option(Text(str(card["name"] or "Unnamed")), id=str(card["id"])) for card in cards]
            )
            list_widget.display = True
            list_empty.display = False
        else:
            list_widget.display = False
            list_empty.display = True
            if query.strip():
                list_empty.set_content(
                    glyph="◌", title="No matches",
                    message=f'No cards match "{query.strip()}".',
                    hint="Press esc to clear the search.",
                )
            else:
                list_empty.set_content(
                    glyph="❖", title="Card corpus is empty",
                    message=_EMPTY_TABLE, hint=_EMPTY_TABLE_HINT,
                )

        if query.strip():
            label = f"{total} match{'es' if total != 1 else ''}"
            if len(cards) < total:
                label += f" · showing {len(cards)}"
        else:
            label = f"{total} card{'s' if total != 1 else ''}"
            if len(cards) < total:
                label += f" · showing {len(cards)}"
        self.query_one("#corpus-count", Static).update(Text(label, style=pal.FAINT))

        if cards:
            list_widget.highlighted = 0
            self._select(cards[0]["id"])
        else:
            self._show_detail(None)

    def _select(self, card_id: int) -> None:
        self._show_detail(self.data.card(card_id) if card_id is not None else None)

    def _show_detail(self, card: dict[str, Any] | None) -> None:
        detail = self.query_one("#card-detail", CardDetail)
        empty = self.query_one("#corpus-detail-empty", EmptyState)
        if card is None:
            detail.display = False
            empty.display = True
        else:
            detail.display = True
            empty.display = False
            detail.show(card)

    # -- events / actions ---------------------------------------------------

    @on(Input.Changed, "#corpus-search")
    def _on_search_changed(self, event: Input.Changed) -> None:
        event.stop()
        if self._search_timer is not None:
            self._search_timer.stop()
        self._search_timer = self.set_timer(
            0.18, lambda value=event.value: self._apply_filter(value)
        )

    @on(Input.Submitted, "#corpus-search")
    def _on_search_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._apply_filter(event.value)
        self.query_one("#corpus-list", OptionList).focus()

    @on(OptionList.OptionHighlighted, "#corpus-list")
    def _on_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option.id is not None:
            self._select(int(event.option.id))

    @on(OptionList.OptionSelected, "#corpus-list")
    def _on_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id is not None:
            self._select(int(event.option.id))

    def action_focus_search(self) -> None:
        self.query_one("#corpus-search", Input).focus()

    def action_cursor_down(self) -> None:
        self.query_one("#corpus-list", OptionList).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#corpus-list", OptionList).action_cursor_up()


__all__ = ["CorpusView"]
