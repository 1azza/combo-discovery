"""Presentation helpers: the inline SVG state graph, tables and evidence panes.

The graph is the star of the console. One node per sampled state, laid out
sequentially left to right; recurrence is drawn as a curved back-edge from the
last occurrence of a signature to its first. Cycles are *never* shown alone:
the back-edge is coloured by the run's verdict and carries the verdict label,
and for ``no_loop``/``refuted`` the judge's reason is printed on the arc too.
"""

from __future__ import annotations

import html
import json
from typing import Any, Sequence

from .db import RESOURCE_COLUMNS, parse_json

#: Stable marker the test-suite (and curious readers) can grep for in the HTML.
CYCLE_MARKER = 'data-cycle="1"'

DECISION_TYPES = {
    1: "PRIORITY",
    2: "MULLIGAN_KEEP",
    3: "MULLIGAN_TUCK",
    4: "DECLARE_ATTACKERS",
    5: "DECLARE_BLOCKERS",
    6: "ASSIGN_COMBAT_DAMAGE",
    7: "ORDER_BLOCKERS",
    8: "CHOOSE_CARDS",
    9: "ANNOUNCE",
    10: "SCRY_ARRANGE",
    11: "CHOOSE_TARGETS",
    12: "CHOOSE_MODE",
    13: "OPTIONAL_COSTS",
}

VERDICT_LABELS = {
    "loops": "loops",
    "no_loop": "no loop",
    "refuted": "refuted",
    "inconclusive": "inconclusive",
    "error": "error",
}

#: Verdict -> CSS class. Anything unknown is treated as inconclusive (grey).
_VERDICT_CLASS = {
    "loops": "loops",
    "no_loop": "no_loop",
    "refuted": "refuted",
    "inconclusive": "inconclusive",
    "error": "error",
}

# Layout constants for the SVG (deliberately in user units, not screen px).
NODE_W = 92
NODE_H = 58
GAP = 58
MARGIN_X = 24
ARC_BASE = 34
ARC_STEP = 42

# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def verdict_class(verdict: Any) -> str:
    return _VERDICT_CLASS.get(str(verdict or "").lower(), "inconclusive")


def verdict_label(verdict: Any) -> str:
    return VERDICT_LABELS.get(str(verdict or "").lower(), str(verdict or "unknown"))


def sig_short(signature: Any, length: int = 8) -> str:
    text = str(signature or "")
    return text[:length] if text else "—"


def pretty_json(value: Any) -> str:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return value
    try:
        return json.dumps(value, indent=2, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def signed_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, int]:
    """Every key whose value changed, with a signed difference."""
    keys = set(before) | set(after)
    out: dict[str, int] = {}
    for key in keys:
        diff = _as_int(after.get(key)) - _as_int(before.get(key))
        if diff:
            out[str(key)] = diff
    return out


def grew_between(before: dict[str, Any], after: dict[str, Any]) -> dict[str, int]:
    """Only the keys that *grew* between two samples (positive differences)."""
    return {
        key: value
        for key, value in signed_delta(before, after).items()
        if value > 0
    }


#: Short display aliases so an edge label fits the gap between two nodes.
RESOURCE_ALIASES = {
    "permanents": "perm",
    "spells_resolved": "spells",
    "extra_phases": "phases",
    "graveyard": "grave",
    "library": "lib",
    "tokens": "tokens",
    "casts": "casts",
    "damage": "dmg",
    "mana": "mana",
    "life": "life",
    "hand": "hand",
}


def alias(key: str) -> str:
    return RESOURCE_ALIASES.get(key, key[:6])


def delta_label(delta: dict[str, int], limit: int = 3) -> str:
    if not delta:
        return "no growth"
    items = sorted(delta.items(), key=lambda item: (-abs(item[1]), item[0]))
    parts = [f"{alias(key)} {value:+d}" for key, value in items[:limit]]
    if len(items) > limit:
        parts.append(f"+{len(items) - limit} more")
    return " · ".join(parts)


def delta_lines(delta: dict[str, int], limit: int = 2) -> list[str]:
    """Compact, one-per-line edge labels (aliased so each fits the gap)."""
    if not delta:
        return ["no growth"]
    items = sorted(delta.items(), key=lambda item: (-abs(item[1]), item[0]))
    lines = [f"{alias(key)} {value:+d}" for key, value in items[:limit]]
    if len(items) > limit:
        lines.append(f"+{len(items) - limit} more")
    return lines


# ---------------------------------------------------------------------------
# samples
# ---------------------------------------------------------------------------


def build_samples(
    observations: Sequence[dict[str, Any]], result: dict[str, Any]
) -> tuple[list[dict[str, Any]], bool]:
    """Return ``(samples, per_step)``.

    ``per_step`` is True when the rows came from ``witness_observations`` (and
    therefore carry turn/phase); False when we fell back to the result's
    signature + cumulative resource-delta columns.
    """
    if observations:
        samples: list[dict[str, Any]] = []
        for index, row in enumerate(observations):
            resources = row.get("resources")
            if not isinstance(resources, dict):
                resources = parse_json(row.get("resources_json"), {}) or {}
            previous = samples[-1]["resources"] if samples else {}
            samples.append(
                {
                    "iteration": _as_int(row.get("iteration", index)),
                    "turn": row.get("turn"),
                    "phase": row.get("phase") or "",
                    "signature": row.get("signature") or "",
                    "resources": resources,
                    "delta": signed_delta(previous, resources),
                    "event_seq": row.get("event_seq"),
                }
            )
        return samples, True

    signatures = parse_json(result.get("signature_json"), []) or []
    deltas = parse_json(result.get("resource_deltas_json"), []) or []
    cumulative: list[dict[str, int]] = []
    running: dict[str, int] = {}
    for index, raw in enumerate(deltas):
        if not isinstance(raw, dict):
            raw = {}
        if index == 0:
            running = {str(k): _as_int(v) for k, v in raw.items()}
        else:
            running = dict(running)
            for key, value in raw.items():
                running[str(key)] = running.get(str(key), 0) + _as_int(value)
        cumulative.append(dict(running))

    samples = []
    for index, signature in enumerate(signatures):
        delta = {}
        if index < len(deltas) and isinstance(deltas[index], dict):
            delta = {str(k): _as_int(v) for k, v in deltas[index].items() if _as_int(v)}
        samples.append(
            {
                "iteration": index,
                "turn": None,
                "phase": "",
                "signature": str(signature or ""),
                "resources": cumulative[index] if index < len(cumulative) else {},
                "delta": delta,
                "event_seq": None,
            }
        )
    return samples, False


def find_cycles(samples: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group samples by signature; a signature seen twice makes a cycle."""
    groups: dict[str, list[int]] = {}
    for index, sample in enumerate(samples):
        signature = sample.get("signature") or ""
        if not signature:
            continue
        groups.setdefault(signature, []).append(index)
    cycles = [
        {
            "start": indices[0],
            "end": indices[-1],
            "signature": signature,
            "visits": len(indices),
        }
        for signature, indices in groups.items()
        if len(indices) >= 2
    ]
    cycles.sort(key=lambda cycle: (cycle["start"], cycle["end"]))
    return cycles


# ---------------------------------------------------------------------------
# the graph
# ---------------------------------------------------------------------------


def _node_svg(
    index: int, sample: dict[str, Any], per_step: bool, row_y: int
) -> str:
    x = MARGIN_X + index * (NODE_W + GAP)
    y = row_y
    iteration = sample["iteration"]
    baseline = index == 0 and iteration == 0
    classes = "node baseline" if baseline else "node"
    iter_text = "baseline" if baseline else f"it {iteration}"
    if per_step and sample.get("turn") is not None:
        phase = sample.get("phase") or ""
        meta = f"T{sample['turn']}"
        if phase:
            meta += f" · {phase}"
    else:
        meta = "no turn/phase"
    signature = sample.get("signature") or ""
    resources = sample.get("resources") or {}
    title = f"iteration {iteration} · signature {sig_short(signature, 16)} · {pretty_json(resources)}"
    return (
        f'<g class="{classes}" transform="translate({x}, {y})">'
        f"<title>{esc(title)}</title>"
        f'<rect width="{NODE_W}" height="{NODE_H}" rx="9"/>'
        f'<text class="n-iter" x="11" y="19">{esc(iter_text)}</text>'
        f'<text class="n-meta" x="11" y="34">{esc(meta)}</text>'
        f'<text class="n-sig" x="11" y="48">{esc(sig_short(signature))}</text>'
        "</g>"
    )


# The node row y depends on how many arc layers the cycles need; it is
# computed once per graph and passed to the node builder.


def render_graph(
    samples: Sequence[dict[str, Any]],
    verdict: Any,
    reason: str = "",
    *,
    per_step: bool = True,
) -> str:
    """Render the sequential state graph, cycles highlighted, as inline SVG."""
    if not samples:
        return '<p class="empty">No observations recorded for this run.</p>'

    vclass = verdict_class(verdict)
    cycles = find_cycles(samples)
    row_y = ARC_BASE + max(0, len(cycles) - 1) * ARC_STEP + 30
    node_markup = "".join(
        _node_svg(index, sample, per_step, row_y)
        for index, sample in enumerate(samples)
    )

    total_width = MARGIN_X * 2 + len(samples) * NODE_W + (len(samples) - 1) * GAP
    center_y = row_y + NODE_H / 2

    # -- straight edges between consecutive samples -------------------------
    edges: list[str] = []
    for index in range(len(samples) - 1):
        left = MARGIN_X + index * (NODE_W + GAP) + NODE_W
        right = MARGIN_X + (index + 1) * (NODE_W + GAP)
        delta = samples[index + 1].get("delta") or {}
        idle = not delta
        state = "edge-idle" if idle else "edge-progress"
        title = pretty_json(delta) if delta else "no resource change"
        lines = delta_lines(delta)
        # Stack the labels upward from just above the connector so short aliases
        # never spill into the neighbouring nodes.
        labels = "".join(
            f'<text class="e-label" x="{(left + right) / 2:.1f}"'
            f' y="{center_y - 8 - (len(lines) - 1 - position) * 10:.1f}"'
            f' text-anchor="middle">{esc(line)}</text>'
            for position, line in enumerate(lines)
        )
        edges.append(
            f'<g class="edge {state}">'
            f"<title>{esc(title)}</title>"
            f'<line x1="{left}" y1="{center_y:.1f}" x2="{right}" y2="{center_y:.1f}"/>'
            f"{labels}"
            "</g>"
        )

    # -- curved cycle back-edges --------------------------------------------
    cycle_markup: list[str] = []
    for layer, cycle in enumerate(cycles):
        start, end = cycle["start"], cycle["end"]
        x_from = MARGIN_X + end * (NODE_W + GAP) + NODE_W / 2
        x_to = MARGIN_X + start * (NODE_W + GAP) + NODE_W / 2
        arc_height = ARC_BASE + layer * ARC_STEP
        control_y = row_y - 2 * arc_height
        apex_y = row_y - arc_height
        grown = grew_between(
            samples[start].get("resources") or {},
            samples[end].get("resources") or {},
        )
        grown_text = delta_label(grown, limit=3)
        verdict_text = f"cycle · {verdict_label(verdict)}"
        title = (
            f"cycle: iteration {samples[start]['iteration']} -> "
            f"{samples[end]['iteration']}; {grown_text}; verdict {verdict_label(verdict)}"
        )
        if reason:
            title += f"; reason: {reason}"

        labels = [
            f'<text class="c-label" x="{(x_from + x_to) / 2:.1f}"'
            f' y="{apex_y - 10:.1f}" text-anchor="middle">{esc(verdict_text)}</text>',
            f'<text class="c-grown" x="{(x_from + x_to) / 2:.1f}"'
            f' y="{apex_y + 5:.1f}" text-anchor="middle">{esc(grown_text)}</text>',
        ]
        if vclass in {"no_loop", "refuted"} and reason:
            short_reason = reason if len(reason) <= 96 else reason[:93] + "…"
            labels.append(
                f'<text class="c-reason" x="{(x_from + x_to) / 2:.1f}"'
                f' y="{apex_y + 20:.1f}" text-anchor="middle">{esc(short_reason)}</text>'
            )

        cycle_markup.append(
            f'<g class="cycle-back-edge cycle-{vclass}" {CYCLE_MARKER}>'
            f"<title>{esc(title)}</title>"
            f'<path d="M {x_from:.1f},{row_y:.1f} '
            f"Q {(x_from + x_to) / 2:.1f},{control_y:.1f} {x_to:.1f},{row_y:.1f}\""
            f' pathLength="1" marker-end="url(#cycle-arrow)"/>'
            + "".join(labels)
            + "</g>"
        )

    svg = (
        f'<div class="graph-scroll">'
        f'<svg viewBox="0 0 {total_width} {row_y + NODE_H + 34}"'
        f' width="{total_width}" height="{row_y + NODE_H + 34}"'
        f' role="img" aria-label="witness state graph with {len(cycles)} cycle(s)">'
        "<defs>"
        '<marker id="cycle-arrow" viewBox="0 0 10 10" refX="7.5" refY="5"'
        ' markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        '<path d="M0,0 L10,5 L0,10 z" fill="context-stroke"/></marker>'
        "</defs>"
        f"{node_markup}{''.join(edges)}{''.join(cycle_markup)}"
        "</svg></div>"
    )
    return svg


def render_legend() -> str:
    return (
        '<div class="legend">'
        '<span><i class="ok"></i> cycle · loops</span>'
        '<span><i class="warn"></i> cycle · no loop / refuted (reason shown)</span>'
        '<span><i></i> cycle · inconclusive</span>'
        '<span><i class="idle"></i> step with no resource growth</span>'
        "</div>"
    )


# ---------------------------------------------------------------------------
# observation table
# ---------------------------------------------------------------------------


def _resource_columns(samples: Sequence[dict[str, Any]]) -> list[str]:
    present: set[str] = set()
    for sample in samples:
        present.update((sample.get("resources") or {}).keys())
    columns = list(RESOURCE_COLUMNS)
    extras = sorted(present - set(RESOURCE_COLUMNS))
    return columns + extras


def render_observation_table(
    samples: Sequence[dict[str, Any]], *, per_step: bool
) -> str:
    if not samples:
        return '<p class="empty">No observations recorded for this run.</p>'
    columns = _resource_columns(samples)
    head = (
        "<tr><th>iter</th><th>turn</th><th>phase</th><th>sig</th>"
        + "".join(f'<th class="num">{esc(col)}</th>' for col in columns)
        + "</tr>"
    )
    rows: list[str] = []
    for index, sample in enumerate(samples):
        resources = sample.get("resources") or {}
        baseline = index == 0 and sample.get("iteration") == 0
        iterator = "baseline" if baseline else esc(sample.get("iteration"))
        turn = "—" if sample.get("turn") is None else esc(sample["turn"])
        phase = esc(sample.get("phase") or "—")
        if not per_step:
            turn = phase = "—"
        cells = "".join(
            f'<td class="num">{esc(resources.get(col, ""))}</td>' for col in columns
        )
        rows.append(
            f"<tr><td class=\"mono-cell{' accent' if baseline else ''}\">{iterator}</td>"
            f'<td class="mono-cell">{turn}</td>'
            f'<td class="mono-cell">{phase}</td>'
            f'<td class="mono-cell faint">{esc(sig_short(sample.get("signature"), 10))}</td>'
            f"{cells}</tr>"
        )
    return f'<div class="table-wrap"><table><thead>{head}</thead><tbody>{"".join(rows)}</tbody></table></div>'


# ---------------------------------------------------------------------------
# timeline
# ---------------------------------------------------------------------------


def format_answer(answer: Any) -> str:
    if isinstance(answer, (list, tuple)) and len(answer) == 2 and isinstance(answer[0], str):
        return f"{answer[0]} {pretty_compact(answer[1])}"
    return pretty_compact(answer)


def pretty_compact(value: Any) -> str:
    try:
        return json.dumps(value, separators=(", ", ": "), sort_keys=False)
    except (TypeError, ValueError):
        return str(value)


def render_timeline(trace: Any) -> str:
    entries = parse_json(trace, []) or []
    if not isinstance(entries, list) or not entries:
        return '<p class="empty">No decision trace recorded for this run.</p>'
    items: list[str] = []
    for entry in entries:
        if not isinstance(entry, (list, tuple)) or len(entry) < 3:
            items.append(
                f'<li><span class="t-id">—</span><span class="t-type">unknown</span>'
                f'<span class="t-answer">{esc(pretty_compact(entry))}</span></li>'
            )
            continue
        decision_id, decision_type, answer = entry[0], entry[1], entry[2]
        name = DECISION_TYPES.get(_as_int(decision_type), f"TYPE_{decision_type}")
        items.append(
            f'<li><span class="t-id">#{esc(decision_id)}</span>'
            f'<span class="t-type">{esc(name)}</span>'
            f'<span class="t-answer">{esc(format_answer(answer))}</span></li>'
        )
    return f'<ul class="timeline">{"".join(items)}</ul>'


# ---------------------------------------------------------------------------
# evidence
# ---------------------------------------------------------------------------


def render_evidence(evidence: Any) -> str:
    data = evidence
    if isinstance(data, str):
        data = parse_json(data, None)
    if not isinstance(data, dict) or not data:
        return '<p class="empty">No evidence recorded (verdict rests on the trace alone).</p>'

    highlight_keys = ("reason", "kind", "pair", "grown")
    meta: list[str] = []
    for key in highlight_keys:
        if key in data and data[key] not in (None, "", [], {}):
            value = data[key]
            if isinstance(value, (dict, list)):
                value = pretty_compact(value)
            meta.append(f"<dt>{esc(key)}</dt><dd>{esc(value)}</dd>")
    for key in sorted(data):
        if key in highlight_keys or key == "diagnostics":
            continue
        value = data[key]
        if isinstance(value, (dict, list)):
            continue
        meta.append(f"<dt>{esc(key)}</dt><dd>{esc(value)}</dd>")

    body = f'<dl class="meta">{"".join(meta)}</dl>' if meta else ""
    raw = (
        "<details class=\"raw\"><summary>evidence_json</summary>"
        f"<pre>{esc(pretty_json(data))}</pre></details>"
    )
    return body + raw


# ---------------------------------------------------------------------------
# mechanism vs executed
# ---------------------------------------------------------------------------


def _diagnostics_stats(diagnostics: dict[str, Any]) -> str:
    link_hits = diagnostics.get("link_hits") or []
    link_misses = diagnostics.get("link_misses") or []
    trigger_hits = diagnostics.get("trigger_hits") or []

    def total(values: Any) -> int:
        if not isinstance(values, list):
            return 0
        return sum(_as_int(v) for v in values)

    tiles = [
        ("decisions", diagnostics.get("decisions")),
        ("executed actions", diagnostics.get("executed_actions")),
        ("iterations", diagnostics.get("iterations")),
        ("link hits", total(link_hits)),
        ("link misses", total(link_misses)),
        ("trigger hits", total(trigger_hits)),
    ]
    cells = "".join(
        f'<div class="stat"><div class="k">{esc(label)}</div>'
        f'<div class="v{" warn" if label == "link misses" and _as_int(value) else ""}">'
        f'{esc(value if value is not None else "—")}</div></div>'
        for label, value in tiles
    )
    return f'<div class="stats">{cells}</div>'


def render_mechanism_vs_executed(
    hypothesis: dict[str, Any] | None,
    interactions: Sequence[dict[str, Any]],
    diagnostics: Any,
) -> str:
    diag = diagnostics
    if isinstance(diag, str):
        diag = parse_json(diag, None)
    diag = diag if isinstance(diag, dict) else {}

    # -- proposed line ------------------------------------------------------
    mechanism = ""
    if hypothesis:
        mechanism = str(hypothesis.get("mechanism") or "").strip()
    proposed: list[str] = []
    for row in interactions:
        source = row.get("source_name") or row.get("source_card_id")
        target = row.get("target_name") or row.get("target_card_id")
        score = row.get("score")
        score_text = f"{float(score):.3f}" if isinstance(score, (int, float)) else "—"
        predicates = [
            str(item.get("predicate"))
            for item in (row.get("evidence") or [])
            if isinstance(item, dict) and item.get("predicate")
        ]
        tags = "".join(
            f'<span class="pill">{esc(p)}</span>' for p in predicates[:4]
        )
        proposed.append(
            f'<li><span class="link">{esc(source)} → {esc(target)}</span>'
            f'<div class="tags"><span class="pill">{esc(row.get("pattern") or "—")}</span>'
            f'<span class="pill">{esc(row.get("direction") or "—")}</span>'
            f'<span class="pill">score {esc(score_text)}</span>{tags}</div></li>'
        )
    mechanism_html = f'<p class="mech">{esc(mechanism)}</p>' if mechanism else ""
    proposed_html = (
        f'<ul class="plan-list">{"".join(proposed)}</ul>'
        if proposed
        else '<p class="empty">No proposed interactions recorded for these cards.</p>'
    )

    # -- executed line ------------------------------------------------------
    links = diag.get("links") or []
    link_hits = diag.get("link_hits") or []
    link_misses = diag.get("link_misses") or []
    executed: list[str] = []
    any_miss = False
    for index, link in enumerate(links):
        hits = _as_int(link_hits[index]) if index < len(link_hits) else 0
        misses = _as_int(link_misses[index]) if index < len(link_misses) else 0
        # A link that never fired is as much a divergence as one that missed.
        diverged = misses > 0 or hits == 0
        state = "ok" if not diverged else "warn"
        if diverged:
            any_miss = True
        executed.append(
            f'<li><span class="link">{esc(link)}</span>'
            f'<div class="tags"><span class="badge {state}">'
            f'{"hit" if hits else "never fired"} · {hits} hit / {misses} miss</span></div></li>'
        )
    executed_html = (
        f'<ul class="plan-list">{"".join(executed)}</ul>'
        if executed
        else '<p class="empty">No link execution diagnostics recorded.</p>'
    )

    if any_miss:
        chip = '<span class="badge warn">diverged · a proposed link missed</span>'
    elif links:
        chip = '<span class="badge ok">executed as planned</span>'
    else:
        chip = '<span class="badge neutral">no link plan</span>'

    return (
        f'<div class="grid halves">'
        f'<div><div class="panel-title" style="margin-top:0">Proposed line'
        f" {chip}</div>{mechanism_html}{proposed_html}</div>"
        f'<div><div class="panel-title" style="margin-top:0">Executed'
        f"</div>{_diagnostics_stats(diag)}{executed_html}</div>"
        f"</div>"
    )


def render_verdict_badge(verdict: Any) -> str:
    key = str(verdict or "inconclusive").lower()
    return f'<span class="badge {esc(key)}">{esc(verdict_label(key))}</span>'


__all__ = [
    "CYCLE_MARKER",
    "DECISION_TYPES",
    "RESOURCE_ALIASES",
    "alias",
    "build_samples",
    "delta_label",
    "delta_lines",
    "esc",
    "find_cycles",
    "format_answer",
    "grew_between",
    "pretty_json",
    "render_evidence",
    "render_graph",
    "render_legend",
    "render_mechanism_vs_executed",
    "render_observation_table",
    "render_timeline",
    "render_verdict_badge",
    "sig_short",
    "signed_delta",
    "verdict_class",
    "verdict_label",
]
