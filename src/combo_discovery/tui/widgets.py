"""Reusable presentation widgets for the research console.

Small, dependency-light building blocks: a two-part header, a context
statusline, an empty-state card, and the card / candidate detail panes. All use
Rich renderables built from the palette in :mod:`combo_discovery.tui.theme`.
"""

from __future__ import annotations

from typing import Any

from rich.console import Group
from rich.syntax import Syntax
from rich.text import Text
from textual.containers import Vertical
from textual.widgets import Rule, Static

from . import theme as pal
from .data import pretty_json, short_id

# run / worker state -> accent color for the little status glyph
_STATE_COLORS = {
    "idle": pal.FAINT,
    "checking": pal.WARN,
    "running": pal.ACCENT,
    "ok": pal.OK,
    "finished": pal.OK,
    "error": pal.ERR,
    "cancelled": pal.WARN,
    "degraded": pal.WARN,
}


def state_glyph(state: str) -> Text:
    return Text("●", style=_STATE_COLORS.get(state, pal.FAINT))


class HeaderBar(Static):
    """Top bar: brand, active run id, and worker health."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__("", **kwargs)
        self._run_label = "no active run"
        self._run_state = "idle"
        self._worker_label = "no workers"
        self._worker_state = "idle"

    def update_header(
        self,
        *,
        run_label: str,
        run_state: str,
        worker_label: str,
        worker_state: str,
    ) -> None:
        self._run_label = run_label
        self._run_state = run_state
        self._worker_label = worker_label
        self._worker_state = worker_state
        self.refresh(layout=False)

    def render(self) -> Text:
        text = Text(no_wrap=True, overflow="ellipsis")
        text.append("◆ ", style=pal.ACCENT)
        text.append("combo-discovery", style=f"bold {pal.TEXT}")
        text.append("  ▏ ", style=pal.FAINT)
        text.append("run ", style=pal.FAINT)
        text.append(self._run_label, style=pal.MUTED)
        text.append("  ▏ ", style=pal.FAINT)
        text.append_text(state_glyph(self._run_state))
        text.append(" ")
        text.append(self._worker_label, style=pal.MUTED)
        return text


class StatusLine(Static):
    """Bottom bar: contextual key hints on the left, live stats on the right."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__("", **kwargs)
        self._hints: list[tuple[str, str]] = []
        self._stats = ""

    def set_content(self, hints: list[tuple[str, str]], stats: str) -> None:
        self._hints = hints
        self._stats = stats
        self.refresh(layout=False)

    def _render_hints(self) -> Text:
        text = Text(no_wrap=True, overflow="ellipsis")
        for index, (key, label) in enumerate(self._hints):
            if index:
                text.append("   ", style=pal.FAINT)
            text.append(key, style=f"bold {pal.ACCENT}")
            text.append(f" {label}", style=pal.FAINT)
        return text

    def render(self) -> Any:
        from rich.table import Table

        grid = Table.grid(expand=True)
        grid.add_column(justify="left", ratio=1)
        grid.add_column(justify="right", no_wrap=True)
        grid.add_row(self._render_hints(), Text(self._stats, style=pal.MUTED))
        return grid


class EmptyState(Static):
    """A quiet centred placeholder with a glyph, title, message and hint."""

    def __init__(
        self,
        *,
        glyph: str = "◇",
        title: str = "Nothing here",
        message: str = "",
        hint: str = "",
        **kwargs: Any,
    ) -> None:
        super().__init__("", **kwargs)
        self._glyph = glyph
        self._title = title
        self._message = message
        self._hint = hint
        self.add_class("empty-state")

    def set_content(
        self, *, glyph: str | None = None, title: str | None = None,
        message: str | None = None, hint: str | None = None,
    ) -> None:
        if glyph is not None:
            self._glyph = glyph
        if title is not None:
            self._title = title
        if message is not None:
            self._message = message
        if hint is not None:
            self._hint = hint
        self.refresh(layout=False)

    def render(self) -> Text:
        text = Text(justify="center")
        text.append(f"{self._glyph}\n", style=pal.ACCENT)
        text.append(self._title, style=f"bold {pal.TEXT}")
        if self._message:
            text.append(f"\n\n{self._message}", style=pal.MUTED)
        if self._hint:
            text.append(f"\n\n{self._hint}", style=pal.FAINT)
        return text


class CardDetail(Vertical):
    """Right-hand card inspector for the Corpus view."""

    def compose(self):
        yield Static("", id="card-name", classes="detail-name")
        yield Static("", id="card-type", classes="detail-type")
        yield Static("", id="card-mana", classes="detail-mana")
        yield Rule()
        yield Static("", id="card-oracle", classes="detail-oracle")
        yield Rule()
        yield Static("", id="card-effects", classes="detail-effects")
        yield Static("", id="card-extra", classes="detail-extra")

    def clear(self) -> None:
        for widget in self.query(Static):
            widget.update("")
        self.query_one("#card-name", Static).update("")

    def show(self, card: dict[str, Any] | None) -> None:
        self.clear()
        if card is None:
            return
        name = card.get("name") or "Unnamed card"
        self.query_one("#card-name", Static).update(Text(name, style=f"bold {pal.ACCENT}"))
        type_line = card.get("type_line") or ""
        self.query_one("#card-type", Static).update(Text(type_line, style=pal.MUTED))
        mana = card.get("mana")
        self.query_one("#card-mana", Static).update(
            Text(f"mana  {mana}" if mana else "mana  —", style=pal.ACCENT)
        )
        oracle = card.get("oracle") or "No oracle text recorded."
        self.query_one("#card-oracle", Static).update(Text(oracle, style=pal.TEXT))
        effects = card.get("effects")
        if effects is None:
            effects_line = "extracted effects  not yet imported"
        else:
            effects_line = f"extracted effects  {effects}"
        self.query_one("#card-effects", Static).update(Text(effects_line, style=pal.OK))
        extras = []
        if card.get("set_code"):
            extras.append(str(card["set_code"]))
        if card.get("rarity"):
            extras.append(str(card["rarity"]))
        extras.append(f"id {card.get('id', '—')}")
        self.query_one("#card-extra", Static).update(
            Text("  ·  ".join(extras), style=pal.FAINT)
        )


class CandidateDetail(Vertical):
    """Right-hand candidate inspector for the Candidates view."""

    def compose(self):
        yield Static("", id="cand-title", classes="detail-name")
        yield Static("", id="cand-meta", classes="detail-type")
        yield Rule()
        yield Static("Evidence", classes="section-label")
        yield Static("", id="cand-evidence")
        yield Static("Adjudications", classes="section-label")
        yield Static("", id="cand-verdicts")

    def clear(self) -> None:
        for widget in self.query(Static):
            widget.update("")

    def show(self, candidate: dict[str, Any] | None, verdicts: list[dict[str, Any]] | None = None) -> None:
        self.clear()
        if candidate is None:
            return
        from .data import parse_json_list

        names = [str(n) for n in parse_json_list(candidate.get("card_names_json"))]
        names = [n for n in names if n]
        title = "  +  ".join(names) if names else f"candidate #{candidate.get('id')}"
        self.query_one("#cand-title", Static).update(Text(title, style=f"bold {pal.ACCENT}"))

        status = str(candidate.get("status") or "unknown")
        status_color = {
            "verified": pal.OK,
            "refuted": pal.ERR,
            "inconclusive": pal.WARN,
            "proposed": pal.ACCENT,
        }.get(status, pal.MUTED)
        meta = Text()
        meta.append("status ", style=pal.FAINT)
        meta.append(status, style=f"bold {status_color}")
        meta.append("    run ", style=pal.FAINT)
        meta.append(short_id(candidate.get("run_id")), style=pal.MUTED)
        meta.append("    created ", style=pal.FAINT)
        meta.append(str(candidate.get("created_at") or "—"), style=pal.MUTED)
        self.query_one("#cand-meta", Static).update(meta)

        evidence = pretty_json(candidate.get("evidence_json"))
        if evidence:
            self.query_one("#cand-evidence", Static).update(
                Syntax(evidence, "json", theme="monokai", word_wrap=True, background_color="default")
            )
        else:
            self.query_one("#cand-evidence", Static).update(
                Text("No evidence recorded for this candidate yet.", style=pal.FAINT)
            )

        if verdicts:
            lines: list[Any] = []
            for entry in verdicts:
                line = Text()
                line.append(str(entry.get("verdict") or "—"), style=f"bold {pal.TEXT}")
                if entry.get("reviewer"):
                    line.append(f"  ·  {entry['reviewer']}", style=pal.MUTED)
                if entry.get("created_at"):
                    line.append(f"  {entry['created_at']}", style=pal.FAINT)
                if entry.get("notes"):
                    line.append(f"\n    {entry['notes']}", style=pal.MUTED)
                lines.append(line)
            self.query_one("#cand-verdicts", Static).update(Group(*lines))
        else:
            self.query_one("#cand-verdicts", Static).update(
                Text("No adjudications recorded.", style=pal.FAINT)
            )


__all__ = [
    "CardDetail",
    "CandidateDetail",
    "EmptyState",
    "HeaderBar",
    "StatusLine",
    "state_glyph",
]
