"""Full-page composition for the web console.

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
    esc,
    find_cycles,
    pretty_json,
    render_evidence,
    render_graph,
    render_legend,
    render_mechanism_vs_executed,
    render_observation_table,
    render_timeline,
    render_verdict_badge,
    sig_short,
    verdict_class,
    verdict_label,
)

BRAND = "combo-discovery"
TAGLINE = "witness console"

PAGES = (
    ("live", "/", "Live"),
    ("candidates", "/candidates", "Candidates"),
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


# ---------------------------------------------------------------------------
# feed
# ---------------------------------------------------------------------------


def feed_rows(results: Sequence[dict[str, Any]]) -> str:
    rows: list[str] = []
    for row in results:
        cards = " ".join(
            f'<span class="pill">{esc(name)}</span>'
            for name in (row.get("cards") or [])
        )
        rows.append(
            f'<tr data-id="{esc(row.get("id"))}">'
            f'<td class="mono-cell">{cards}</td>'
            f"<td>{render_verdict_badge(row.get('verdict'))}</td>"
            f'<td class="num">{esc(row.get("iterations"))}</td>'
            f'<td class="num">{esc(row.get("observation_count") or 0)}</td>'
            f'<td class="mono-cell"><a href="/run/{esc(row.get("run_id"))}">'
            f'run {esc(row.get("run_id"))}</a> '
            f'<span class="faint">· result {esc(row.get("id"))}</span></td>'
            f'<td class="mono-cell faint">{esc(row.get("created_at") or "")}</td>'
            "</tr>"
        )
    return "".join(rows)


def page_feed(results: Sequence[dict[str, Any]], db_path: str) -> str:
    body_rows = feed_rows(results) or (
        '<tr><td colspan="6"><p class="empty">No witness results yet. '
        "Run <code>combo-witness --persist</code> to watch them accumulate "
        "here.</p></td></tr>"
    )
    body = f"""
<section class="hero" style="--i:0">
  <h1>Live witness feed</h1>
  <p class="lede">Every candidate the witness search has tested, newest first.
  A cycle in the state graph is <strong>necessary but not sufficient</strong> —
  the judge's verdict and its reason decide. Open a run to see the graph.</p>
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
        <th>cards</th><th>verdict</th><th class="num">iters</th>
        <th class="num">steps</th><th>run</th><th>recorded</th>
      </tr></thead>
      <tbody id="feed-body">{body_rows}</tbody>
    </table>
  </div>
</section>
"""
    return layout("Live feed", body, active="live", db_path=db_path)


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


def _cycle_note(
    cycles: Sequence[dict[str, Any]],
    verdict: Any,
    reason: str,
    *,
    in_progress: bool,
) -> str:
    if not cycles:
        return (
            '<p class="cycle-note"><strong>No cycle in this run.</strong> '
            f"Verdict <strong>{esc(verdict_label(verdict))}</strong>"
            + (f" — {esc(reason)}" if reason else "")
            + ".</p>"
        )
    if in_progress:
        return (
            f'<p class="cycle-note"><strong>{len(cycles)} cycle(s) so far.</strong> '
            "The run is still streaming observations; the verdict arrives when the "
            "result row is written.</p>"
        )
    cls = verdict_class(verdict)
    tail = f" — <em>{esc(reason)}</em>" if reason else ""
    return (
        f'<p class="cycle-note {esc(cls)}"><strong>{len(cycles)} cycle(s) detected; '
        f"verdict <span class=\"badge {esc(cls)}\">{esc(verdict_label(verdict))}</span>"
        f"</strong>{tail}. The cycle alone does not decide the combo.</p>"
    )


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
    reason = ""
    if isinstance(evidence, dict):
        reason = str(evidence.get("reason") or evidence.get("kind") or "")
    cards = result.get("cards") or []
    card_names = result.get("candidate_names") or []
    if not card_names:
        card_names = hypothesis.get("card_names") if hypothesis else []
    if not card_names:
        card_names = parse_json(result.get("card_names_json"), []) or []
    title = " + ".join(card_names) if card_names else f"Run {run_id}"

    samples, per_step = build_samples(observations, result)
    cycles = find_cycles(samples)

    seeds = run.get("seeds") or parse_json(run.get("seeds_json"), []) or []
    params = run.get("params") or parse_json(run.get("params_json"), {}) or {}
    seeds_text = ", ".join(str(s) for s in seeds) if isinstance(seeds, list) else str(seeds)
    params_text = pretty_json(params) if params else "—"

    provenance = _meta(
        [
            ("engine commit", run.get("engine_commit") or "unversioned"),
            ("proto version", run.get("proto_version")),
            ("policy version", run.get("policy_version")),
            ("started at", _format_ts(run.get("started_at"))),
            ("seeds", seeds_text or "—"),
            ("candidate", result.get("candidate_key") or "—"),
            ("result id", result.get("id")),
            ("iterations", result.get("iterations")),
        ]
    )
    replay = run.get("replay") or ""
    replay_html = (
        '<div class="replay">'
        f'<code id="replay-cmd">{esc(replay)}</code>'
        '<button class="copy" type="button" data-copy="replay-cmd">copy</button>'
        "</div>"
        if replay
        else ""
    )

    fallback_note = "" if per_step else (
        '<p class="cycle-note"><strong>Per-step data unavailable.</strong> '
        "This run predates per-step observations, so turn/phase are hidden and the "
        "graph is rebuilt from the recorded signatures and resource deltas. "
        "Re-run with <code>--persist</code> for per-step data.</p>"
    )

    graph = render_graph(samples, verdict, reason, per_step=per_step)
    observations_html = render_observation_table(samples, per_step=per_step)
    timeline_html = render_timeline(result.get("trace_json"))
    evidence_html = render_evidence(evidence)
    mech_html = render_mechanism_vs_executed(hypothesis, interactions, diagnostics)

    candidate_link = ""
    if result.get("candidate_kind") == "pair" and str(result.get("candidate_key", "")).isdigit():
        candidate_link = (
            f'<a href="/candidate/{esc(result.get("candidate_key"))}">'
            "open candidate →</a>"
        )

    body = f"""
<div class="crumbs"><a href="/">Live</a> / run {esc(run_id)}</div>
<section class="hero" style="--i:0">
  <h1 class="cards-title">{esc(title)}</h1>
  <p class="lede" style="margin-top:8px">
    {render_verdict_badge(verdict)}
    <span class="pill">candidate {esc(result.get("candidate_kind") or "—")}</span>
    <span class="pill">{esc(len(cycles))} cycle(s)</span>
    <span class="pill">{esc(len(samples))} sampled states</span>
    {candidate_link}
  </p>
</section>
<section class="panel" style="--i:1">
  <div class="panel-title">Provenance</div>
  {provenance}
  {replay_html}
</section>
<section class="panel" style="--i:2">
  <div class="panel-title">State graph</div>
  {fallback_note}
  {graph}
  {render_legend()}
  {_cycle_note(cycles, verdict, reason, in_progress=in_progress)}
</section>
<section class="panel" style="--i:3">
  <div class="panel-title">Observations</div>
  {observations_html}
</section>
<section class="panel" style="--i:4">
  <div class="panel-title">Decision timeline</div>
  {timeline_html}
</section>
<section class="panel" style="--i:5">
  <div class="panel-title">Mechanism vs executed</div>
  {mech_html}
</section>
<section class="panel" style="--i:6">
  <div class="panel-title">Verdict &amp; evidence</div>
  <p style="margin:0 0 14px">{render_verdict_badge(verdict)}
    <span class="muted">state before</span>
    <span class="mono">{esc(sig_short(result.get("state_hash_before"), 12))}</span>
    <span class="faint">→</span>
    <span class="muted">after</span>
    <span class="mono">{esc(sig_short(result.get("state_hash_after"), 12))}</span>
    <span class="pill">events {esc(result.get("event_start_seq") or 0)}–{esc(result.get("event_end_seq") or 0)}</span>
  </p>
  {evidence_html}
</section>
"""
    return layout(title, body, active="", db_path=db_path)


# ---------------------------------------------------------------------------
# candidate page(s)
# ---------------------------------------------------------------------------


def provenance_badge(state: str) -> str:
    mapping = {
        "known": ("known", "Spellbook-known · exact pair"),
        "contained": ("contained", "Spellbook-known · contained"),
        "candidate": ("candidate", "candidate · not in Spellbook"),
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
    title = " + ".join(names) if names else f"Candidate {hypothesis_id}"
    score = hypothesis.get("score")
    score_text = f"{float(score):.3f}" if isinstance(score, (int, float)) else "—"

    interaction_rows = "".join(
        f'<li><span class="link">{esc(row.get("source_name") or row.get("source_card_id"))}'
        f' → {esc(row.get("target_name") or row.get("target_card_id"))}</span>'
        f'<div class="tags"><span class="pill">{esc(row.get("pattern") or "—")}</span>'
        f'<span class="pill">{esc(row.get("direction") or "—")}</span>'
        f'<span class="pill">score {esc(_fmt_score(row.get("score")))}</span></div></li>'
        for row in interactions
    ) or '<li class="empty">No interactions recorded for this pair.</li>'

    card_items = "".join(
        f'<li><span class="link">{esc(card.get("name"))}</span> '
        f'<span class="faint mono">{esc(card.get("mana_cost") or "")} '
        f'{esc(card.get("type_line") or "")}</span>'
        + (
            f'<div class="muted" style="font-size:12.5px;margin-top:3px">{esc(card.get("oracle_text"))}</div>'
            if card.get("oracle_text")
            else ""
        )
        + "</li>"
        for card in cards
    ) or '<li class="empty">Card rows not found in the corpus.</li>'

    result_rows = "".join(
        f'<tr><td><a href="/run/{esc(row.get("run_id"))}">run {esc(row.get("run_id"))}</a>'
        f' <span class="faint mono">· {esc(row.get("id"))}</span></td>'
        f"<td>{render_verdict_badge(row.get('verdict'))}</td>"
        f'<td class="num">{esc(row.get("iterations"))}</td>'
        f'<td class="mono-cell faint">{esc(row.get("created_at") or "")}</td></tr>'
        for row in results
    ) or (
        '<tr><td colspan="4"><p class="empty">No witness results recorded yet. '
        "Run <code>combo-witness --candidate "
        f"{esc(hypothesis_id)} --persist</code>.</p></td></tr>"
    )

    body = f"""
<div class="crumbs"><a href="/">Live</a> / <a href="/candidates">Candidates</a> /
  {esc(hypothesis_id)}</div>
<section class="hero" style="--i:0">
  <h1 class="cards-title">{esc(title)}</h1>
  <p class="lede" style="margin-top:8px">
    {provenance_badge(badge_state)}
    <span class="pill">status {esc(hypothesis.get("status") or "—")}</span>
    <span class="pill">pattern {esc(hypothesis.get("pattern") or "—")}</span>
    <span class="pill">score {esc(score_text)}</span>
    <span class="pill">id {esc(hypothesis_id)}</span>
  </p>
</section>
<section class="panel" style="--i:1">
  <div class="panel-title">Pattern</div>
  <p class="mech"><strong class="accent">{esc(hypothesis.get("pattern") or "—")}</strong>
  <span class="muted">— {esc(hypothesis.get("pattern_description") or "")}</span></p>
</section>
<section class="panel" style="--i:2">
  <div class="panel-title">Proposed mechanism</div>
  <p class="mech">{esc(hypothesis.get("mechanism") or "No mechanism text recorded.")}</p>
  <div class="panel-title">Cards</div>
  <ul class="plan-list">{card_items}</ul>
  <div class="panel-title" style="margin-top:16px">Interactions</div>
  <ul class="plan-list">{interaction_rows}</ul>
</section>
<section class="panel" style="--i:3">
  <div class="panel-title">Witness results</div>
  <div class="table-wrap"><table>
    <thead><tr><th>run</th><th>verdict</th><th class="num">iters</th>
    <th>recorded</th></tr></thead>
    <tbody>{result_rows}</tbody>
  </table></div>
</section>
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
    options = ['<option value="">All patterns</option>']
    for row in patterns:
        selected = " selected" if str(row.get("id")) == str(pattern) or row.get("name") == pattern else ""
        options.append(
            f'<option value="{esc(row.get("id"))}"{selected}>{esc(row.get("name"))}</option>'
        )

    rows = "".join(
        f'<tr><td class="mono-cell">{" ".join(f"<span class=\"pill\">{esc(n)}</span>" for n in (c.get("card_names") or []))}</td>'
        f'<td><span class="pill">{esc(c.get("pattern") or "—")}</span></td>'
        f'<td class="num">{esc(_fmt_score(c.get("score")))}</td>'
        f'<td>{esc(c.get("status") or "—")}</td>'
        f'<td class="mono-cell"><a href="/candidate/{esc(c.get("id"))}">'
        f'#{esc(c.get("id"))} →</a></td></tr>'
        for c in candidates
    ) or (
        '<tr><td colspan="5"><p class="empty">No candidates match. '
        "Build the ontology with <code>combo-build-ontology</code> first.</p></td></tr>"
    )

    body = f"""
<section class="hero" style="--i:0">
  <h1>Candidates</h1>
  <p class="lede">Proposed combo hypotheses from the ontology, highest score
  first. Open one for its mechanism and the witness runs that tested it.</p>
</section>
<section class="panel" style="--i:1">
  <form method="get" action="/candidates" class="filter-row"
        style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px">
    <select name="pattern" class="pill" style="padding:6px 8px">{''.join(options)}</select>
    <input type="search" name="q" value="{esc(search)}"
      placeholder="search card or mechanism…" class="pill"
      style="padding:6px 10px;min-width:220px;flex:1;background:var(--surface);color:var(--text);border:1px solid var(--border)">
    <button type="submit" class="copy">filter</button>
  </form>
  <div class="table-wrap"><table>
    <thead><tr><th>cards</th><th>pattern</th><th class="num">score</th>
    <th>status</th><th></th></tr></thead>
    <tbody>{rows}</tbody>
  </table></div>
  <p class="faint" style="font-size:12px">{len(candidates)} shown</p>
</section>
"""
    return layout("Candidates", body, active="candidates", db_path=db_path)


__all__ = [
    "feed_rows",
    "layout",
    "page_candidate",
    "page_candidates",
    "page_feed",
    "page_run",
    "provenance_badge",
]
