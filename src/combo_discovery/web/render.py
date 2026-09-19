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
import re
from collections.abc import Sequence
from typing import Any
from urllib.parse import quote

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

#: Friendlier labels for the decision types, used in the collapsed timeline.
DECISION_PLAIN = {
    1: "Priority",
    2: "Keep hand",
    3: "Tuck to bottom",
    4: "Declare attackers",
    5: "Declare blockers",
    6: "Assign combat damage",
    7: "Order blockers",
    8: "Choose cards",
    9: "Announce",
    10: "Arrange scry",
    11: "Choose targets",
    12: "Choose a mode",
    13: "Pay optional costs",
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
NODE_W = 96
NODE_H = 58
GAP = 74
MARGIN_X = 18
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


#: Player-facing names for the resources the tester tracks.
RESOURCE_LABELS = {
    "tokens": "Tokens",
    "permanents": "Permanents",
    "mana": "Mana",
    "life": "Life",
    "casts": "Spells cast",
    "spells_resolved": "Spells resolved",
    "graveyard": "Cards in graveyard",
    "library": "Cards in library",
    "hand": "Cards in hand",
    "damage": "Damage",
    "extra_phases": "Extra phases",
}

#: Shorter forms for the tiny labels beside graph edges.
RESOURCE_LABELS_SHORT = {
    "tokens": "Tokens",
    "permanents": "Permanents",
    "mana": "Mana",
    "life": "Life",
    "casts": "Spells cast",
    "spells_resolved": "Spells resolved",
    "graveyard": "Graveyard",
    "library": "Library",
    "hand": "Hand",
    "damage": "Damage",
    "extra_phases": "Phases",
}


def resource_label(key: str) -> str:
    return RESOURCE_LABELS.get(str(key), str(key).replace("_", " ").capitalize())


def resource_label_short(key: str) -> str:
    return RESOURCE_LABELS_SHORT.get(str(key), resource_label(key))


def join_words(items: Sequence[Any]) -> str:
    words = [str(item) for item in items if str(item)]
    if not words:
        return ""
    if len(words) == 1:
        return words[0]
    if len(words) == 2:
        return f"{words[0]} and {words[1]}"
    return ", ".join(words[:-1]) + f" and {words[-1]}"


def delta_label(delta: dict[str, int], limit: int = 4) -> str:
    """Full Magic-word resource labels, for the cycle caption."""
    if not delta:
        return "nothing increased"
    items = sorted(delta.items(), key=lambda item: (-abs(item[1]), item[0]))
    parts = [f"{resource_label(key)} {value:+d}" for key, value in items[:limit]]
    if len(items) > limit:
        parts.append(f"and {len(items) - limit} more")
    return ", ".join(parts)


def delta_lines(delta: dict[str, int], limit: int = 2) -> list[str]:
    """Compact, one-per-line edge labels (short names so each fits the gap)."""
    if not delta:
        return ["no change"]
    items = sorted(delta.items(), key=lambda item: (-abs(item[1]), item[0]))
    lines = [f"{resource_label_short(key)} {value:+d}" for key, value in items[:limit]]
    if len(items) > limit:
        lines.append(f"+{len(items) - limit} more")
    return lines


# -- plain-English translations ---------------------------------------------

#: Raw internal reason -> a sentence a player can read cold.
REASON_TEXT = (
    (
        "signature recurred but no tracked resource grew",
        "The board repeated, but nothing increased — so it isn't a loop.",
    ),
    (
        "recurrence only across turns",
        "It only repeated over several turns. That's normal play, not an infinite loop.",
    ),
    (
        "recurrence consumes mana each pass",
        "It uses up mana every time, so it can't keep going.",
    ),
    (
        "only policy-driven event counters grew",
        "Nothing on the board changed — the Combo Tester was just going in circles.",
    ),
    (
        "policy never matched an offered option",
        "The Combo Tester didn't know which move to make here, so this combo "
        "hasn't been tested yet.",
    ),
    (
        "need at least two post-baseline observations",
        "The game ended before the Combo Tester could see enough.",
    ),
    (
        "no signature recurrence",
        "The board never came back to the same state.",
    ),
)

#: Fallback translations for the evidence ``kind`` field.
REASON_KIND_TEXT = {
    "degenerate": "The same board appeared again.",
    "recurrence": "The board repeated.",
}


def plain_reason(raw: Any, kind: Any = None) -> str:
    """Translate an internal reason string; unknown text passes through unchanged."""
    text = str(raw or "")
    for needle, plain in REASON_TEXT:
        if needle in text:
            return plain
    if kind:
        for needle, plain in REASON_KIND_TEXT.items():
            if needle in str(kind):
                return plain
    return text


def verdict_headline(verdict: Any) -> str:
    return {
        "loops": "Loop found.",
        "no_loop": "No loop.",
        "refuted": "No loop.",
        "inconclusive": "Couldn't test this one.",
        "error": "Something went wrong testing this.",
    }.get(str(verdict or "").lower(), "Couldn't test this one.")


def verdict_word(verdict: Any) -> str:
    """Short plain result, for badges and the feed."""
    return {
        "loops": "Loop found",
        "no_loop": "No loop",
        "refuted": "No loop",
        "inconclusive": "Couldn't test",
        "error": "Error",
    }.get(str(verdict or "").lower(), "Couldn't test")


PHASE_LABELS = {
    "MAIN1": "Main",
    "MAIN2": "Main",
    "PRECOMBAT_MAIN": "Main",
    "POSTCOMBAT_MAIN": "Main",
    "BEGIN_COMBAT": "Combat",
    "COMBAT": "Combat",
    "DECLARE_ATTACKERS": "Combat",
    "DECLARE_BLOCKERS": "Combat",
    "COMBAT_DAMAGE": "Combat",
    "END_COMBAT": "Combat",
    "UPKEEP": "Upkeep",
    "DRAW": "Draw",
    "BEGINNING": "Start",
    "END": "End",
    "CLEANUP": "Cleanup",
}


def plain_phase(phase: Any) -> str:
    """Short, player-readable phase word (``COMBAT_DECLARE_ATTACKERS`` -> Combat)."""
    text = str(phase or "").strip()
    if not text:
        return ""
    upper = text.upper()
    if upper in PHASE_LABELS:
        return PHASE_LABELS[upper]
    if "MAIN" in upper:
        return "Main"
    if any(word in upper for word in ("COMBAT", "ATTACK", "BLOCK", "DAMAGE")):
        return "Combat"
    if "UPKEEP" in upper:
        return "Upkeep"
    if "DRAW" in upper:
        return "Draw"
    if "CLEANUP" in upper:
        return "Cleanup"
    if "END" in upper:
        return "End"
    if "BEGIN" in upper:
        return "Start"
    return text.replace("_", " ").title()


_MOTIF_RE = re.compile(r"\s*\([^)]*[~/:][^)]*\)")
_SYNTH_RE = re.compile(r"\s*\[synthetic\]")


def strip_motifs(text: Any) -> str:
    """Drop internal motif codes such as ``(COPIES_CREATURE~ETB_TRIGGER)``."""
    cleaned = _MOTIF_RE.sub("", str(text or ""))
    cleaned = re.sub(r"\s+([;,.]|$)", r"\1", cleaned)
    return " ".join(cleaned.split())


def plain_link(link: Any) -> str:
    """A diagnostics link in player words: ``A -> B [synthetic]`` -> ``A → B``."""
    return strip_motifs(_SYNTH_RE.sub("", str(link or ""))).replace("->", "→")


def scryfall_url(name: Any) -> str:
    return "https://scryfall.com/search?q=" + quote(f'"{name}"')


def card_link(name: Any, *, css: str = "card-link") -> str:
    return f'<a class="{esc(css)}" href="{esc(scryfall_url(name))}">{esc(name)}</a>'


def short_time(value: Any) -> str:
    text = str(value or "")
    if not text:
        return "—"
    return text.replace("T", " ")[:16]


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
    step_text = "Start" if baseline else f"Step {iteration}"
    if per_step and sample.get("turn") is not None:
        turn_text = f"Turn {sample['turn']}"
        phase_text = plain_phase(sample.get("phase")) or "—"
    else:
        turn_text = "—"
        phase_text = "—"
    title = f"{step_text} · {turn_text} · {phase_text}"
    return (
        f'<g class="{classes}" transform="translate({x}, {y})">'
        f"<title>{esc(title)}</title>"
        f'<rect width="{NODE_W}" height="{NODE_H}" rx="9"/>'
        f'<text class="n-iter" x="11" y="19">{esc(step_text)}</text>'
        f'<text class="n-meta" x="11" y="34">{esc(turn_text)}</text>'
        f'<text class="n-phase" x="11" y="48">{esc(phase_text)}</text>'
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
        return '<p class="empty">No Board Samples recorded for this run.</p>'

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
        growth_text = delta_label(grown, limit=4)
        board_text = f"back to the same board — {growth_text}"
        verdict_text = verdict_word(verdict)
        from_step = (
            "start"
            if samples[start]["iteration"] == 0
            else f"step {samples[start]['iteration']}"
        )
        title = (
            f"{verdict_text}: {board_text} "
            f"(from {from_step} to step {samples[end]['iteration']})"
        )
        if reason:
            title += f"; reason: {reason}"

        labels = [
            f'<text class="c-label" x="{(x_from + x_to) / 2:.1f}"'
            f' y="{apex_y - 10:.1f}" text-anchor="middle">{esc(verdict_text)}</text>',
            f'<text class="c-grown" x="{(x_from + x_to) / 2:.1f}"'
            f' y="{apex_y + 5:.1f}" text-anchor="middle">{esc(board_text)}</text>',
        ]
        if vclass in {"no_loop", "refuted"} and reason:
            short_reason = plain_reason(reason)
            if len(short_reason) > 96:
                short_reason = short_reason[:93] + "…"
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
        f' role="img" aria-label="board snapshots, one per step, '
        f'with {len(cycles)} repeated board(s)">'
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
        '<span><i class="ok"></i> loop found</span>'
        '<span><i class="warn"></i> board repeated, but no loop</span>'
        '<span><i></i> board repeated, couldn\'t decide</span>'
        '<span><i class="idle"></i> step where nothing changed</span>'
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
    samples: Sequence[dict[str, Any]],
    *,
    per_step: bool,
    include_signature: bool = False,
) -> str:
    if not samples:
        return '<p class="empty">No Board Samples recorded for this run.</p>'
    columns = _resource_columns(samples)
    head = "<tr><th>Step</th><th>Turn</th><th>Phase</th>"
    if include_signature:
        head += "<th>Signature</th>"
    head += "".join(
        f'<th class="num">{esc(resource_label(col))}</th>' for col in columns
    )
    head += "</tr>"
    rows: list[str] = []
    for index, sample in enumerate(samples):
        resources = sample.get("resources") or {}
        baseline = index == 0 and sample.get("iteration") == 0
        step = "Start" if baseline else esc(sample.get("iteration"))
        turn = "—" if sample.get("turn") is None else esc(sample["turn"])
        phase = esc(plain_phase(sample.get("phase")) or "—")
        if not per_step:
            turn = phase = "—"
        cells = "".join(
            f'<td class="num">{esc(resources.get(col, ""))}</td>' for col in columns
        )
        signature = (
            f'<td class="mono-cell faint">{esc(sample.get("signature") or "—")}</td>'
            if include_signature
            else ""
        )
        rows.append(
            f"<tr><td class=\"mono-cell{' accent' if baseline else ''}\">{step}</td>"
            f'<td class="mono-cell">{turn}</td>'
            f'<td class="mono-cell">{phase}</td>'
            f"{signature}{cells}</tr>"
        )
    return (
        '<div class="table-wrap"><table><thead>'
        f'{head}</thead><tbody>{"".join(rows)}</tbody></table></div>'
    )


def render_signatures(samples: Sequence[dict[str, Any]]) -> str:
    """Full fingerprints, for the collapsed technical section."""
    if not samples:
        return '<p class="empty">No Board Samples recorded.</p>'
    items: list[str] = []
    for index, sample in enumerate(samples):
        baseline = index == 0 and sample.get("iteration") == 0
        step = "Start" if baseline else f"Step {sample.get('iteration')}"
        items.append(
            f'<li><span class="sig-step">{esc(step)}</span>'
            f'<code>{esc(sample.get("signature") or "—")}</code></li>'
        )
    return f'<ul class="sig-list">{"".join(items)}</ul>'


# ---------------------------------------------------------------------------
# timeline
# ---------------------------------------------------------------------------


def format_answer(answer: Any) -> str:
    if isinstance(answer, (list, tuple)) and len(answer) == 2 and isinstance(answer[0], str):
        key = {"option_id": "option"}.get(answer[0], answer[0])
        return f"{key} {pretty_compact(answer[1])}"
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
        type_int = _as_int(decision_type)
        name = DECISION_PLAIN.get(type_int, DECISION_TYPES.get(type_int, f"Type {type_int}"))
        raw = DECISION_TYPES.get(type_int, type_int)
        items.append(
            f'<li><span class="t-id">#{esc(decision_id)}</span>'
            f'<span class="t-type" title="{esc(raw)}">{esc(name)}</span>'
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
        ("Moves", diagnostics.get("decisions")),
        ("Actions", diagnostics.get("executed_actions")),
        ("Repeats", diagnostics.get("iterations")),
        ("Plan hits", total(link_hits)),
        ("Plan misses", total(link_misses)),
        ("Triggers", total(trigger_hits)),
    ]
    cells = "".join(
        f'<div class="stat"><div class="k">{esc(label)}</div>'
        f'<div class="v{" warn" if label == "Plan misses" and _as_int(value) else ""}">'
        f'{esc(value if value is not None else "—")}</div></div>'
        for label, value in tiles
    )
    return f'<div class="stats">{cells}</div>'


def render_mechanism_vs_executed(
    hypothesis: dict[str, Any] | None,
    interactions: Sequence[dict[str, Any]],
    diagnostics: Any,
) -> str:
    """The proposed plan vs what actually happened, in player words."""
    diag = diagnostics
    if isinstance(diag, str):
        diag = parse_json(diag, None)
    diag = diag if isinstance(diag, dict) else {}

    # -- the plan -----------------------------------------------------------
    mechanism = ""
    if hypothesis:
        mechanism = strip_motifs(hypothesis.get("mechanism") or "")
    proposed: list[str] = []
    for row in interactions:
        source = row.get("source_name") or row.get("source_card_id")
        target = row.get("target_name") or row.get("target_card_id")
        proposed.append(
            f'<li><span class="link">{esc(source)} → {esc(target)}</span>'
            f'<div class="muted" style="font-size:12.5px;margin-top:2px">'
            "these two are supposed to set each other off</div></li>"
        )
    plan_html = f'<p class="mech">{esc(mechanism)}</p>' if mechanism else ""
    plan_html += (
        f'<ul class="plan-list">{"".join(proposed)}</ul>'
        if proposed
        else '<p class="empty">No proposed interactions recorded for these cards.</p>'
    )

    # -- what actually happened --------------------------------------------
    links = diag.get("links") or []
    link_hits = diag.get("link_hits") or []
    link_misses = diag.get("link_misses") or []
    executed: list[str] = []
    any_miss = False
    for index, link in enumerate(links):
        hits = _as_int(link_hits[index]) if index < len(link_hits) else 0
        misses = _as_int(link_misses[index]) if index < len(link_misses) else 0
        diverged = misses > 0 or hits == 0
        if diverged:
            any_miss = True
        if hits:
            outcome = f"happened {hits} time{'s' if hits != 1 else ''}"
        else:
            outcome = "never happened"
        executed.append(
            f'<li><span class="link">{esc(plain_link(link))}</span>'
            f'<div class="tags"><span class="badge {"warn" if diverged else "ok"}">'
            f"{esc(outcome)}</span></div></li>"
        )
    executed_html = (
        f'<ul class="plan-list">{"".join(executed)}</ul>'
        if executed
        else '<p class="empty">No record of what happened here.</p>'
    )

    if any_miss:
        chip = '<span class="badge warn">didn\'t go as planned</span>'
    elif links:
        chip = '<span class="badge ok">went as planned</span>'
    else:
        chip = '<span class="badge neutral">no plan recorded</span>'

    return (
        f'<div class="grid halves">'
        f'<div><div class="panel-title" style="margin-top:0">The plan'
        f" {chip}</div>{plan_html}</div>"
        f'<div><div class="panel-title" style="margin-top:0">What actually happened'
        f"</div>{_diagnostics_stats(diag)}{executed_html}</div>"
        f"</div>"
    )


def render_interaction_detail(interactions: Sequence[dict[str, Any]]) -> str:
    """Full interaction internals, for the collapsed technical section."""
    if not interactions:
        return '<p class="empty">No interactions recorded.</p>'
    items: list[str] = []
    for row in interactions:
        source = row.get("source_name") or row.get("source_card_id")
        target = row.get("target_name") or row.get("target_card_id")
        predicates = [
            str(item.get("predicate"))
            for item in (row.get("evidence") or [])
            if isinstance(item, dict) and item.get("predicate")
        ]
        tags = "".join(f'<span class="pill">{esc(p)}</span>' for p in predicates)
        items.append(
            f'<li><span class="link">{esc(source)} → {esc(target)}</span>'
            f'<div class="tags"><span class="pill">{esc(row.get("pattern") or "—")}</span>'
            f'<span class="pill">{esc(row.get("direction") or "—")}</span>'
            f'<span class="pill">score {esc(_fmt_number(row.get("score")))}</span>{tags}</div></li>'
        )
    return f'<ul class="plan-list">{"".join(items)}</ul>'


def _fmt_number(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "—"
    return f"{float(value):.3f}"


def render_verdict_badge(verdict: Any) -> str:
    key = str(verdict or "inconclusive").lower()
    return f'<span class="badge {esc(key)}">{esc(verdict_label(key))}</span>'


__all__ = [
    "CYCLE_MARKER",
    "DECISION_PLAIN",
    "DECISION_TYPES",
    "RESOURCE_LABELS",
    "RESOURCE_LABELS_SHORT",
    "build_samples",
    "card_link",
    "delta_label",
    "delta_lines",
    "esc",
    "find_cycles",
    "format_answer",
    "grew_between",
    "join_words",
    "plain_link",
    "plain_phase",
    "plain_reason",
    "pretty_json",
    "render_evidence",
    "render_graph",
    "render_interaction_detail",
    "render_legend",
    "render_mechanism_vs_executed",
    "render_observation_table",
    "render_signatures",
    "render_timeline",
    "render_verdict_badge",
    "resource_label",
    "resource_label_short",
    "scryfall_url",
    "short_time",
    "sig_short",
    "signed_delta",
    "strip_motifs",
    "verdict_class",
    "verdict_headline",
    "verdict_label",
    "verdict_word",
]
