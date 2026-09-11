"""Corpus view — browse the card store and drive the card loop.

Actions: ``i`` opens the interactions for the highlighted card, ``d`` adds it
to the research scratch deck, ``D`` views/clears that deck. Selecting a
hypothesis in the interactions modal or a card link jumps back into Corpus.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from .. import theme as pal
from ..data import (
    StoreBinding,
    add_card_to_scratch_deck,
    scratch_deck_path,
)
from ..screens import InteractionsScreen, ScratchDeckScreen
from ..widgets import CardDetail, EmptyState

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..app import ComboDiscoveryApp

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
    HINTS = [
        ("/", "search"),
        ("j k", "move"),
        ("i", "interactions"),
        ("d", "add"),
        ("D", "deck"),
    ]
    BINDINGS = [
        Binding("slash", "focus_search", "search", show=False),
        Binding("j", "cursor_down", "down", show=False),
        Binding("k", "cursor_up", "up", show=False),
        Binding("i", "interactions", "interactions", show=False),
        Binding("d", "add_to_deck", "add to deck", show=False),
        Binding("D", "show_scratch_deck", "scratch deck", show=False),
        Binding("r", "reload_data", "reload", show=False),
    ]

    def __init__(self, data: StoreBinding, decks_dir: Path, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.data = data
        self.decks_dir = Path(decks_dir)
        self._search_timer = None
        self._current_card_id: int | None = None
        self._current_card: dict[str, Any] | None = None

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
        self._current_card = card
        self._current_card_id = int(card["id"]) if card else None
        detail = self.query_one("#card-detail", CardDetail)
        empty = self.query_one("#corpus-detail-empty", EmptyState)
        if card is None:
            detail.display = False
            empty.display = True
        else:
            detail.display = True
            empty.display = False
            detail.show(card)

    # -- card loop actions --------------------------------------------------

    def action_interactions(self) -> None:
        card = self._current_card
        if not card:
            self.notify("no card selected", severity="warning")
            return
        self.app.push_screen(InteractionsScreen(card))

    def action_add_to_deck(self) -> None:
        card = self._current_card
        if not card:
            self.notify("no card selected", severity="warning")
            return
        path = scratch_deck_path(self.decks_dir)
        try:
            count, total = add_card_to_scratch_deck(path, str(card.get("name") or ""))
        except ValueError:
            self.notify("card has no usable name", severity="error")
            return
        cast("ComboDiscoveryApp", self.app).refresh_decks()
        self.notify(
            f"added {card.get('name')} → research_scratch.dck "
            f"(×{count}, {total} card{'s' if total != 1 else ''})",
            severity="information",
        )

    def action_show_scratch_deck(self) -> None:
        self.app.push_screen(ScratchDeckScreen(scratch_deck_path(self.decks_dir)))

    def focus_card(self, card_id: int, name: str | None = None) -> None:
        """Search for a card and highlight it — the jump target for links."""
        card = self.data.card(card_id)
        label = str((card or {}).get("name") or name or "")
        search = self.query_one("#corpus-search", Input)
        if self._search_timer is not None:
            self._search_timer.stop()
        search.value = label
        if self._search_timer is not None:
            self._search_timer.stop()
        self._apply_filter(label)
        listing = self.query_one("#corpus-list", OptionList)
        target = str(card_id)
        for index in range(listing.option_count):
            option = listing.get_option_at_index(index)
            if option.id == target:
                listing.highlighted = index
                break
        listing.focus()

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
