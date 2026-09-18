"""Full-page composition for the web console.

The primary view is written for a Magic player who knows what an infinite combo
is but nothing about this project. Internal vocabulary (signatures, state
hashes, policy versions, raw reasons) is kept, but demoted into a collapsed
"Technical details" section.

Server-side HTML is the source of truth; the only client-side behaviour is the
feed poll in :mod:`combo_discovery.web.assets`. Every page degrades gracefully
with Javascript disabled.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Sequence

from .assets import CSS, JS
from .db import parse_json
from .render import (
    build_samples,
    card_link,
    esc,
    find_cycles,
    grew_between,
    join_words,
    plain_reason,
    pretty_json,
    render_evidence,
    render_graph,
    render_interaction_detail,
    render_legend,
    render_mechanism_vs_executed,
    render_observation_table,
    render_signatures,
    render_timeline,
    resource_label,
    short_time,
    sig_short,
    strip_motifs,
    verdict_class,
    verdict_headline,
    verdict_word,
)

BRAND = "combo-discovery"
TAGLINE = "combo loop tester"

PAGES = (
    ("live", "/", "Live"),
    ("candidates", "/candidates", "Combos"),
)


def _nav(active: str) -> str:
    links = "".join(
        f'<a class="{"active" if key == active else ""}" href="{href}">{label}</a>'
        for key, href, label in PAGES
    )
    return f'<nav class="nav">{links}</nav>'


def _foot(db_path: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
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
        '<header class="topbar"><div class="topbar-inner">'
        f'<a class="brand" href="/"><span class="mark">◆</span> {BRAND}'
        f'<span class="sub">{esc(TAGLINE)}</span></a>'
        f"{_nav(active)}"
        "</div></header>\n"
        f'<main class="wrap">{body}{_foot(db_path)}</main>\n'
        f"<script>{JS}</script>\n"
        "</body>\n</html>\n"
    )


def _result_badge(verdict: Any) -> str:
    return (
        f'<span class="badge {esc(verdict_class(verdict))}">'
        f"{esc(verdict_word(verdict))}</span>"
    )


def _card_links(names: Sequence[Any], *, css: str = "card-link") -> str:
    return " + ".join(card_link(name, css=css) for name in names if name)


# ---------------------------------------------------------------------------
# feed
# ---------------------------------------------------------------------------


def feed_rows(results: Sequence[dict[str, Any]]) -> str:
    rows: list[str] = []
    for row in results:
        cards = _card_links(row.get("cards") or [])
        rows.append(
            f'<tr data-id="{esc(row.get("id"))}">'
            f'<td class="cards-cell">{cards}</td>'
            f'<td><a class="result-link" href="/run/{esc(row.get("run_id"))}">'
            f"{_result_badge(row.get('verdict'))}</a></td>"
            f'<td class="num">{esc(row.get("iterations"))}</td>'
            f'<td class="mono-cell faint">{esc(short_time(row.get("created_at")))}</td>'
            "</tr>"
        )
    return "".join(rows)


def page_feed(results: Sequence[dict[str, Any]], db_path: str) -> str:
    body_rows = feed_rows(results) or (
        '<tr><td colspan="4"><p class="empty">Nothing tested yet. '
        "Once the tester runs, each pair of cards it tries will show up here."
        "</p></td></tr>"
    )
    body = f"""
<section class="hero" style="--i:0">
  <h1>Combos being tested</h1>
  <p class="lede">Each row is a pair of cards the tester put on the battlefield
  to see whether they loop. Open a row to watch the board, step by step, and
  find out what the tester decided.</p>
</section>
<section class="panel" style="--i:1">
  <div class="panel-title">
    <span id="feed-live" class="live"><span class="dot"></span> live</span>
    <span class="count-tag" id="feed-count">{len(results)} results</span>
    <span class="count-tag faint" id="feed-updated"></span>
  </div>
  <div class="table-wrap">
    <table>
      <thead><tr>
        <th>Cards</th><th>Result</th><th class="num">Steps</th><th>When</th>
      </tr></thead>
      <tbody id="feed-body">{body_rows}</tbody>
    </table>
  </div>
</section>
"""
    return layout("Combos being tested", body, active="live", db_path=db_path)


# ---------------------------------------------------------------------------
# run page
# ---------------------------------------------------------------------------


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


def _raw_json(summary: str, value: Any) -> str:
    return (
        f'<details class="raw"><summary>{esc(summary)}</summary>'
        f"<pre>{esc(pretty_json(value))}</pre></details>"
    )


def _growth_names(grown: dict[str, int], limit: int = 4) -> str:
    ordered = sorted(grown.items(), key=lambda item: (-item[1], item[0]))
    return join_words([resource_label(key) for key, _ in ordered[:limit]])


def _cycle_note(
    cycles: Sequence[dict[str, Any]],
    verdict: Any,
    reason_plain: str,
    growth_names: str,
    *,
    in_progress: bool,
) -> str:
    if in_progress:
        return (
            '<p class="cycle-note">Still testing — the board snapshots below will '
            "keep filling in until it finishes.</p>"
        )
    if not cycles:
        return (
            '<p class="cycle-note"><strong>The board never repeated.</strong> '
            "Without a repeated board there is no loop to check.</p>"
        )
    cls = verdict_class(verdict)
    if cls == "loops":
        return (
            '<p class="cycle-note ok"><strong>A repeated board is only a clue.</strong> '
            f"The board came back to the same state while {esc(growth_names or 'resources')} "
            "grew. The result at the top of this page is what decides whether it is "
            "really an infinite combo.</p>"
        )
    if cls in {"no_loop", "refuted"}:
        return (
            '<p class="cycle-note warn"><strong>The board came back, but the '
            f"tester ruled it out.</strong> {esc(reason_plain)}</p>"
        )
    return (
        '<p class="cycle-note"><strong>The board came back, but the tester '
        f"couldn't be sure.</strong> {esc(reason_plain)}</p>"
    )


def _technical_run(
    *,
    run: dict[str, Any],
    result: dict[str, Any],
    samples: Sequence[dict[str, Any]],
    per_step: bool,
    evidence: Any,
    diagnostics: Any,
    interactions: Sequence[dict[str, Any]],
    raw_reason: str,
    kind: str,
    seeds_text: str,
    params_text: str,
    scenario_json: str,
) -> str:
    provenance = _meta(
        [
            ("engine commit", run.get("engine_commit") or "unversioned"),
            ("proto version", run.get("proto_version")),
            ("policy version", run.get("policy_version")),
            ("started at", _format_ts(run.get("started_at"))),
            ("seeds", seeds_text or "—"),
            ("params", params_text),
            ("candidate key", result.get("candidate_key") or "—"),
            ("result id", result.get("id")),
            ("iterations", result.get("iterations")),
            ("state hash before", sig_short(result.get("state_hash_before"), 16)),
            ("state hash after", sig_short(result.get("state_hash_after"), 16)),
            ("events", f"{result.get('event_start_seq') or 0}–{result.get('event_end_seq') or 0}"),
        ]
    )
    reason_block = (
        f'<p class="mono">reason: {esc(raw_reason or "—")}<br>'
        f'kind: {esc(kind or "—")}</p>'
    )
    return f"""
<details class="tech">
  <summary>Technical details</summary>
  <div class="tech-body">
    <div class="panel-title" style="margin-top:0">Provenance</div>
    {provenance}
    <div class="panel-title">Raw reason</div>
    {reason_block}
    <div class="panel-title">Board fingerprints (signatures)</div>
    {render_signatures(samples)}
    <div class="panel-title">Evidence</div>
    {render_evidence(evidence)}
    <div class="panel-title">Link diagnostics</div>
    {_raw_json("diagnostics_json", diagnostics)}
    <div class="panel-title">Proposed interactions (internal)</div>
    {render_interaction_detail(interactions)}
    <div class="panel-title">Scenario</div>
    {_raw_json("scenario_json", parse_json(scenario_json, scenario_json))}
    <div class="panel-title">Every move the tester made</div>
    {render_timeline(result.get("trace_json"))}
  </div>
</details>
"""


def page_run(
    *,
    run: dict[str, Any],
    result: dict[str, Any] | None,
    observations: Sequence[dict[str, Any]],
    evidence: Any,
    diagnostics: Any,
    hypothesis: dict[str, Any] | None,
    interactions: Sequence[dict[str, Any]],
    db_path: str,
) -> str:
    run_id = run.get("id")
    result = result or {}
    in_progress = not result
    verdict = result.get("verdict") if result else "inconclusive"
    kind = ""
    if isinstance(evidence, dict):
        kind = str(evidence.get("kind") or "")
    raw_reason = str(evidence.get("reason") or "") if isinstance(evidence, dict) else ""
    reason_plain = plain_reason(raw_reason, kind)

    card_names = (
        result.get("candidate_names")
        or (hypothesis.get("card_names") if hypothesis else [])
        or parse_json(result.get("card_names_json"), [])
        or []
    )
    title = " + ".join(card_names) if card_names else f"Run {run_id}"

    samples, per_step = build_samples(observations, result)
    cycles = find_cycles(samples)
    grown: dict[str, int] = {}
    if cycles:
        first = cycles[0]
        grown = grew_between(
            samples[first["start"]].get("resources") or {},
            samples[first["end"]].get("resources") or {},
        )
    growth_names = _growth_names(grown)

    # -- the five-second verdict -------------------------------------------
    if in_progress:
        headline = "Still testing."
        sentence = (
            "The tester is still working through this combo; the result will show "
            "up here when it finishes."
        )
    else:
        headline = verdict_headline(verdict)
        if verdict_class(verdict) == "loops":
            sentence = (
                "The board came back to the same state while "
                f"{growth_names or 'the same resources'} kept growing — so these "
                "cards can repeat forever."
            )
        elif verdict_class(verdict) in {"no_loop", "refuted"}:
            sentence = reason_plain or "The board never came back to the same state."
        elif verdict_class(verdict) == "inconclusive":
            sentence = reason_plain or "The tester couldn't be sure either way."
        else:
            sentence = raw_reason or "The tester hit a problem and stopped."

    seeds = run.get("seeds") or parse_json(run.get("seeds_json"), []) or []
    params = run.get("params") or parse_json(run.get("params_json"), {}) or {}
    seeds_text = ", ".join(str(s) for s in seeds) if isinstance(seeds, list) else str(seeds)
    params_text = pretty_json(params) if params else "—"

    replay = run.get("replay") or ""
    replay_html = (
        '<div class="replay">'
        f'<code id="replay-cmd">{esc(replay)}</code>'
        '<button class="copy" type="button" data-copy="replay-cmd">copy</button>'
        "</div>"
        if replay
        else '<p class="empty">This run has not finished yet, so there is nothing '
        "to replay.</p>"
    )

    fallback_note = "" if per_step else (
        '<p class="cycle-note"><strong>Older run.</strong> This one was recorded '
        "before the tester saved a snapshot at every step, so turn and phase are "
        "hidden and the graph is rebuilt from the saved summaries.</p>"
    )

    graph = render_graph(samples, verdict, raw_reason or kind, per_step=per_step)
    board_html = render_observation_table(samples, per_step=per_step)
    mech_html = render_mechanism_vs_executed(hypothesis, interactions, diagnostics)

    candidate_link = ""
    if (
        result.get("candidate_kind") == "pair"
        and str(result.get("candidate_key", "")).isdigit()
    ):
        candidate_link = (
            f'<a href="/candidate/{esc(result.get("candidate_key"))}">'
            "See this combo →</a>"
        )

    cards_line = _card_links(card_names) if card_names else esc(title)
    technical = _technical_run(
        run=run,
        result=result,
        samples=samples,
        per_step=per_step,
        evidence=evidence,
        diagnostics=diagnostics,
        interactions=interactions,
        raw_reason=raw_reason,
        kind=kind,
        seeds_text=seeds_text,
        params_text=params_text,
        scenario_json=run.get("scenario_json") or "{}",
    )

    body = f"""
<div class="crumbs"><a href="/">Live</a> / {esc(title)}</div>
<section class="hero verdict-hero {esc(verdict_class(verdict))}" style="--i:0">
  <h1 class="verdict-title">{esc(headline)}</h1>
  <p class="verdict-cards">{cards_line}</p>
  <p class="verdict-sentence">{esc(sentence)}</p>
  <p class="verdict-meta">
    {_result_badge(verdict)}
    {candidate_link}
  </p>
</section>
<section class="panel" style="--i:1">
  <div class="panel-title">What the tester did, step by step</div>
  <p class="graph-caption">Each dot is a snapshot of the board. If the board
  comes back to a state it was already in, the tester draws the loop.</p>
  {fallback_note}
  {graph}
  {render_legend()}
  {_cycle_note(cycles, verdict, reason_plain, growth_names, in_progress=in_progress)}
</section>
<section class="panel" style="--i:2">
  <div class="panel-title">The board, step by step</div>
  {board_html}
</section>
<section class="panel" style="--i:3">
  <div class="panel-title">What we think this combo does</div>
  {mech_html}
</section>
<section class="panel" style="--i:4">
  <div class="panel-title">Run this test again</div>
  {replay_html}
</section>
{technical}
"""
    return layout(title, body, active="", db_path=db_path)


# ---------------------------------------------------------------------------
# candidate page(s)
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
<div class="crumbs"><a href="/">Live</a> / <a href="/candidates">Combos</a> /
  {esc(title)}</div>
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
    return layout(title, body, active="candidates", db_path=db_path)


def page_candidates(
    *,
    candidates: Sequence[dict[str, Any]],
    patterns: Sequence[dict[str, Any]],
    pattern: str,
    search: str,
    db_path: str,
) -> str:
    options = ['<option value="">All combo types</option>']
    for row in patterns:
        selected = (
            " selected"
            if str(row.get("id")) == str(pattern) or row.get("name") == pattern
            else ""
        )
        options.append(
            f'<option value="{esc(row.get("id"))}"{selected}>{esc(row.get("name"))}</option>'
        )

    rows = "".join(
        f'<tr><td class="cards-cell">{_card_links(c.get("card_names") or [])}</td>'
        f'<td class="mono-cell faint">{esc(c.get("pattern") or "—")}</td>'
        f'<td class="num">{esc(_fmt_score(c.get("score")))}</td>'
        f'<td>{esc(c.get("status") or "—")}</td>'
        f'<td class="mono-cell"><a href="/candidate/{esc(c.get("id"))}">'
        "open →</a></td></tr>"
        for c in candidates
    ) or (
        '<tr><td colspan="5"><p class="empty">No combos match. They are built '
        "from the imported card collection.</p></td></tr>"
    )

    body = f"""
<section class="hero" style="--i:0">
  <h1>Combos we think might loop</h1>
  <p class="lede">Pairs of cards that look like they set each other off. Open
  one to read the idea and see how it tested.</p>
</section>
<section class="panel" style="--i:1">
  <form method="get" action="/candidates" class="filter-row"
        style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px">
    <select name="pattern" class="pill" style="padding:6px 8px">{''.join(options)}</select>
    <input type="search" name="q" value="{esc(search)}"
      placeholder="search by card…" class="pill"
      style="padding:6px 10px;min-width:220px;flex:1;background:var(--surface);color:var(--text);border:1px solid var(--border)">
    <button type="submit" class="copy">filter</button>
  </form>
  <div class="table-wrap"><table>
    <thead><tr><th>Cards</th><th>Combo type</th><th class="num">Match</th>
    <th>Status</th><th></th></tr></thead>
    <tbody>{rows}</tbody>
  </table></div>
  <p class="faint" style="font-size:12px">{len(candidates)} shown</p>
</section>
"""
    return layout("Combos", body, active="candidates", db_path=db_path)


__all__ = [
    "feed_rows",
    "layout",
    "page_candidate",
    "page_candidates",
    "page_feed",
    "page_run",
    "provenance_badge",
]
