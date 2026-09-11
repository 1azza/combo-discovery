"""Candidates view — the live combo-hypothesis graph.

Lists ``combo_hypotheses`` (joined to card names and the latest adjudication),
filtered by pattern or a card-name substring. Row ids are hypothesis ids; the
verdict action appends an adjudication through the store (documented bridge:
``adjudications.candidate_id`` currently stores the hypothesis row id).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Input, OptionList, Select, Static
from textual.widgets.option_list import Option

from .. import theme as pal
from ..data import (
    StoreBinding,
    effective_status,
    next_status,
    status_color,
)
from ..widgets import CandidateDetail, EmptyState

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..app import ComboDiscoveryApp

HYPOTHESIS_LIMIT = 500
ALL_PATTERNS = 0

_EMPTY_DB = (
    "No combo hypotheses recorded yet. They are built by `combo-build-ontology`\n"
    "from the imported card corpus."
)
_EMPTY_HINT = "use the pattern filter or search by card name"
_NO_SELECTION = (
    "Highlight a hypothesis to read its mechanism, jump to its cards, and\n"
    "cycle its verdict."
)


class CandidatesView(Vertical):
    HINTS = [("j k", "move"), ("enter", "inspect"), ("v", "verdict"), ("r", "reload")]
    BINDINGS = [
        Binding("j", "cursor_down", "down", show=False),
        Binding("k", "cursor_up", "up", show=False),
        Binding("v", "cycle_verdict", "verdict", show=False),
        Binding("slash", "focus_search", "search", show=False),
        Binding("r", "reload_data", "reload", show=False),
    ]

    def __init__(self, data: StoreBinding, store, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.data = data
        self.store = store
        self._pattern_id = ALL_PATTERNS
        self._search = ""
        self._rows: list[dict[str, Any]] = []
        self._gen = 0
        self._wanted_id: int | None = None
        self._current_hypothesis: dict[str, Any] | None = None
        self._search_timer = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="cand-body"):
            with Vertical(id="cand-sidebar", classes="panel"):
                yield Static("Hypotheses", classes="panel-title")
                yield Static("", id="cand-count", classes="dim")
                with Horizontal(classes="filter-row"):
                    yield Select(
                        [("All patterns", ALL_PATTERNS)],
                        value=ALL_PATTERNS,
                        allow_blank=False,
                        id="cand-pattern",
                    )
                    yield Input(placeholder="card name…", id="cand-search")
                yield OptionList(id="cand-list")
                yield EmptyState(
                    glyph="◇",
                    title="No hypotheses yet",
                    message=_EMPTY_DB,
                    hint=_EMPTY_HINT,
                    id="cand-empty",
                )
            with VerticalScroll(id="cand-detail", classes="panel"):
                yield CandidateDetail(id="candidate-detail")
                yield EmptyState(
                    glyph="◌",
                    title="No hypothesis selected",
                    message=_NO_SELECTION,
                    id="cand-detail-empty",
                )

    def on_mount(self) -> None:
        self._load_patterns()
        self.reload_data()

    def focus_primary(self) -> None:
        self.query_one("#cand-list", OptionList).focus()

    # -- data ---------------------------------------------------------------

    def _load_patterns(self) -> None:
        select = self.query_one("#cand-pattern", Select)
        options = [(str(p.get("name") or p.get("id")), int(p["id"]))
                   for p in self.data.list_patterns()]
        select.set_options([("All patterns", ALL_PATTERNS), *options])
        select.value = ALL_PATTERNS

    def reload_data(self) -> None:
        self._gen += 1
        self._load_async(self._gen, self._pattern_id, self._search)

    @work(thread=True, group="candidates", exclusive=True)
    def _load_async(self, gen: int, pattern_id: int, search: str) -> None:
        app = cast("ComboDiscoveryApp", self.app)
        pattern = pattern_id or None
        rows = self.data.list_hypotheses(
            pattern_id=pattern, search=search, limit=HYPOTHESIS_LIMIT
        )
        total = self.data.hypothesis_total(pattern_id=pattern, search=search)
        app.call_from_thread(self._apply, gen, rows, total)

    def _apply(self, gen: int, rows: list[dict[str, Any]], total: int) -> None:
        if gen != self._gen:
            return
        self._rows = rows
        listing = self.query_one("#cand-list", OptionList)
        empty = self.query_one("#cand-empty", EmptyState)
        listing.clear_options()

        if rows:
            listing.add_options(
                [Option(self._label(row), id=str(row["id"])) for row in rows]
            )
            listing.display = True
            empty.display = False
            index = 0
            if self._wanted_id is not None:
                for i, row in enumerate(rows):
                    if int(row["id"]) == self._wanted_id:
                        index = i
                        break
            listing.highlighted = index
            self._show(int(rows[index]["id"]))
        else:
            listing.display = False
            empty.display = True
            self._show(None)

        if search := self._search.strip():
            label = f'showing {len(rows)} of {total} · "{search}"'
        else:
            label = f"showing {len(rows)} of {total}"
        self.query_one("#cand-count", Static).update(Text(label, style=pal.FAINT))
        self._wanted_id = None

    @staticmethod
    def _label(row: dict[str, Any]) -> Text:
        status = effective_status(row)
        text = Text()
        text.append("● ", style=status_color(status))
        score = row.get("score")
        text.append(
            f"{float(score):.2f}  " if isinstance(score, (int, float)) else "—  ",
            style=pal.ACCENT,
        )
        text.append(str(row.get("pattern") or "—"), style=pal.MUTED)
        names = [str(card.get("name")) for card in row.get("cards") or [] if card.get("name")]
        text.append("   ", style=pal.FAINT)
        text.append(" + ".join(names) or "—", style=pal.TEXT)
        return text

    def _show(self, hypothesis_id: int | None) -> None:
        detail = self.query_one("#candidate-detail", CandidateDetail)
        empty = self.query_one("#cand-detail-empty", EmptyState)
        if hypothesis_id is None:
            self._current_hypothesis = None
            detail.display = False
            empty.display = True
            return
        hypothesis = self.data.hypothesis(hypothesis_id)
        if hypothesis is None:
            self._current_hypothesis = None
            detail.display = False
            empty.display = True
            return
        self._current_hypothesis = hypothesis
        detail.display = True
        empty.display = False
        detail.show(hypothesis, self.data.adjudications(hypothesis_id))

    # -- actions ------------------------------------------------------------

    def action_cycle_verdict(self) -> None:
        listing = self.query_one("#cand-list", OptionList)
        option = listing.highlighted
        if option is None:
            self.notify("no hypothesis selected", severity="warning")
            return
        hypothesis_id = int(listing.get_option_at_index(option).id)
        hypothesis = self.data.hypothesis(hypothesis_id)
        if hypothesis is None:
            return
        current = effective_status(hypothesis)
        target = next_status(current)
        self.store.record_adjudication(
            hypothesis_id,
            target,
            reviewer="tui",
            notes="status cycled in TUI",
        )
        self._wanted_id = hypothesis_id
        self.notify(
            f"hypothesis #{hypothesis_id}: {current} → {target}",
            severity="information",
        )
        self.reload_data()

    def action_focus_search(self) -> None:
        self.query_one("#cand-search", Input).focus()

    def action_cursor_down(self) -> None:
        self.query_one("#cand-list", OptionList).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#cand-list", OptionList).action_cursor_up()

    # -- events -------------------------------------------------------------

    @on(Select.Changed, "#cand-pattern")
    def _on_pattern_changed(self, event: Select.Changed) -> None:
        event.stop()
        self._pattern_id = int(event.value) if isinstance(event.value, int) else ALL_PATTERNS
        self.reload_data()

    @on(Input.Changed, "#cand-search")
    def _on_search_changed(self, event: Input.Changed) -> None:
        event.stop()
        self._search = event.value
        if self._search_timer is not None:
            self._search_timer.stop()
        self._search_timer = self.set_timer(0.3, self.reload_data)

    @on(OptionList.OptionHighlighted, "#cand-list")
    def _on_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option.id is not None:
            self._show(int(event.option.id))

    @on(OptionList.OptionSelected, "#cand-list")
    def _on_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id is not None:
            self._show(int(event.option.id))

    @on(OptionList.OptionSelected, "#cand-card-links")
    def _on_card_link(self, event: OptionList.OptionSelected) -> None:
        # Card links in the detail pane jump to that card in the Corpus view.
        if event.option.id is None:
            return
        card_id = int(event.option.id)
        name = None
        hypothesis = self._current_hypothesis
        if hypothesis:
            for card in hypothesis.get("cards") or []:
                if int(card.get("id") or 0) == card_id:
                    name = str(card.get("name"))
                    break
        cast("ComboDiscoveryApp", self.app).open_card(card_id, name)

    def _highlighted_id(self) -> int | None:
        listing = self.query_one("#cand-list", OptionList)
        option = listing.highlighted
        if option is None:
            return None
        return int(listing.get_option_at_index(option).id)


__all__ = ["CandidatesView"]
