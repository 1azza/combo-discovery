"""Card Lab — interactive ground-truth tuning for one card at a time.

Known (Commander Spellbook) on the left, our proposals on the right, with the
ground-truth verdict badge, the card/aggregate metrics, the false-positive and
miss diagnostics, and a Missed section for known combos we fail to propose.

All classification/metrics logic is the ``combo_discovery.evaluation`` library;
this view is the read-only presentation plus the ``r`` persistence path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from .. import theme as pal
from ...corpus.names import normalize_card_name
from ..data import (
    StoreBinding,
    add_card_to_scratch_deck,
    badge_color,
    effective_status,
    novelty_badge,
    pair_hash_for_names,
    pattern_module_name,
    scratch_deck_path,
    status_color,
)
from ...evaluation import (
    classify_pairs,
    diagnostics,
    metrics,
    novelty_status,
    persist_evaluation,
)
from ..widgets import EmptyState

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..app import ComboDiscoveryApp

DEFAULT_CARD = "Kiki-Jiki, Mirror Breaker"
KNOWN_LIMIT = 200
PROPOSAL_LIMIT = 200

_LIST_IDS = ("#lab-known-list", "#lab-proposed-list", "#lab-missed-list")


def _short_count(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "—"
    return f"{value / 1000:.1f}k" if value >= 1000 else str(int(value))


def _pct(value: Any) -> str:
    return f"{float(value):.3f}" if isinstance(value, (int, float)) else "—"


class CardLabView(Vertical):
    HINTS = [
        ("/", "pick"),
        ("f", "filter"),
        ("r", "evaluate"),
        ("d", "add"),
        ("g", "pattern"),
    ]
    BINDINGS = [
        Binding("slash", "pick_card", "pick", show=False),
        Binding("f", "toggle_filter", "filter", show=False),
        Binding("r", "run_evaluation", "evaluate", show=False),
        Binding("d", "add_to_deck", "add to deck", show=False),
        Binding("g", "pattern_module", "pattern module", show=False),
        Binding("j", "cursor_down", "down", show=False),
        Binding("k", "cursor_up", "up", show=False),
    ]

    def __init__(self, data: StoreBinding, store, decks_dir, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.data = data
        self.store = store
        self.decks_dir = decks_dir
        self._card: dict[str, Any] | None = None
        self._normalized = ""
        self._known_exact_only = False
        self._known: list[dict[str, Any]] = []
        self._proposals: list[tuple[dict[str, Any], str]] = []
        self._missed: dict[int, dict[str, Any]] = {}
        self._card_metrics: dict[str, Any] | None = None
        self._pattern_metrics: dict[str, dict[str, Any]] = {}
        self._global_aggregate: dict[str, Any] | None = None
        self._global_by_pattern: dict[str, dict[str, Any]] = {}
        self._diagnostics: tuple[list, list] = ([], [])
        self._eval_gen = 0
        self._global_started = False
        self._activated = False

    # -- layout -------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical(id="lab-head"):
            yield Static("◇ Card Lab", id="lab-brand")
            yield Static("", id="lab-card-header")
        with Horizontal(id="lab-body"):
            with Vertical(id="lab-known", classes="panel"):
                with Horizontal(classes="lab-col-head"):
                    yield Static("Known · Spellbook", classes="panel-title")
                    yield Static("", id="lab-known-filter", classes="dim")
                yield Static("", id="lab-known-count", classes="dim")
                yield OptionList(id="lab-known-list")
                yield EmptyState(
                    glyph="◇",
                    title="No known combos",
                    message="This card has no Commander Spellbook combos in the store.",
                    id="lab-known-empty",
                )
                with VerticalScroll(id="lab-known-detail"):
                    yield Static("", id="lab-known-detail-text")
            with Vertical(id="lab-proposed", classes="panel"):
                yield Static("Proposed · ours", classes="panel-title")
                yield Static("", id="lab-proposed-count", classes="dim")
                yield OptionList(id="lab-proposed-list")
                yield EmptyState(
                    glyph="◇",
                    title="No proposals",
                    message="Our ontology proposes nothing for this card yet.",
                    id="lab-proposed-empty",
                )
                with VerticalScroll(id="lab-proposed-detail"):
                    yield Static("", id="lab-proposed-detail-text")
        with Horizontal(id="lab-bottom"):
            with Vertical(id="lab-metrics", classes="panel"):
                yield Static("Metrics", classes="panel-title")
                yield Static("", id="lab-metrics-text")
            with Vertical(id="lab-missed", classes="panel"):
                yield Static("Missed", classes="panel-title")
                yield Static("", id="lab-missed-count", classes="dim")
                yield OptionList(id="lab-missed-list")
                yield Static("", id="lab-missed-detail", classes="dim")
            with Vertical(id="lab-diagnostics", classes="panel"):
                yield Static("Diagnostics", classes="panel-title")
                yield Static("", id="lab-diagnostics-text")

    def on_mount(self) -> None:
        matches = self.data.list_cards(DEFAULT_CARD, limit=5)
        card = next((c for c in matches if c["name"] == DEFAULT_CARD), None)
        if card is None and matches:
            card = matches[0]
        if card is not None:
            self._select_card(card)
        else:
            self._render_empty("card corpus is empty")

    def on_show(self) -> None:
        self.activate()

    def activate(self) -> None:
        """Start the (heavy) evaluation the first time the view is shown."""
        if self._activated:
            return
        self._activated = True
        if self._card is not None:
            self._eval_gen += 1
            self._run_card_eval(str(self._card.get("name") or ""), False, self._eval_gen)

    def focus_primary(self) -> None:
        self.query_one("#lab-known-list", OptionList).focus()

    # -- card selection -----------------------------------------------------

    def _select_card(self, card: dict[str, Any]) -> None:
        self._card = card
        self._normalized = str(
            card.get("normalized_name") or normalize_card_name(card.get("name"))
        )
        self._card_metrics = None
        self._pattern_metrics = {}
        self._diagnostics = ([], [])
        self._load_known()
        self._load_proposed()
        self._render_card_header()
        self._render_metrics()
        self._render_diagnostics()
        if self._activated:
            self._eval_gen += 1
            self._run_card_eval(str(card.get("name") or ""), False, self._eval_gen)

    def _render_card_header(self) -> None:
        widget = self.query_one("#lab-card-header", Static)
        card = self._card
        if card is None:
            widget.update(Text("no card selected — press / to pick one", style=pal.FAINT))
            return
        known = self.data.known_combo_total(self._normalized)
        exact = self.data.known_combo_total(self._normalized, exact_only=True)
        text = Text()
        text.append(str(card.get("name") or "—"), style=f"bold {pal.ACCENT}")
        if card.get("mana"):
            text.append(f"   {card['mana']}", style=pal.MUTED)
        if card.get("type_line"):
            text.append(f"   {card['type_line']}", style=pal.MUTED)
        text.append("   ·   ", style=pal.FAINT)
        text.append(f"{known} known combos", style=pal.TEXT)
        text.append(f" ({exact} exact 2-card)", style=pal.FAINT)
        text.append("   ·   ", style=pal.FAINT)
        text.append(f"{len(self._proposals)} proposals", style=pal.TEXT)
        widget.update(text)

    # -- known column -------------------------------------------------------

    def _load_known(self) -> None:
        listing = self.query_one("#lab-known-list", OptionList)
        empty = self.query_one("#lab-known-empty", EmptyState)
        detail = self.query_one("#lab-known-detail-text", Static)
        listing.clear_options()
        self.query_one("#lab-known-filter", Static).update(
            Text("exact 2-card only" if self._known_exact_only else "all combos",
                 style=pal.FAINT)
        )
        if self._card is None:
            listing.display = False
            empty.display = True
            detail.update("")
            self.query_one("#lab-known-count", Static).update("")
            return

        exact = self._known_exact_only
        total = self.data.known_combo_total(self._normalized, exact_only=exact)
        rows = self.data.known_combos_for_card(
            self._normalized, exact_only=exact, limit=KNOWN_LIMIT
        )
        self._known = rows
        self.query_one("#lab-known-count", Static).update(
            Text(f"showing {len(rows)} of {total}", style=pal.FAINT)
        )
        if not rows:
            listing.display = False
            empty.display = True
            detail.update("")
            return
        listing.display = True
        empty.display = False
        listing.add_options(
            Option(self._known_label(row), id=str(row["id"])) for row in rows
        )
        listing.highlighted = 0
        self._show_known(rows[0])

    def _known_label(self, row: dict[str, Any]) -> Text:
        partners = [str(p.get("name")) for p in row.get("partners") or []]
        text = Text()
        text.append(" + ".join(partners) or "—", style=pal.TEXT)
        text.append("   ", style=pal.FAINT)
        badge = str(row.get("badge") or "combo")
        text.append(badge, style=pal.OK if badge == "exact pair" else pal.ACCENT)
        text.append(f"   {_short_count(row.get('popularity'))}", style=pal.FAINT)
        return text

    def _show_known(self, row: dict[str, Any] | None) -> None:
        widget = self.query_one("#lab-known-detail-text", Static)
        if row is None:
            widget.update("")
            return
        text = Text()
        text.append(str(row.get("description") or "No description recorded.").strip(),
                    style=pal.TEXT)
        for label, key in (("easy", "easy_prereqs"), ("notable", "notable_prereqs")):
            value = str(row.get(key) or "").strip()
            if value:
                text.append(f"\n\n{label} prereqs  ", style=pal.FAINT)
                text.append(value, style=pal.MUTED)
        produces = row.get("produces") or []
        if produces:
            text.append("\n\nproduces  ", style=pal.FAINT)
            text.append("; ".join(str(p) for p in produces[:8]), style=pal.OK)
        legal = row.get("legalities") or {}
        vintage = "vintage legal" if legal.get("vintage") else "not vintage legal"
        footer = (
            f"{row.get('badge')}  ·  bracket {row.get('bracket_tag') or '—'}"
            f"  ·  source {row.get('source_id') or '—'} v{row.get('source_version') or '—'}"
            f"  ·  {vintage}  ·  popularity {_short_count(row.get('popularity'))}"
        )
        text.append(f"\n\n{footer}", style=pal.FAINT)
        widget.update(text)

    # -- proposed column ----------------------------------------------------

    def _load_proposed(self) -> None:
        listing = self.query_one("#lab-proposed-list", OptionList)
        empty = self.query_one("#lab-proposed-empty", EmptyState)
        detail = self.query_one("#lab-proposed-detail-text", Static)
        listing.clear_options()
        if self._card is None:
            listing.display = False
            empty.display = True
            detail.update("")
            self.query_one("#lab-proposed-count", Static).update("")
            self._proposals = []
            return

        hypotheses = self.data.hypotheses_for_card(
            int(self._card["id"]), limit=PROPOSAL_LIMIT
        )
        hashes: list[str | None] = []
        for hypothesis in hypotheses:
            cards = hypothesis.get("cards") or []
            hashes.append(
                pair_hash_for_names(cards[0]["name"], cards[1]["name"])
                if len(cards) == 2
                else None
            )
        real_hashes = [h for h in hashes if h]
        states = self.data.ground_truth_states(real_hashes)
        observed = self.data.observed_hashes(real_hashes)

        self._proposals = []
        for hypothesis, phash in zip(hypotheses, hashes):
            badge = (
                novelty_badge(states.get(phash, "unknown"), phash in observed)
                if phash
                else "candidate"
            )
            self._proposals.append((hypothesis, badge))

        self.query_one("#lab-proposed-count", Static).update(
            Text(f"{len(self._proposals)} proposals  ·  ground truth checked",
                 style=pal.FAINT)
        )
        if not self._proposals:
            listing.display = False
            empty.display = True
            detail.update("")
            return
        listing.display = True
        empty.display = False
        listing.add_options(
            Option(self._proposed_label(hypothesis, badge), id=str(hypothesis["id"]))
            for hypothesis, badge in self._proposals
        )
        listing.highlighted = 0
        self._show_proposed(*self._proposals[0])

    def _proposed_label(self, hypothesis: dict[str, Any], badge: str) -> Text:
        status = effective_status(hypothesis)
        text = Text()
        text.append("● ", style=status_color(status))
        text.append(f"{badge}  ", style=badge_color(badge))
        score = hypothesis.get("score")
        text.append(_pct(score) + "  ", style=pal.ACCENT)
        text.append(str(hypothesis.get("pattern") or "—"), style=pal.MUTED)
        partner = self._partner_label(hypothesis)
        text.append("   ", style=pal.FAINT)
        text.append(partner or "—", style=pal.TEXT)
        return text

    def _partner_label(self, hypothesis: dict[str, Any]) -> str:
        subject_id = int(self._card["id"]) if self._card else -1
        names = [
            str(c["name"]) for c in hypothesis.get("cards") or []
            if int(c.get("id") or -1) != subject_id
        ]
        return " + ".join(names)

    def _show_proposed(self, hypothesis: dict[str, Any], badge: str) -> None:
        widget = self.query_one("#lab-proposed-detail-text", Static)
        text = Text()
        text.append(str(hypothesis.get("mechanism") or "No mechanism text recorded.").strip(),
                    style=pal.TEXT)

        cards = hypothesis.get("cards") or []
        evidence = None
        if len(cards) == 2:
            evidence = self.data.interaction_evidence(
                int(cards[0]["id"]), int(cards[1]["id"]), str(hypothesis.get("pattern") or "")
            )
        if evidence:
            predicates = evidence.get("predicates") or []
            if predicates:
                text.append("\n\npredicates  ", style=pal.FAINT)
                text.append(" + ".join(predicates), style=pal.ACCENT)
            verification = evidence.get("verification") or {}
            flags = "  ".join(
                f"{name} {'yes' if verification.get(name) else 'no'}"
                for name in ("type_verified", "copy_verified", "ability_linked")
            )
            if flags:
                text.append("\nlink  ", style=pal.FAINT)
                text.append(flags, style=pal.TEXT)
                if verification.get("link_kind"):
                    text.append(f"  ({verification['link_kind']})", style=pal.MUTED)
            if verification.get("detail"):
                text.append(f"\n      {verification['detail']}", style=pal.FAINT)

        if badge in ("known", "contained"):
            truth = f"Tier A: {badge}"
            truth_style = badge_color(badge)
        elif len(cards) == 2:
            status = novelty_status(
                self.store, (cards[0]["name"], cards[1]["name"])
            )
            if status == "observed":
                truth, truth_style = "Tier B: observed (needs review)", pal.MUTED
            else:
                truth = "unverified candidate (needs a second source)"
                truth_style = pal.WARN
        else:
            truth, truth_style = "unverified candidate", pal.WARN
        text.append("\n\nground truth  ", style=pal.FAINT)
        text.append(truth, style=truth_style)
        text.append(f"\nstatus  {effective_status(hypothesis)}", style=pal.FAINT)
        widget.update(text)

    # -- evaluation (worker) ------------------------------------------------

    @work(thread=True, group="lab-card", exclusive=True)
    def _run_card_eval(self, card_name: str, persist: bool, generation: int) -> None:
        app = cast("ComboDiscoveryApp", self.app)
        store = app.store
        try:
            verdicts = classify_pairs(store, card=card_name)
            report = metrics(verdicts)
            diag = diagnostics(store, verdicts)
            normalized = normalize_card_name(card_name)
            card_metric = report.by_card.get(normalized)
            missed = [v for v in verdicts if v.verdict == "missed"]
            combo_ids = [v.known_combo_id for v in missed if v.known_combo_id is not None]
            descriptions = self.data.known_descriptions(combo_ids)
            names = {m.source_name for m in missed} | {m.target_name for m in missed}
            refs = self.data.card_refs_for_normalized(names)
            payload: list[dict[str, Any]] = []
            for miss in missed:
                partner_norm = (
                    miss.target_name if miss.source_name == normalized else miss.source_name
                )
                ref = refs.get(partner_norm) or refs.get(miss.source_name) or refs.get(miss.target_name)
                info = (
                    descriptions.get(int(miss.known_combo_id))
                    if miss.known_combo_id is not None
                    else {}
                ) or {}
                payload.append(
                    {
                        "partner": (ref or {}).get("name") or partner_norm,
                        "partner_id": (ref or {}).get("id"),
                        "combo_id": miss.known_combo_id,
                        "description": info.get("description", ""),
                        "produces": info.get("produces", []),
                    }
                )
            run_id = None
            if persist:
                known_import, ontology_import = self.data.latest_eval_import_ids()
                run_id = persist_evaluation(
                    store, report, verdicts, card_filter=card_name,
                    known_import_id=known_import, ontology_import_id=ontology_import,
                    params={"view": "card_lab"}, notes="card lab evaluation",
                )
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator
            app.call_from_thread(self._eval_failed, f"{type(exc).__name__}: {exc}")
            return
        app.call_from_thread(
            self._apply_eval, generation, card_name,
            card_metric.as_dict() if card_metric else None,
            {name: m.as_dict() for name, m in report.by_pattern.items()},
            payload, diag.false_positive_clusters, diag.miss_clusters, run_id,
        )

    def _apply_eval(
        self,
        generation: int,
        card_name: str,
        card_metric: dict[str, Any] | None,
        pattern_metrics: dict[str, dict[str, Any]],
        missed: list[dict[str, Any]],
        fp_clusters: list,
        miss_clusters: list,
        run_id: int | None,
    ) -> None:
        if generation != self._eval_gen:
            return
        self._card_metrics = card_metric
        self._pattern_metrics = pattern_metrics
        self._diagnostics = (fp_clusters, miss_clusters)
        self._render_metrics()
        self._render_diagnostics()
        self._render_missed(missed)
        if run_id is not None:
            self.notify(f"persisted evaluation run #{run_id}", severity="information")
        if not self._global_started:
            self._global_started = True
            self._load_global()

    def _eval_failed(self, message: str) -> None:
        self.query_one("#lab-metrics-text", Static).update(
            Text(f"evaluation failed: {message}", style=pal.ERR)
        )

    @work(thread=True, group="lab-global", exclusive=True)
    def _load_global(self) -> None:
        app = cast("ComboDiscoveryApp", self.app)
        try:
            verdicts = classify_pairs(app.store)
            report = metrics(verdicts)
        except Exception as exc:  # noqa: BLE001
            app.call_from_thread(
                self._global_failed, f"{type(exc).__name__}: {exc}"
            )
            return
        app.call_from_thread(
            self._apply_global,
            report.aggregate.as_dict(),
            {name: m.as_dict() for name, m in report.by_pattern.items()},
        )

    def _apply_global(
        self, aggregate: dict[str, Any], by_pattern: dict[str, dict[str, Any]]
    ) -> None:
        self._global_aggregate = aggregate
        self._global_by_pattern = by_pattern
        self._render_metrics()

    def _global_failed(self, message: str) -> None:
        if self._card_metrics is None:
            self.query_one("#lab-metrics-text", Static).update(
                Text(f"aggregate metrics unavailable: {message}", style=pal.WARN)
            )

    # -- panels -------------------------------------------------------------

    @staticmethod
    def _metric_line(label: str, metric: dict[str, Any] | None, style: str) -> Text:
        text = Text()
        text.append(f"{label:<10}", style=style)
        if not metric:
            text.append("—", style=pal.FAINT)
            return text
        text.append(
            f"P {_pct(metric.get('precision'))}  R {_pct(metric.get('recall'))}"
            f"  F1 {_pct(metric.get('f1'))}",
            style=pal.TEXT,
        )
        text.append(
            f"   known {metric.get('true_positives')}"
            f"  contained {metric.get('partials')}"
            f"  candidate {metric.get('false_positives')}"
            f"  missed {metric.get('missed')}",
            style=pal.FAINT,
        )
        at_k = metric.get("precision_at_k") or {}
        if at_k:
            best = sorted(at_k.items(), key=lambda kv: int(kv[0]))[:2]
            text.append(
                "   " + "  ".join(f"p@{k} {_pct(v)}" for k, v in best),
                style=pal.MUTED,
            )
        return text

    def _render_metrics(self) -> None:
        widget = self.query_one("#lab-metrics-text", Static)
        lines: list[Text] = []
        lines.append(self._metric_line("this card", self._card_metrics, pal.ACCENT))
        if self._global_aggregate is not None:
            lines.append(
                self._metric_line("aggregate", self._global_aggregate, pal.MUTED)
            )
        patterns = self._global_by_pattern or {}
        if patterns:
            lines.append(Text("by pattern (global)", style=pal.FAINT))
            for name in sorted(patterns):
                metric = patterns[name]
                line = Text()
                line.append(f"  {name:<22}", style=pal.MUTED)
                line.append(
                    f"P {_pct(metric.get('precision'))}  R {_pct(metric.get('recall'))}"
                    f"  F1 {_pct(metric.get('f1'))}",
                    style=pal.TEXT,
                )
                line.append(
                    f"   known {metric.get('true_positives')}"
                    f"  contained {metric.get('partials')}"
                    f"  candidate {metric.get('false_positives')}"
                    f"  missed {metric.get('missed')}",
                    style=pal.FAINT,
                )
                lines.append(line)
        elif self._global_aggregate is None and self._card_metrics is not None:
            lines.append(Text("aggregate metrics computing…", style=pal.FAINT))
        combined = lines[0]
        for line in lines[1:]:
            combined.append("\n")
            combined.append_text(line)
        widget.update(combined)

    def _render_diagnostics(self) -> None:
        widget = self.query_one("#lab-diagnostics-text", Static)
        fp_clusters, miss_clusters = self._diagnostics
        if not fp_clusters and not miss_clusters:
            widget.update(Text("no diagnostics yet — press r to evaluate", style=pal.FAINT))
            return
        text = Text()
        text.append("false positives (predicate sets)", style=pal.ERR)
        if fp_clusters:
            for label, count in fp_clusters[:4]:
                text.append(f"\n  {count:>6}  ", style=pal.FAINT)
                text.append(str(label), style=pal.MUTED)
        else:
            text.append("\n  none for this card", style=pal.FAINT)
        text.append("\nmisses (known produces/requires)", style=pal.WARN)
        if miss_clusters:
            for label, count in miss_clusters[:5]:
                text.append(f"\n  {count:>6}  ", style=pal.FAINT)
                text.append(str(label), style=pal.MUTED)
        else:
            text.append("\n  none for this card", style=pal.FAINT)
        widget.update(text)

    def _render_missed(self, missed: list[dict[str, Any]]) -> None:
        listing = self.query_one("#lab-missed-list", OptionList)
        listing.clear_options()
        self._missed = {}
        self.query_one("#lab-missed-count", Static).update(
            Text(f"{len(missed)} missed exact variants", style=pal.FAINT)
        )
        if not missed:
            self.query_one("#lab-missed-detail", Static).update(
                Text("nothing missed for this card", style=pal.FAINT)
            )
            return
        for row in missed:
            partner_id = row.get("partner_id")
            key = int(partner_id) if partner_id is not None else -int(row.get("combo_id") or 0)
            self._missed[key] = row
            listing.add_option(Option(Text(str(row.get("partner") or "—"), style=pal.TEXT),
                                      id=str(key)))
        listing.highlighted = 0
        self._show_missed(list(self._missed)[0])

    def _show_missed(self, key: int | None) -> None:
        widget = self.query_one("#lab-missed-detail", Static)
        row = self._missed.get(key) if key is not None else None
        if row is None:
            widget.update("")
            return
        text = Text()
        text.append(str(row.get("partner") or "—"), style=pal.TEXT)
        description = str(row.get("description") or "").strip().replace("\n", " ")
        if description:
            text.append(f"  —  {description}", style=pal.MUTED)
        produces = row.get("produces") or []
        if produces:
            text.append(f"  ·  produces: {'; '.join(str(p) for p in produces[:4])}",
                        style=pal.FAINT)
        widget.update(text)

    def _render_empty(self, message: str) -> None:
        for widget_id in ("#lab-known-empty", "#lab-proposed-empty"):
            self.query_one(widget_id, EmptyState).set_content(message=message)
            self.query_one(widget_id, EmptyState).display = True
        for listing_id in ("#lab-known-list", "#lab-proposed-list", "#lab-missed-list"):
            self.query_one(listing_id, OptionList).display = False
        self.query_one("#lab-card-header", Static).update(Text(message, style=pal.WARN))

    # -- actions ------------------------------------------------------------

    def action_pick_card(self) -> None:
        from ..screens import CardPickerScreen

        self.app.push_screen(
            CardPickerScreen(self.data), callback=self._on_card_picked
        )

    def _on_card_picked(self, card: dict[str, Any] | None) -> None:
        if card:
            self._select_card(card)

    def action_toggle_filter(self) -> None:
        self._known_exact_only = not self._known_exact_only
        self._load_known()

    def action_run_evaluation(self) -> None:
        if self._card is None:
            self.notify("pick a card first", severity="warning")
            return
        self.notify(f"evaluating {self._card.get('name')}…", severity="information")
        self._eval_gen += 1
        self._run_card_eval(str(self._card.get("name") or ""), True, self._eval_gen)

    def action_add_to_deck(self) -> None:
        if self._card is None:
            self.notify("pick a card first", severity="warning")
            return
        try:
            count, total = add_card_to_scratch_deck(
                scratch_deck_path(self.decks_dir), str(self._card.get("name") or "")
            )
        except ValueError:
            self.notify("card has no usable name", severity="error")
            return
        cast("ComboDiscoveryApp", self.app).refresh_decks()
        self.notify(
            f"added {self._card.get('name')} → research_scratch.dck (×{count}, {total})",
            severity="information",
        )

    def action_pattern_module(self) -> None:
        listing = self.query_one("#lab-proposed-list", OptionList)
        option = listing.highlighted
        if option is None or not self._proposals:
            self.notify("highlight a proposal first", severity="warning")
            return
        hypothesis_id = int(listing.get_option_at_index(option).id)
        match = next((h for h, _ in self._proposals if int(h["id"]) == hypothesis_id), None)
        if match is None:
            return
        module = pattern_module_name(str(match.get("pattern") or ""))
        self.notify(f"pattern module: {module}", title="Pattern", severity="information")

    def action_cursor_down(self) -> None:
        self._move_focused(1)

    def action_cursor_up(self) -> None:
        self._move_focused(-1)

    def _move_focused(self, delta: int) -> None:
        for widget_id in _LIST_IDS:
            listing = self.query_one(widget_id, OptionList)
            if listing.has_focus:
                if delta > 0:
                    listing.action_cursor_down()
                else:
                    listing.action_cursor_up()
                return
        listing = self.query_one("#lab-known-list", OptionList)
        if delta > 0:
            listing.action_cursor_down()
        else:
            listing.action_cursor_up()

    # -- list events --------------------------------------------------------

    @on(OptionList.OptionHighlighted, "#lab-known-list")
    def _on_known_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option.id is not None:
            self._show_known(self._known_by_id(int(event.option.id)))

    @on(OptionList.OptionSelected, "#lab-known-list")
    def _on_known_selected(self, event: OptionList.OptionSelected) -> None:
        row = self._known_by_id(int(event.option.id)) if event.option.id else None
        if row:
            self._jump_to_partner(row.get("partners") or [])

    @on(OptionList.OptionHighlighted, "#lab-proposed-list")
    def _on_proposed_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option.id is not None:
            match = self._proposal_by_id(int(event.option.id))
            if match:
                self._show_proposed(*match)

    @on(OptionList.OptionSelected, "#lab-proposed-list")
    def _on_proposed_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id is None:
            return
        match = self._proposal_by_id(int(event.option.id))
        if match:
            self._jump_hypothesis_partner(match[0])

    @on(OptionList.OptionHighlighted, "#lab-missed-list")
    def _on_missed_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option.id is not None:
            self._show_missed(int(event.option.id))

    @on(OptionList.OptionSelected, "#lab-missed-list")
    def _on_missed_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id is None:
            return
        row = self._missed.get(int(event.option.id))
        if row and row.get("partner_id") is not None:
            cast("ComboDiscoveryApp", self.app).open_card(
                int(row["partner_id"]), str(row.get("partner") or "")
            )

    # -- helpers ------------------------------------------------------------

    def _known_by_id(self, combo_id: int) -> dict[str, Any] | None:
        return next((row for row in self._known if int(row["id"]) == combo_id), None)

    def _proposal_by_id(self, hypothesis_id: int) -> tuple[dict[str, Any], str] | None:
        return next(
            ((h, badge) for h, badge in self._proposals if int(h["id"]) == hypothesis_id),
            None,
        )

    def _jump_to_partner(self, partners: list[dict[str, Any]]) -> None:
        partner = next((p for p in partners if p.get("id") is not None), None)
        if partner is None:
            self.notify("partner card is not in the corpus", severity="warning")
            return
        cast("ComboDiscoveryApp", self.app).open_card(
            int(partner["id"]), str(partner.get("name") or "")
        )

    def _jump_hypothesis_partner(self, hypothesis: dict[str, Any]) -> None:
        subject_id = int(self._card["id"]) if self._card else -1
        partner = next(
            (c for c in hypothesis.get("cards") or [] if int(c.get("id") or -1) != subject_id),
            None,
        )
        if partner is None:
            self.notify("no partner card to jump to", severity="warning")
            return
        cast("ComboDiscoveryApp", self.app).open_card(
            int(partner["id"]), str(partner.get("name") or "")
        )


__all__ = ["CardLabView"]
