"""Full-page composition for the web console.

The product is The Gallery: a board of pairings being checked, written for a
Magic player. The Goldfish watches a single run: the play-by-play, the board
rebuilt from it, the pass counter and what grew, and the loop drawn as cards and
actions. Server-side HTML is the source of truth; both pages poll JSON and
advance on their own while a run streams.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from .assets import CSS, JS
from .gallery import (
    COLOUR_FILTERS,
    TYPE_FILTERS,
    render_board,
    render_tabs,
)
from .goldfish import render_play_by_play
from .render import (
    card_link,
    esc,
    render_interaction_detail,
    short_time,
    strip_motifs,
)

BRAND = "combo-discovery"
TAGLINE = "combo loop tester"

#: (key, href or None, title, subtitle). ``None`` href = not shipped yet.
NAV_ITEMS: tuple[tuple[str, str | None, str, str], ...] = (
    ("gallery", "/", "The Gallery", "combos being checked"),
    ("goldfish", "/goldfish", "The Goldfish", "watch a combo being tested"),
)


def _nav(active: str) -> str:
    parts: list[str] = []
    for key, href, title, subtitle in NAV_ITEMS:
        if href is None:
            parts.append(
                '<span class="nav-item nav-disabled" aria-disabled="true">'
                f'<span class="nav-title">{esc(title)}</span>'
                f'<span class="nav-sub">{esc(subtitle)}</span>'
                '<span class="nav-soon">coming soon</span></span>'
            )
        else:
            classes = "nav-item active" if key == active else "nav-item"
            parts.append(
                f'<a class="{classes}" href="{href}">'
                f'<span class="nav-title">{esc(title)}</span>'
                f'<span class="nav-sub">{esc(subtitle)}</span></a>'
            )
    return f'<nav class="nav" aria-label="Primary">{"".join(parts)}</nav>'


def _foot(db_path: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%SZ")
    return (
        '<footer class="foot">'
        f"<span>read-only · {esc(db_path)}</span>"
        f"<span>rendered {esc(stamp)}</span>"
        "</footer>"
    )


def layout(
    title: str,
    body: str,
    *,
    active: str = "",
    db_path: str = "",
) -> str:
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{esc(title)} · {BRAND}</title>\n"
        f"<style>{CSS}</style>\n"
        "</head>\n<body>\n"
        '<a class="skip" href="#main">Skip to content</a>'
        '<header class="topbar"><div class="topbar-inner">'
        f'<a class="brand" href="/"><span class="mark">◆</span> {BRAND}'
        f'<span class="sub">{esc(TAGLINE)}</span></a>'
        f"{_nav(active)}"
        "</div></header>\n"
        f'<main class="wrap" id="main">{body}{_foot(db_path)}</main>\n'
        f"<script>{JS}</script>\n"
        "</body>\n</html>\n"
    )


def _result_badge(verdict: Any) -> str:
    return (
        f'<span class="badge {esc(_verdict_class(verdict))}">'
        f"{esc(_verdict_word(verdict))}</span>"
    )


def _verdict_class(verdict: Any) -> str:
    mapping = {
        "loops": "loops",
        "no_loop": "no_loop",
        "refuted": "refuted",
        "inconclusive": "inconclusive",
        "error": "error",
    }
    return mapping.get(str(verdict or "").lower(), "inconclusive")


def _verdict_word(verdict: Any) -> str:
    mapping = {
        "loops": "Loop found",
        "no_loop": "No loop",
        "refuted": "No loop",
        "inconclusive": "Couldn't test",
        "error": "Error",
    }
    return mapping.get(str(verdict or "").lower(), "Couldn't test")


def _card_links(names: Sequence[Any], *, css: str = "card-link") -> str:
    return " + ".join(card_link(name, css=css) for name in names if name)


# ---------------------------------------------------------------------------
# gallery (home)
# ---------------------------------------------------------------------------


def _filter_select(
    name: str,
    label: str,
    options: Sequence[tuple[str, str]] | Sequence[str],
    current: str,
) -> str:
    items = [f'<option value="">{esc(label)}: any</option>']
    for option in options:
        value, text = option if isinstance(option, tuple) else (option, option)
        selected = " selected" if str(value) == str(current) else ""
        items.append(
            f'<option value="{esc(value)}"{selected}>{esc(text)}</option>'
        )
    return (
        f'<label class="filter"><span class="filter-label">{esc(label)}</span>'
        f'<select name="{esc(name)}">{"".join(items)}</select></label>'
    )


def page_gallery(payload: dict[str, Any], db_path: str) -> str:
    filters = payload.get("filters") or {}
    colour = (filters.get("colours") or [""])[0] if filters.get("colours") else ""
    tab = payload.get("tab") or "queued"
    form = (
        _filter_select("color", "Colour", COLOUR_FILTERS, colour)
        + _filter_select("type", "Card type", TYPE_FILTERS, filters.get("type") or "")
        + f'<input type="hidden" name="tab" value="{esc(tab)}">'
    )
    body = f"""
<section class="hero" style="--i:0">
  <h1>The Gallery</h1>
  <p class="lede">Every pairing the machine thinks might loop, sorted by how
  promising it looks. Watch them move from queued, to on the table, to a result
  while it checks them.</p>
</section>
<form class="filters" method="get" action="/" id="gallery-filters">
  {form}
  <noscript><button type="submit" class="copy">Filter</button></noscript>
</form>
<div class="board-status">
  <span id="gallery-live" class="live"><span class="dot"></span> live</span>
  <span class="count-tag" id="gallery-updated"></span>
</div>
{render_tabs(payload["tabs"])}
<section class="board-wrap" aria-label="Pairings by status">
  {render_board(payload["columns"], tab)}
</section>
<p class="board-note">One tile per pairing; open a tile's attempts to see every
test. Columns page 12 at a time and refresh on their own.</p>
<div class="lightbox" id="lightbox" hidden>
  <button type="button" class="lb-close" id="lightbox-close">Close</button>
  <img class="lb-img" id="lightbox-img" alt="" width="672" height="936">
</div>
"""
    return layout("The Gallery", body, active="gallery", db_path=db_path)


# ---------------------------------------------------------------------------
# goldfish: watch a combo being tested
# ---------------------------------------------------------------------------


def page_goldfish(payload: dict[str, Any], db_path: str) -> str:
    run = payload["run"]
    result = payload.get("result")
    names = payload.get("card_names") or []
    html = payload["html"]
    run_id = run.get("id")
    live = payload["live"]
    seq = payload["seq"]
    obs_cursor = payload["obs_cursor"]
    title = " + ".join(names) if names else f"Run {run_id}"
    head_cards = _card_links(names) if names else esc(title)

    rows = render_play_by_play(payload["events"], names)
    candidate_link = ""
    if (
        result
        and result.get("candidate_kind") == "pair"
        and str(result.get("candidate_key", "")).isdigit()
    ):
        candidate_link = (
            f'<a href="/candidate/{esc(result.get("candidate_key"))}">'
            "See this pairing →</a>"
        )

    summary_hidden = "" if html.get("summary") else " hidden"
    body = f"""
<section id="goldfish" data-run="{esc(run_id)}" data-seq="{esc(seq)}"
         data-obs="{esc(obs_cursor)}" data-live="{str(bool(live)).lower()}">
  <div class="crumbs"><a href="/">The Gallery</a> / \
<a href="/goldfish">The Goldfish</a> / {esc(title)}</div>
  <header class="gf-head">
    <h1 class="gf-pair">{head_cards}</h1>
    <div class="gf-status" id="gf-status">{html["status"]}</div>
    {f'<div class="gf-links">{candidate_link}</div>' if candidate_link else ""}
  </header>
  <div id="gf-verdict">{html["verdict"]}</div>
  <section class="panel gf-loop-panel" style="--i:1">
    <div class="panel-title">The loop so far</div>
    <div id="gf-loop">{html["loop"]}</div>
    <div id="gf-counters">{html["counters"]}</div>
  </section>
  <div class="gf-grid">
    <section class="panel gf-graph-panel" style="--i:3">
      <div class="panel-title">How the loop turns</div>
      <div id="gf-graph">{html["graph"]}</div>
    </section>
    <section class="panel gf-board-panel" style="--i:2">
      <div class="panel-title">The board, rebuilt</div>
      <p class="board-note"><strong>Not a board snapshot.</strong> There is no
      per-permanent record in the data, so this is put back together from the
      narration sentences. "Enters" adds a card, "leaves" takes it away (and we
      assume it went to the graveyard), tapped and counters are read from the
      words. Treat it as a close reading, not the engine's own view.</p>
      <div id="gf-board">{html["board"]}</div>
    </section>
  </div>
  <section class="panel gf-pb-panel" style="--i:4">
    <div class="panel-title">Play by play
      <span class="count-tag" id="gf-pb-count">{len(payload["events"])} moves</span>
    </div>
    <ol class="pb" id="pb-list">{rows}</ol>
    <button type="button" class="pb-jump" id="pb-jump" hidden>Jump to newest
      moves ↓</button>
  </section>
  <section class="panel gf-summary-panel" id="gf-summary-wrap"{summary_hidden}>
    <div class="panel-title">The run</div>
    <div id="gf-summary">{html["summary"]}</div>
  </section>
  <div class="lightbox" id="lightbox" hidden>
    <button type="button" class="lb-close" id="lightbox-close">Close</button>
    <img class="lb-img" id="lightbox-img" alt="" width="672" height="936">
  </div>
</section>
"""
    return layout(title, body, active="goldfish", db_path=db_path)


def page_goldfish_index(runs: Sequence[dict[str, Any]], db_path: str) -> str:
    """A list of runs, live ones first, so a reader can find a game happening."""
    live = [run for run in runs if run.get("live")]
    ended = [run for run in runs if not run.get("live")]

    def row(run: dict[str, Any]) -> str:
        cards = run.get("cards") or []
        label = " + ".join(cards) if cards else f"Run {run.get('id')}"
        status = (
            '<span class="chip chip-playing">live now</span>'
            if run.get("live")
            else f'<span class="chip chip-{esc(_verdict_class(run.get("verdict")))}">'
            f'{esc(_verdict_word(run.get("verdict")))}</span>'
        )
        passes = run.get("iterations")
        meta = (
            f"{passes} passes" if passes is not None else
            f"{run.get('observation_count') or 0} samples"
        )
        return (
            '<li class="run-row">'
            f'<div class="run-cards">{card_link(cards[0]) if cards else esc(label)}'
            + (f" + {card_link(cards[1])}" if len(cards) > 1 else "")
            + "</div>"
            f'<div class="run-meta">{status}<span class="faint">{esc(meta)}</span>'
            f'<time>{esc(short_time(run.get("started_at")))}</time></div>'
            f'<a class="run-open" href="/run/{esc(run.get("id"))}">watch →</a></li>'
        )

    live_html = "".join(row(run) for run in live) or (
        '<li class="empty">Nothing is being tested right now. Queued pairings '
        "start on their own.</li>"
    )
    ended_html = "".join(row(run) for run in ended[:40]) or (
        '<li class="empty">No finished runs yet.</li>'
    )
    body = f"""
<section class="hero" style="--i:0">
  <h1>The Goldfish</h1>
  <p class="lede">Watch a combo being played out in Magic. A live run shows its
  moves as they happen, the board it is building, and — the question this whole
  project asks — whether the board comes back around while something grows.</p>
</section>
<section class="panel" style="--i:1">
  <div class="panel-title">Happening now</div>
  <ul class="run-list">{live_html}</ul>
</section>
<section class="panel" style="--i:2">
  <div class="panel-title">Recently finished</div>
  <ul class="run-list">{ended_html}</ul>
</section>
"""
    return layout("The Goldfish", body, active="goldfish", db_path=db_path)


# ---------------------------------------------------------------------------
# candidate page
# ---------------------------------------------------------------------------


def provenance_badge(state: str) -> str:
    mapping = {
        "known": ("known", "In Commander Spellbook"),
        "contained": ("contained", "Part of a known combo"),
        "candidate": ("candidate", "Not in Commander Spellbook"),
    }
    key, label = mapping.get(state, mapping["candidate"])
    return f'<span class="badge {key}">{esc(label)}</span>'


def page_candidate(
    *,
    hypothesis: dict[str, Any],
    badge_state: str,
    interactions: Sequence[dict[str, Any]],
    results: Sequence[dict[str, Any]],
    cards: Sequence[dict[str, Any]],
    db_path: str,
) -> str:
    hypothesis_id = hypothesis.get("id")
    names = hypothesis.get("card_names") or []
    mechanism = strip_motifs(hypothesis.get("mechanism") or "")
    title = " + ".join(names) if names else f"Combo {hypothesis_id}"

    card_items = "".join(
        f'<li><span class="link">{card_link(card.get("name"))}</span> '
        f'<span class="faint mono">{esc(card.get("mana_cost") or "")} '
        f"{esc(card.get('type_line') or '')}</span>"
        + (
            f'<div class="muted" style="font-size:12.5px;margin-top:3px">'
            f'{esc(card.get("oracle_text"))}</div>'
            if card.get("oracle_text")
            else ""
        )
        + "</li>"
        for card in cards
    ) or '<li class="empty">Card text not found in the collection.</li>'

    result_rows = "".join(
        f'<tr><td><a class="result-link" href="/run/{esc(row.get("run_id"))}">'
        f"{_result_badge(row.get('verdict'))}</a></td>"
        f'<td class="num">{esc(row.get("iterations"))}</td>'
        f'<td class="mono-cell faint">{esc(short_time(row.get("created_at")))}</td>'
        f'<td><a href="/run/{esc(row.get("run_id"))}">watch it run →</a></td></tr>'
        for row in results
    ) or (
        '<tr><td colspan="4"><p class="empty">This combo hasn\'t been tested yet. '
        "When it is, the result will appear here.</p></td></tr>"
    )

    technical = f"""
<details class="tech">
  <summary>Technical details</summary>
  <div class="tech-body">
    <div class="panel-title" style="margin-top:0">What flagged it</div>
    {_meta([
        ("pattern", hypothesis.get("pattern") or "—"),
        ("pattern id", hypothesis.get("pattern_id")),
        ("match score", _fmt_score(hypothesis.get("score"))),
        ("status", hypothesis.get("status") or "—"),
        ("hypothesis id", hypothesis_id),
    ])}
    <p class="muted">{esc(hypothesis.get("pattern_description") or "")}</p>
    <div class="panel-title">Raw mechanism</div>
    <p class="mech">{esc(hypothesis.get("mechanism") or "—")}</p>
    <div class="panel-title">Interactions (internal)</div>
    {render_interaction_detail(interactions)}
  </div>
</details>
"""

    body = f"""
<div class="crumbs"><a href="/">The Gallery</a> / {esc(title)}</div>
<section class="hero" style="--i:0">
  <h1 class="verdict-cards">{_card_links(names) if names else esc(title)}</h1>
  <p class="verdict-meta">{provenance_badge(badge_state)}</p>
</section>
<section class="panel" style="--i:1">
  <div class="panel-title">The cards</div>
  <ul class="plan-list">{card_items}</ul>
</section>
<section class="panel" style="--i:2">
  <div class="panel-title">What we think this does</div>
  <p class="mech">{esc(mechanism or "No description recorded for this combo yet.")}</p>
</section>
<section class="panel" style="--i:3">
  <div class="panel-title">Has it looped?</div>
  <div class="table-wrap"><table>
    <thead><tr><th>Result</th><th class="num">Steps</th><th>When</th><th></th></tr></thead>
    <tbody>{result_rows}</tbody>
  </table></div>
</section>
{technical}
"""
    return layout(title, body, active="gallery", db_path=db_path)


def _format_ts(value: Any) -> str:
    if not value:
        return "—"
    return str(value)


def _fmt_score(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "—"
    return f"{float(value):.3f}"


def _meta(pairs: Sequence[tuple[str, Any]]) -> str:
    cells = "".join(
        f"<dt>{esc(key)}</dt><dd>{esc(value if value not in (None, '') else '—')}</dd>"
        for key, value in pairs
    )
    return f'<dl class="meta">{cells}</dl>'


__all__ = [
    "layout",
    "page_candidate",
    "page_gallery",
    "page_goldfish",
    "page_goldfish_index",
    "provenance_badge",
]
