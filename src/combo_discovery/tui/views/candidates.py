"""Candidates view — discovered combo candidates and their evidence."""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from .. import theme as pal
from ..data import StoreBinding, parse_json_list, short_id
from ..widgets import CandidateDetail, EmptyState

CANDIDATE_LIMIT = 400

_EMPTY_DB = (
    "No candidates recorded yet. Proposals appear here as they are written,\n"
    "each with its status (proposed / verified / refuted / inconclusive)."
)
_EMPTY_HINT = "candidate generation is a later layer"
_NO_SELECTION = (
    "Highlight a candidate to inspect its card set, evidence JSON and the\n"
    "adjudication trail."
)


class CandidatesView(Vertical):
    HINTS = [("j k", "move"), ("enter", "inspect"), ("r", "reload")]
    BINDINGS = [
        Binding("j", "cursor_down", "down", show=False),
        Binding("k", "cursor_up", "up", show=False),
        Binding("r", "reload_data", "reload", show=False),
    ]

    def __init__(self, data: StoreBinding, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.data = data

    def compose(self) -> ComposeResult:
        with Horizontal(id="cand-body"):
            with Vertical(id="cand-sidebar", classes="panel"):
                yield Static("Candidates", classes="panel-title")
                yield Static("", id="cand-count", classes="dim")
                yield OptionList(id="cand-list")
                yield EmptyState(
                    glyph="◇",
                    title="No candidates yet",
                    message=_EMPTY_DB,
                    hint=_EMPTY_HINT,
                    id="cand-empty",
                )
            with VerticalScroll(id="cand-detail", classes="panel"):
                yield CandidateDetail(id="candidate-detail")
                yield EmptyState(
                    glyph="◌",
                    title="No candidate selected",
                    message=_NO_SELECTION,
                    id="cand-detail-empty",
                )

    def on_mount(self) -> None:
        self.reload_data()

    def focus_primary(self) -> None:
        self.query_one("#cand-list", OptionList).focus()

    # -- data ---------------------------------------------------------------

    def reload_data(self) -> None:
        candidates = self.data.list_candidates(CANDIDATE_LIMIT)
        list_widget = self.query_one("#cand-list", OptionList)
        empty = self.query_one("#cand-empty", EmptyState)
        list_widget.clear_options()

        if candidates:
            list_widget.add_options(
                [Option(self._label(c), id=str(c["id"])) for c in candidates]
            )
            list_widget.display = True
            empty.display = False
        else:
            list_widget.display = False
            empty.display = True

        count = len(candidates)
        self.query_one("#cand-count", Static).update(
            Text(f"{count} candidate{'s' if count != 1 else ''}", style=pal.FAINT)
        )

        if candidates:
            list_widget.highlighted = 0
            self._show(candidates[0]["id"])
        else:
            self._show(None)

    @staticmethod
    def _label(candidate: dict[str, Any]) -> Text:
        names = [str(n) for n in parse_json_list(candidate.get("card_names_json")) if n]
        status = str(candidate.get("status") or "")
        color = {
            "verified": pal.OK,
            "refuted": pal.ERR,
            "inconclusive": pal.WARN,
            "proposed": pal.ACCENT,
        }.get(status, pal.FAINT)
        text = Text()
        # Status first so it never collides with a wrapped name list.
        text.append(f"{status}  ", style=f"bold {color}")
        text.append(" + ".join(names) if names else f"candidate #{candidate.get('id')}")
        return text

    def _show(self, candidate_id: int | None) -> None:
        detail = self.query_one("#candidate-detail", CandidateDetail)
        empty = self.query_one("#cand-detail-empty", EmptyState)
        if candidate_id is None:
            detail.display = False
            empty.display = True
            return
        candidate = self.data.candidate(candidate_id)
        if candidate is None:
            detail.display = False
            empty.display = True
            return
        detail.display = True
        empty.display = False
        detail.show(candidate, self.data.adjudications(candidate_id))

    # -- events / actions ---------------------------------------------------

    @on(OptionList.OptionHighlighted, "#cand-list")
    def _on_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option.id is not None:
            self._show(int(event.option.id))

    @on(OptionList.OptionSelected, "#cand-list")
    def _on_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id is not None:
            self._show(int(event.option.id))

    def action_cursor_down(self) -> None:
        self.query_one("#cand-list", OptionList).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#cand-list", OptionList).action_cursor_up()


__all__ = ["CandidatesView"]
