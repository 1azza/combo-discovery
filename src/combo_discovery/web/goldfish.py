"""The Goldfish: watch a combo being tested, in Magic.

The Gallery answers "which pairings might loop". This screen answers the next
question while a run is still in flight: *does the board come back around while
something grows?*

Everything a reader sees here is reconstructed from the two live streams:

* ``witness_events`` — one sentence per game event, in order. This is the spine:
  the play-by-play, and the raw material the board is rebuilt from.
* ``witness_observations`` — a handful of samples of the tracked resources. This
  is where the growth story comes from.

There is deliberately **no board snapshot** in the data, so the board on screen
is a *reconstruction* from the narration sentences, not a replay of the engine's
internal state. The heuristic is documented beside the board and in
:func:`reconstruct_board`; the honest limits are named there too.

The loop graph is the element the rebuild exists for: nodes are cards, the
engine sits at the hub, and every edge is the actual action in Magic words —
"copies Deceiver Exarch" → "untaps Kiki-Jiki, Mirror Breaker" → back to the hub.
It is derived from the *last pass* of narration, because a loop's signature is
that it repeats: the last pass is a clean sample of one turn of the wheel.
"""

from __future__ import annotations

import math
import re
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .db import ReadOnlyStore, parse_json
from .gallery import BASIC_LANDS
from .images import image_url
from .render import (
    RESOURCE_LABELS,
    card_link,
    esc,
    grew_between,
    join_words,
    plain_phase,
    plain_reason,
    short_time,
    verdict_class,
    verdict_headline,
    verdict_word,
)

#: A run with no result row is "live" until it has been quiet this long; a
#: process killed mid-run leaves no result, and a forever-spinning "live" dot
#: would be a lie.
LIVE_GRACE_SECONDS = 90

#: Board categories, in the order a player reads a battlefield.
TYPE_ORDER = ("Creature", "Planeswalker", "Artifact", "Enchantment", "Land", "Other")

TYPE_LABELS = {
    "Creature": "Creatures",
    "Planeswalker": "Planeswalkers",
    "Artifact": "Artifacts",
    "Enchantment": "Enchantments",
    "Land": "Lands",
    "Other": "Other permanents",
}

#: Narration ``kind`` -> a plain word for the play-by-play gutter.
KIND_WORDS = {
    "play_land": "land",
    "cast": "cast",
    "activate": "activate",
    "trigger": "trigger",
    "copy": "copy",
    "untap": "untap",
    "token": "token",
    "iteration": "pass",
    "verdict": "verdict",
    "other": "board",
}

#: Card names that name a token instead of a spell: used only to label the
#: growth on the graph, never to claim a board fact the narration didn't state.
_ENTER_SUFFIX = "enters the battlefield"
_LEAVE_SUFFIX = "leaves the battlefield"
_TAPPED_SUFFIX = "becomes tapped"
_COUNTERS_SUFFIX = "gets counters"
_MOVES_SUFFIX = " moves"

#: Resource key -> (singular, plural) for the growth sentence. Deliberately not
#: "Tokens +1": the counter row shows a number, this sentence says how much it
#: climbed.
GROWTH_WORDS = {
    "tokens": ("token", "tokens"),
    "permanents": ("permanent", "permanents"),
    "mana": ("mana", "mana"),
    "life": ("life", "life"),
    "casts": ("spell cast", "spells cast"),
    "spells_resolved": ("spell resolved", "spells resolved"),
    "graveyard": ("card in the graveyard", "cards in the graveyard"),
    "library": ("card in the library", "cards in the library"),
    "hand": ("card in hand", "cards in hand"),
    "damage": ("damage", "damage"),
    "extra_phases": ("extra phase", "extra phases"),
}

#: The order the tracked counters are shown, whatever the observation carries.
COUNTER_ORDER = (
    "tokens",
    "permanents",
    "mana",
    "life",
    "casts",
    "spells_resolved",
    "damage",
    "extra_phases",
    "graveyard",
    "library",
    "hand",
)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_ts(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _resource_label(key: str) -> str:
    return RESOURCE_LABELS.get(str(key), str(key).replace("_", " ").capitalize())


def _plural(count: int, singular: str, plural: str) -> str:
    return singular if count == 1 else plural


def _growth_phrase(grown: dict[str, int], limit: int = 3) -> str:
    """Turn positive resource deltas into "2 more tokens, 1 more permanent"."""
    if not grown:
        return "nothing"
    items = sorted(grown.items(), key=lambda item: (-abs(item[1]), item[0]))
    parts: list[str] = []
    for key, value in items[:limit]:
        singular, plural = GROWTH_WORDS.get(
            key, (_resource_label(key), _resource_label(key))
        )
        word = singular if value == 1 else plural
        parts.append(f"{value} more {word}")
    phrase = join_words(parts)
    if len(items) > limit:
        phrase += f", plus {len(items) - limit} more"
    return phrase


#: Only these resources are *game state*; ``casts``/``spells_resolved`` climb
#: because the tester keeps acting, not because the board grew, so they never
#: lead the growth sentence.
_GAME_STATE_KEYS = ("mana", "tokens", "life", "damage", "permanents", "extra_phases")


def _relevant_growth(grown: dict[str, int]) -> dict[str, int]:
    """The grown resources a player would call board growth."""
    if not grown:
        return {}
    relevant = {key: value for key, value in grown.items() if key in _GAME_STATE_KEYS}
    return relevant or dict(grown)


def _linkify(text: Any, names: list[str]) -> str:
    """Escape ``text`` and link the card names that appear inside it."""
    raw = str(text or "")
    wanted = sorted({str(n) for n in names if n}, key=len, reverse=True)
    if not wanted:
        return esc(raw)
    pattern = re.compile("|".join(re.escape(name) for name in wanted))
    out: list[str] = []
    position = 0
    for match in pattern.finditer(raw):
        out.append(esc(raw[position : match.start()]))
        out.append(card_link(match.group(0)))
        position = match.end()
    out.append(esc(raw[position:]))
    return "".join(out)


def _run_card_names(run: dict[str, Any], result: dict[str, Any] | None) -> list[str]:
    for source in (result or {}, run):
        names = parse_json(source.get("card_names_json"), []) or source.get("cards") or []
        if names:
            return [str(n) for n in names if n]
    return []


def run_card_names(run: dict[str, Any], result: dict[str, Any] | None) -> list[str]:
    """Public alias for the pair identity of a run (works before a result)."""
    return _run_card_names(run, result)


# ---------------------------------------------------------------------------
# the board, rebuilt from narration
# ---------------------------------------------------------------------------


@dataclass
class Permanent:
    """One permanent the narration named. Not a snapshot — a running tally."""

    name: str
    seq: int
    tapped: bool = False
    token: bool = False
    copy: bool = False
    counters: int = 0


def _pop_named(permanents: list[Permanent], name: str) -> Permanent | None:
    for index in range(len(permanents) - 1, -1, -1):
        if permanents[index].name == name:
            return permanents.pop(index)
    return None


def _most_recent(permanents: list[Permanent], name: str) -> Permanent | None:
    for perm in reversed(permanents):
        if perm.name == name:
            return perm
    return None


def reconstruct_board(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Rebuild a battlefield from the narration stream.

    Facts taken from the sentences, not from a snapshot:

    * ``play_land`` / ``enters the battlefield`` add a permanent;
    * ``token`` adds a token; a following ``copy`` marks the most recent token
      of that name as a copy (the engine emits the token "enters" first);
    * ``becomes tapped`` taps, ``untap`` untaps;
    * ``gets counters`` bumps a counter;
    * ``leaves the battlefield`` removes the most recent permanent of that name
      and **assumes it went to the graveyard** — the stream does not say where.

    Not reconstructed (the narration does not carry it): which controller owns a
    permanent, the exact zone a departing permanent went to, and attachments.
    Those limits are stated on screen beside the board.
    """
    permanents: list[Permanent] = []
    graveyard: dict[str, int] = {}

    for event in events:
        kind = str(event.get("kind") or "")
        text = str(event.get("text") or "")
        low = text.lower()
        actor = str(event.get("actor") or "")
        target = str(event.get("target") or "")
        detail = event.get("detail") or {}
        seq = _as_int(event.get("seq"))

        if kind == "play_land":
            permanents.append(Permanent(actor or "A land", seq))
        elif kind == "token":
            permanents.append(Permanent(actor or "A token", seq, token=True))
        elif kind == "copy":
            name = target or actor
            marked = False
            for perm in reversed(permanents):
                if perm.name == name and perm.token and not perm.copy:
                    perm.copy = True
                    marked = True
                    break
            if not marked and name:
                permanents.append(Permanent(name, seq, token=True, copy=True))
        elif kind == "untap":
            perm = _most_recent(permanents, actor)
            if perm is not None:
                perm.tapped = False
        elif kind == "other":
            if low.endswith(_ENTER_SUFFIX):
                from_raw = str((detail or {}).get("from") or "").lower()
                is_token = "from=null" in from_raw or "from=none" in from_raw
                permanents.append(Permanent(actor or "A permanent", seq, token=is_token))
            elif low.endswith(_LEAVE_SUFFIX):
                removed = _pop_named(permanents, actor)
                if removed is not None:
                    graveyard[removed.name] = graveyard.get(removed.name, 0) + 1
            elif low.endswith(_TAPPED_SUFFIX):
                perm = _most_recent(permanents, actor)
                if perm is not None:
                    perm.tapped = True
            elif low.endswith(_COUNTERS_SUFFIX):
                perm = _most_recent(permanents, actor)
                if perm is not None:
                    perm.counters += 1
            # "X moves" (an attachment) is intentionally ignored: the stream
            # does not name the host, so guessing one would be a lie.
    return {"permanents": permanents, "graveyard": graveyard}


def _classify(name: str, type_line: str) -> str:
    low = str(type_line or "").lower()
    for category in ("Creature", "Planeswalker", "Artifact", "Enchantment"):
        if category.lower() in low:
            return category
    if "land" in low or name in BASIC_LANDS:
        return "Land"
    return "Other"


def group_permanents(
    permanents: list[Permanent], type_lines: dict[str, str]
) -> list[dict[str, Any]]:
    """Stack identical permanents and group the stacks by card type."""
    stacks: OrderedDict[tuple, int] = OrderedDict()
    for perm in permanents:
        key = (perm.name, perm.tapped, perm.token, perm.copy, perm.counters)
        stacks[key] = stacks.get(key, 0) + 1

    groups: dict[str, list[dict[str, Any]]] = {key: [] for key in TYPE_ORDER}
    for (name, tapped, token, copy, counters), count in stacks.items():
        type_line = type_lines.get(name, "")
        category = _classify(name, type_line)
        groups[category].append(
            {
                "name": name,
                "count": count,
                "tapped": tapped,
                "token": token,
                "copy": copy,
                "counters": counters,
                "type_line": type_line,
            }
        )
    ordered: list[dict[str, Any]] = []
    for category in TYPE_ORDER:
        if groups[category]:
            ordered.append(
                {
                    "key": category.lower(),
                    "label": TYPE_LABELS[category],
                    "perm": sorted(groups[category], key=lambda item: item["name"]),
                }
            )
    return ordered


# ---------------------------------------------------------------------------
# growth / the pass counter
# ---------------------------------------------------------------------------


def build_growth(
    observations: list[dict[str, Any]],
    events: list[dict[str, Any]],
    result: dict[str, Any] | None,
) -> dict[str, Any]:
    """The hero: which pass it is and what grew since the same board last pass."""
    resources = observations[-1].get("resources") if observations else {}
    resources = resources if isinstance(resources, dict) else {}

    iteration_events = [e for e in events if e.get("kind") == "iteration"]
    pass_now = 0
    pass_total = 0
    if iteration_events:
        detail = iteration_events[-1].get("detail") or {}
        pass_now = _as_int(detail.get("iteration"), len(iteration_events))
        pass_total = max(
            _as_int((e.get("detail") or {}).get("iteration")) for e in iteration_events
        )
    elif observations:
        pass_now = _as_int(observations[-1].get("iteration"))
        pass_total = pass_now

    current = observations[-1] if observations else None
    previous = None
    if current is not None:
        signature = str(current.get("signature") or "")
        for candidate in reversed(observations[:-1]):
            if signature and str(candidate.get("signature") or "") == signature:
                previous = candidate
                break
        if previous is None and len(observations) >= 2:
            previous = observations[-2]

    grown: dict[str, int] = {}
    prev_pass: int | None = None
    if current is not None and previous is not None:
        before = previous.get("resources") or {}
        after = current.get("resources") or {}
        grown = grew_between(before, after)
        prev_pass = _as_int(previous.get("iteration"))

    first = None
    if current is not None:
        signature = str(current.get("signature") or "")
        for candidate in observations:
            if signature and str(candidate.get("signature") or "") == signature:
                first = candidate
                break
    cumulative: dict[str, int] = {}
    if first is not None and first is not current:
        cumulative = grew_between(
            first.get("resources") or {}, (current or {}).get("resources") or {}
        )

    same_board = bool(previous is not None and prev_pass is not None)
    grown_relevant = _relevant_growth(grown)
    cumulative_relevant = _relevant_growth(cumulative)
    return {
        "resources": resources,
        "current": current,
        "previous": previous,
        "pass_now": pass_now,
        "pass_total": pass_total,
        "prev_pass": prev_pass,
        "same_board": same_board,
        "grown": grown,
        "growth_phrase": _growth_phrase(grown_relevant),
        "cumulative": cumulative,
        "cumulative_phrase": _growth_phrase(cumulative_relevant),
        "loop_start_pass": _as_int(first.get("iteration")) if first is not None else None,
        "observation_count": len(observations),
        "iterations": _as_int((result or {}).get("iterations"), pass_total),
    }


def render_loop_hero(growth: dict[str, Any], live: bool) -> str:
    """The big pass number and the one sentence that explains it."""
    pass_now = growth["pass_now"]
    if not growth["observation_count"]:
        return (
            '<p class="loop-waiting">Waiting for the first sample of the board. '
            "The pass counter appears as soon as the tester looks.</p>"
        )
    if growth["same_board"]:
        if growth["grown"]:
            sentence = (
                f"Same board as pass {growth['prev_pass']}, and "
                f"{growth['growth_phrase']}."
            )
        else:
            sentence = (
                f"Same board as pass {growth['prev_pass']} — nothing changed on "
                "this pass."
            )
    elif growth["grown"]:
        sentence = f"Still growing: {growth['growth_phrase']} since the last sample."
    else:
        sentence = "A fresh sample of the board; nothing has climbed yet."
    if live:
        state = (
            f'<span class="live"><span class="dot"></span> pass {pass_now} · '
            "watching</span>"
        )
    else:
        state = f'<span class="loop-pass-static">pass {pass_now}</span>'
    return (
        '<div class="loop-hero">'
        f'<div class="pass-badge"><span class="pass-num">{pass_now}</span>'
        f'<span class="pass-word">{"pass" if pass_now == 1 else "passes"}</span></div>'
        f'<div class="loop-copy"><p class="loop-sentence">{sentence}</p>'
        f'<p class="loop-sub">{state}'
        f'<span class="faint"> · {growth["observation_count"]} board samples</span>'
        "</p></div></div>"
    )


def render_counters(growth: dict[str, Any]) -> str:
    """Every tracked resource as a named counter, never as a delta."""
    resources = growth.get("resources") or {}
    if not resources:
        return '<p class="empty">No resources recorded yet.</p>'
    keys = [key for key in COUNTER_ORDER if key in resources]
    keys += sorted(key for key in resources if key not in COUNTER_ORDER)
    cells = "".join(
        f'<div class="counter"><div class="k">{esc(_resource_label(key))}</div>'
        f'<div class="v" data-resource="{esc(key)}">{esc(resources[key])}</div></div>'
        for key in keys
    )
    return f'<div class="counters">{cells}</div>'


# ---------------------------------------------------------------------------
# the play-by-play
# ---------------------------------------------------------------------------


def render_play_by_play(
    events: list[dict[str, Any]],
    card_names: list[str],
    *,
    previous: dict[str, Any] | None = None,
) -> str:
    """The narration rows in order, with a turn/phase break when it changes.

    ``previous`` is the row just before this batch, so a live append can decide
    whether the first new row needs a fresh turn heading.
    """
    rows: list[str] = []
    last_context: tuple[Any, Any] = (
        (previous.get("turn"), previous.get("phase")) if previous else (None, None)
    )
    for event in events:
        if event.get("kind") == "iteration":
            detail = event.get("detail") or {}
            number = _as_int(detail.get("iteration"))
            rows.append(
                '<li class="pb-pass"><span class="pb-pass-num">Pass '
                f"{number}</span><span class=\"pb-pass-line\"></span></li>"
            )
            continue
        context = (event.get("turn"), event.get("phase"))
        if context != last_context:
            turn = event.get("turn")
            phase = plain_phase(event.get("phase"))
            turn_text = f"Turn {turn}" if turn not in (None, "") else "Turn —"
            phase_text = f" · {phase}" if phase else ""
            rows.append(
                '<li class="pb-turnbreak"><span>'
                f"{esc(turn_text)}{esc(phase_text)}</span></li>"
            )
            last_context = context
        kind = str(event.get("kind") or "other")
        word = KIND_WORDS.get(kind, kind)
        rows.append(
            f'<li class="pb-row pb-{esc(kind)}" data-seq="{esc(event.get("seq"))}">'
            f'<span class="pb-kind">{esc(word)}</span>'
            f'<span class="pb-text">{_linkify(event.get("text"), card_names)}</span>'
            "</li>"
        )
    return "".join(rows)


# ---------------------------------------------------------------------------
# the loop graph
# ---------------------------------------------------------------------------


_ACTION_KINDS = ("copy", "trigger", "untap", "cast", "activate", "play_land", "token")


def _last_pass(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The narration of the most recent complete pass (or the tail so far)."""
    indices = [i for i, event in enumerate(events) if event.get("kind") == "iteration"]
    if len(indices) >= 2:
        return events[indices[-2] + 1 : indices[-1]]
    if indices:
        return events[indices[-1] + 1 :]
    return list(events)


def build_loop_graph(events: list[dict[str, Any]], growth: dict[str, Any]) -> dict[str, Any]:
    """Derive the loop from one pass of narration: card nodes, action edges.

    The engine is the card most involved in the pass. A ``copy`` action becomes
    "copies <target>"; a following ``untap`` is attributed to the trigger that
    caused it ("untaps <card>") rather than drawn as a floating self-loop. An
    ``activate`` that only produces a copy is folded into that copy so the wheel
    has one arrow per real action.
    """
    steps = [e for e in _last_pass(events) if e.get("kind") in _ACTION_KINDS]
    edges: list[dict[str, Any]] = []
    growth_node = ""
    last_trigger: tuple[str, str] | None = None

    def add(src: str, dst: str, label: str, text: str) -> None:
        if src:
            edges.append({"src": src, "dst": dst or src, "label": label, "text": text})

    for event in steps:
        kind = str(event.get("kind"))
        actor = str(event.get("actor") or "")
        target = str(event.get("target") or "")
        text = str(event.get("text") or "")
        if kind == "copy":
            growth_node = target or actor
            add(actor, target, f"copies {target or actor}", text)
        elif kind == "trigger":
            add(
                actor,
                target,
                f"triggers, targeting {target}" if target else "triggers",
                text,
            )
            if actor:
                last_trigger = (actor, target or actor)
        elif kind == "untap":
            if last_trigger and last_trigger[1] == actor:
                add(last_trigger[0], actor, f"untaps {actor}", text)
            else:
                add(actor, actor, "untaps", text)
        elif kind == "cast":
            add(actor, actor, "is cast", text)
        elif kind == "activate":
            add(actor, target, f"activates, targeting {target}" if target else "activates", text)
        elif kind == "play_land":
            add(actor, actor, "plays a land", text)
        elif kind == "token":
            growth_node = growth_node or actor

    # Fold an "activates" into the "copies" edge it produced (same direction).
    copy_pairs = {(e["src"], e["dst"]) for e in edges if e["label"].startswith("copies")}
    edges = [
        e
        for e in edges
        if not (e["label"].startswith("activates") and (e["src"], e["dst"]) in copy_pairs)
    ]

    nodes: list[str] = []
    for edge in edges:
        for side in ("src", "dst"):
            if edge[side] and edge[side] not in nodes:
                nodes.append(edge[side])

    degree: dict[str, int] = dict.fromkeys(nodes, 0)
    for edge in edges:
        degree[edge["src"]] = degree.get(edge["src"], 0) + 1
        if edge["dst"] != edge["src"]:
            degree[edge["dst"]] = degree.get(edge["dst"], 0) + 1

    hub = ""
    if nodes:
        def rank(node: str) -> tuple[int, int, int]:
            engine = 1 if any(
                e["src"] == node and e["label"].startswith("copies") for e in edges
            ) else 0
            return (degree.get(node, 0), engine, -nodes.index(node))

        hub = max(nodes, key=rank)

    if growth_node not in nodes:
        growth_node = hub
    tokens_now = _as_int((growth.get("resources") or {}).get("tokens"))
    return {
        "nodes": nodes,
        "edges": edges,
        "hub": hub,
        "growth_node": growth_node,
        "tokens": tokens_now,
        "caption": _graph_caption(edges),
    }


def _graph_caption(edges: list[dict[str, Any]]) -> str:
    if not edges:
        return ""
    return " → ".join(edge["label"] for edge in edges)


def _node_box(name: str, hub: str) -> tuple[float, float]:
    """Half-width and half-height of a node rectangle."""
    return (96.0, 24.0) if name == hub else (80.0, 24.0)


def _rect_ray(half_w: float, half_h: float, ux: float, uy: float) -> float:
    """Distance from a rectangle's centre to its edge along the unit ray."""
    tx = half_w / abs(ux) if ux else math.inf
    ty = half_h / abs(uy) if uy else math.inf
    return min(tx, ty)


def _wrap_label(text: str, width: int = 26) -> list[str]:
    """Wrap an action label to at most two short lines so it stays legible."""
    words = str(text or "").split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) > width and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    if len(lines) > 2:
        lines = lines[:2]
        lines[-1] = lines[-1][: max(0, width - 1)] + "…"
    return lines or [""]


def render_loop_graph(graph: dict[str, Any], live: bool) -> str:
    """Inline SVG: cards as nodes, the engine at the hub, actions as arrows."""
    nodes = graph["nodes"]
    edges = graph["edges"]
    hub = graph["hub"]
    if not nodes or not edges:
        return (
            '<p class="empty">No repeating action in the narration yet. '
            "The loop appears here once a pass repeats.</p>"
        )

    width, height = 620, 520
    cx, cy = width / 2, height / 2 - 10
    others = [node for node in nodes if node != hub]
    positions: dict[str, tuple[float, float]] = {hub: (cx, cy)}
    if others:
        radius = 190.0
        for index, node in enumerate(others):
            angle = math.pi / 2 + 2 * math.pi * index / len(others)
            positions[node] = (
                cx + radius * math.cos(angle),
                cy + radius * math.sin(angle),
            )

    # Parallel edges share a key; opposite directions bow to opposite sides so
    # the two halves of the wheel never sit on top of each other.
    counts: dict[tuple[str, str], int] = {}
    for edge in edges:
        counts[(edge["src"], edge["dst"])] = counts.get((edge["src"], edge["dst"]), 0) + 1
    lanes: dict[tuple[str, str], int] = {}
    paths: list[str] = []
    label_items: list[dict[str, Any]] = []
    for edge in edges:
        src, dst = edge["src"], edge["dst"]
        key = (src, dst)
        lane = lanes.get(key, 0)
        lanes[key] = lane + 1
        total = counts[key]
        if src == dst:
            path, lx, ly = _self_loop(positions[src], *_node_box(src, hub))
        else:
            sign = 1.0 if src <= dst else -1.0
            base = 54.0 * sign
            spread = (lane - (total - 1) / 2) * 46.0
            label_t = 0.5 + (lane - (total - 1) / 2) * 0.17
            path, lx, ly = _edge_curve(
                positions[src],
                positions[dst],
                base + spread,
                _node_box(src, hub),
                _node_box(dst, hub),
                label_t,
            )
        pulse = " lg-flow" if live else ""
        paths.append(
            f'<g class="lg-edge{pulse}"><title>{esc(edge["text"])}</title>'
            f'<path d="{path}" marker-end="url(#lg-arrow)"/></g>'
        )
        lines = _wrap_label(edge["label"])
        label_items.append(
            {"x": lx, "y": ly, "lines": lines, "height": 12 * len(lines)}
        )

    # Long Magic sentences do not fit on the arcs, so labels are stacked and
    # given room: a simple vertical de-overlap pass keeps every action legible
    # (the label stroke masks the arcs that pass behind it).
    if label_items:
        ordered = sorted(label_items, key=lambda item: item["y"])
        spacing = 7
        total_h = sum(item["height"] for item in ordered) + spacing * (len(ordered) - 1)
        center = sum(item["y"] for item in ordered) / len(ordered)
        cursor = center - total_h / 2
        for item in ordered:
            item["y"] = cursor + item["height"] / 2
            cursor += item["height"] + spacing
        shift = 0.0
        if ordered[0]["y"] - ordered[0]["height"] / 2 < 26:
            shift = 26 - (ordered[0]["y"] - ordered[0]["height"] / 2)
        if ordered[-1]["y"] + ordered[-1]["height"] / 2 + shift > height - 26:
            shift = height - 26 - (ordered[-1]["y"] + ordered[-1]["height"] / 2)
        for item in ordered:
            item["y"] += shift

    labels: list[str] = []
    for item in label_items:
        for row, line in enumerate(item["lines"]):
            labels.append(
                f'<text class="lg-label" x="{item["x"]:.1f}"'
                f' y="{item["y"] + (row - (len(item["lines"]) - 1) / 2) * 12:.1f}"'
                f' text-anchor="middle">{esc(line)}</text>'
            )

    node_markup: list[str] = []
    for node in nodes:
        x, y = positions[node]
        is_hub = node == hub
        half_w, half_h = _node_box(node, hub)
        limit = int((half_w * 2 - 16) / 6.6)
        display = node if len(node) <= limit else node[: limit - 1] + "…"
        node_markup.append(
            f'<g class="lg-node{" lg-hub" if is_hub else ""}">'
            f"<title>{esc(node)}</title>"
            f'<rect x="{x - half_w:.1f}" y="{y - half_h:.1f}" width="{half_w * 2:.1f}"'
            f' height="{half_h * 2}" rx="11"/>'
            f'<text class="lg-name" x="{x:.1f}" y="{y + 5:.1f}" text-anchor="middle">'
            f"{esc(display)}</text></g>"
        )

    # Growth badge on the card that grows: the tokens tick upward each pass.
    badge = ""
    growth_node = graph.get("growth_node")
    if growth_node and growth_node in positions and graph.get("tokens"):
        gx, gy = positions[growth_node]
        _, half_h = _node_box(growth_node, hub)
        count = graph["tokens"]
        text = f"{count} {_plural(count, 'token', 'tokens')}"
        badge = (
            f'<g class="lg-badge"><rect x="{gx - 50:.1f}" y="{gy + half_h + 8:.1f}"'
            f' width="100" height="22" rx="11"/><text x="{gx:.1f}"'
            f' y="{gy + half_h + 23:.1f}" text-anchor="middle">{esc(text)}</text></g>'
        )

    aria = (
        f"Loop of {len(nodes)} cards: {hub} at the hub; "
        f"{len(edges)} actions. {graph['caption']}"
    )
    return (
        '<div class="loopgraph-wrap">'
        f'<svg class="loopgraph{" is-live" if live else ""}" viewBox="0 0 {width} {height}"'
        f' role="img" aria-label="{esc(aria)}">'
        "<defs>"
        '<marker id="lg-arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7"'
        ' markerHeight="7" orient="auto-start-reverse">'
        '<path d="M0,0 L10,5 L0,10 z" fill="context-stroke"/></marker>'
        "</defs>"
        + "".join(paths)
        + "".join(labels)
        + "".join(node_markup)
        + badge
        + "</svg></div>"
        + (
            f'<p class="graph-caption loop-caption">{esc(hub)} is the engine. '
            f"One pass: {esc(graph['caption'])}.</p>"
            if graph["caption"]
            else ""
        )
    )


def _edge_curve(
    start: tuple[float, float],
    end: tuple[float, float],
    offset: float,
    start_box: tuple[float, float],
    end_box: tuple[float, float],
    label_t: float = 0.5,
) -> tuple[str, float, float]:
    x1, y1 = start
    x2, y2 = end
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy) or 1.0
    ux, uy = dx / length, dy / length
    # Trim each end to the rectangle edge so the arrow leaves and lands on the
    # node, never inside it or (worse) past it.
    start_radius = _rect_ray(*start_box, ux, uy)
    end_radius = _rect_ray(*end_box, ux, uy)
    sx, sy = x1 + ux * start_radius, y1 + uy * start_radius
    ex, ey = x2 - ux * end_radius, y2 - uy * end_radius
    mx, my = (sx + ex) / 2, (sy + ey) / 2
    px, py = -uy, ux
    qx, qy = mx + px * offset, my + py * offset
    t = min(0.85, max(0.15, label_t))
    one = 1 - t
    lx = one * one * sx + 2 * one * t * qx + t * t * ex
    ly = one * one * sy + 2 * one * t * qy + t * t * ey - 4
    return f"M {sx:.1f},{sy:.1f} Q {qx:.1f},{qy:.1f} {ex:.1f},{ey:.1f}", lx, ly


def _self_loop(
    position: tuple[float, float], half_w: float, half_h: float
) -> tuple[str, float, float]:
    x, y = position
    path = (
        f"M {x - half_w + 16:.1f},{y - half_h:.1f} "
        f"C {x - 110:.1f},{y - 150:.1f} {x + 110:.1f},{y - 150:.1f} "
        f"{x + half_w - 16:.1f},{y - half_h:.1f}"
    )
    return path, x, y - 116


# ---------------------------------------------------------------------------
# verdict / state
# ---------------------------------------------------------------------------


def run_life(
    run: dict[str, Any],
    result: dict[str, Any] | None,
    events: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    last_activity: Any = None,
) -> tuple[bool, str]:
    """``(live, state)`` where state is ``live`` / ``ended`` / ``stopped``."""
    if result is not None:
        return False, "ended"
    now = now or datetime.now(UTC)
    stamps = [_parse_ts(run.get("started_at")), _parse_ts(last_activity)]
    if events:
        stamps.append(_parse_ts(events[-1].get("created_at")))
    if observations:
        stamps.append(_parse_ts(observations[-1].get("created_at")))
    latest = max((stamp for stamp in stamps if stamp is not None), default=None)
    if latest is None:
        return True, "live"
    if (now - latest).total_seconds() > LIVE_GRACE_SECONDS:
        return False, "stopped"
    return True, "live"


def run_verdict(
    run: dict[str, Any],
    result: dict[str, Any] | None,
    growth: dict[str, Any],
    live: bool,
    state: str,
) -> dict[str, Any]:
    """The verdict, in one plain sentence a player could argue with."""
    if result is None:
        if state == "stopped":
            return {
                "live": False,
                "verdict": "error",
                "headline": "Stopped early.",
                "sentence": (
                    "This run went quiet before a result was recorded. "
                    "Nothing here says the combo does or doesn't work."
                ),
                "word": "Stopped",
            }
        return {
            "live": True,
            "verdict": "inconclusive",
            "headline": "Still testing.",
            "sentence": (
                "The tester is still working through this combo. The verdict shows "
                "up here the moment it finishes."
            ),
            "word": "Testing",
        }

    verdict = str(result.get("verdict") or "inconclusive")
    evidence = result.get("evidence")
    if isinstance(evidence, str):
        evidence = parse_json(evidence, {}) or {}
    evidence = evidence if isinstance(evidence, dict) else {}
    kind = str(evidence.get("kind") or "")
    raw_reason = str(evidence.get("reason") or "")
    reason_plain = plain_reason(raw_reason, kind) if raw_reason or kind else ""
    cls = verdict_class(verdict)

    if cls == "loops":
        phrase = growth.get("cumulative_phrase") or growth.get("growth_phrase") or ""
        if phrase in ("", "nothing"):
            sentence = (
                "The board came back to the same state, so these cards can repeat "
                "this forever."
            )
        else:
            sentence = (
                f"The board came back to the same state while {phrase} piled up, so "
                "these cards can repeat this forever."
            )
    elif cls in {"no_loop", "refuted"}:
        sentence = reason_plain or "The board never came back to the same state."
    elif cls == "inconclusive":
        sentence = reason_plain or (
            "The tester couldn't reach a verdict, and no reason was recorded — "
            "so treat this as untested rather than safe."
        )
    else:
        # An ``error`` verdict carries a raw exception in evidence; it belongs
        # in a log, not on a screen written for a Magic player.
        sentence = (
            "The tester hit a problem and stopped before reaching a verdict, so "
            "this run says nothing about whether the combo works."
        )

    return {
        "live": False,
        "verdict": verdict,
        "headline": verdict_headline(verdict),
        "sentence": sentence,
        "word": verdict_word(verdict),
        "reason_plain": reason_plain,
        "reason_raw": raw_reason,
    }


def render_verdict(verdict: dict[str, Any]) -> str:
    cls = verdict_class(verdict.get("verdict"))
    if verdict.get("live"):
        cls = "live"
    if verdict.get("headline") == "Stopped early.":
        cls = "error"
    return (
        f'<section class="gf-verdict {esc(cls)}">'
        f'<div class="gf-verdict-head"><h2 class="gf-verdict-title">'
        f'{esc(verdict.get("headline"))}</h2>'
        f'<span class="badge {esc(verdict_class(verdict.get("verdict")))}">'
        f'{esc(verdict.get("word"))}</span></div>'
        f'<p class="gf-verdict-sentence">{esc(verdict.get("sentence"))}</p></section>'
    )


def render_status(
    run: dict[str, Any],
    result: dict[str, Any] | None,
    *,
    live: bool,
    state: str,
    event_count: int,
    observation_count: int,
) -> str:
    if live:
        return (
            '<span class="live"><span class="dot"></span> live</span>'
            f'<span class="faint"> · watching · {event_count} moves · '
            f"{observation_count} samples</span>"
        )
    if state == "stopped":
        return '<span class="live stale"><span class="dot"></span> stopped</span>'
    return '<span class="live ended"><span class="dot"></span> ended</span>'


def render_summary(
    run: dict[str, Any],
    result: dict[str, Any] | None,
    *,
    live: bool,
    growth: dict[str, Any],
) -> str:
    if live:
        return ""
    passes = _as_int((result or {}).get("iterations"), growth.get("pass_total") or 0)
    seeds = run.get("seeds") or parse_json(run.get("seeds_json"), []) or []
    seeds_text = ", ".join(str(s) for s in seeds) if isinstance(seeds, list) else str(seeds)
    cards = _run_card_names(run, result)
    title = " + ".join(cards) if cards else f"Run {run.get('id')}"
    cells = [
        ("pairing", title),
        ("result", verdict_word((result or {}).get("verdict"))),
        ("passes", passes),
        ("seeds", seeds_text or "—"),
        ("ran", short_time(run.get("started_at"))),
    ]
    body = "".join(
        f"<div class=\"counter\"><div class=\"k\">{esc(key)}</div>"
        f'<div class="v">{esc(value)}</div></div>'
        for key, value in cells
    )
    return f'<div class="counters gf-summary">{body}</div>'


# ---------------------------------------------------------------------------
# the page + payloads
# ---------------------------------------------------------------------------


def _type_lines(store: ReadOnlyStore, names: list[str]) -> dict[str, str]:
    from ..corpus.names import normalize_card_name

    wanted = sorted({normalize_card_name(n) for n in names if n})
    if not wanted:
        return {}
    out: dict[str, str] = {}
    for start in range(0, len(wanted), 400):
        chunk = wanted[start : start + 400]
        placeholders = ",".join("?" * len(chunk))
        rows = store.query(
            "SELECT name, type_line FROM cards"
            f" WHERE normalized_name IN ({placeholders})",
            tuple(chunk),
        )
        for row in rows:
            out[str(row["name"])] = str(row.get("type_line") or "")
    return out


def render_board(board: dict[str, Any], groups: list[dict[str, Any]]) -> str:
    graveyard = board.get("graveyard") or {}
    parts: list[str] = []
    for group in groups:
        cells = "".join(_permanent_html(perm) for perm in group["perm"])
        parts.append(
            f'<div class="board-zone"><div class="zone-title">{esc(group["label"])}</div>'
            f'<div class="zone-cards">{cells}</div></div>'
        )
    if not parts:
        parts.append(
            '<div class="board-zone"><p class="empty">No permanents named yet. '
            "The board fills in as the narration arrives.</p></div>"
        )
    if graveyard:
        items = "".join(
            f'<li>{card_link(name, css="zone-card-link")}'
            f'<span class="zone-count">×{esc(count)}</span></li>'
            for name, count in sorted(graveyard.items())
        )
        parts.append(
            '<div class="board-zone zone-graveyard">'
            '<div class="zone-title">Graveyard <span class="faint">(assumed)</span></div>'
            f'<ul class="zone-list">{items}</ul></div>'
        )
    return "".join(parts)


def _permanent_html(perm: dict[str, Any]) -> str:
    name = perm["name"]
    count = perm["count"]
    classes = ["perm"]
    if perm["tapped"]:
        classes.append("is-tapped")
    if perm["token"]:
        classes.append("is-token")
    if perm["copy"]:
        classes.append("is-copy")
    chips: list[str] = []
    if perm["token"]:
        chips.append('<span class="chip chip-token">token</span>')
    if perm["copy"]:
        chips.append('<span class="chip chip-copy">copy</span>')
    if perm["tapped"]:
        chips.append('<span class="chip chip-tapped">tapped</span>')
    if perm["counters"]:
        chips.append(
            f'<span class="chip chip-counter">{perm["counters"]} counters</span>'
        )
    count_badge = f'<span class="perm-count">×{count}</span>' if count > 1 else ""
    img = image_url(name, size="small")
    if img:
        art = (
            f'<button type="button" class="thumb perm-thumb" data-large="'
            f'{esc(image_url(name, size="large") or img)}"'
            f' aria-label="View {esc(name)} larger" title="Click to see the full card">'
            f'<img src="{esc(img)}" alt="{esc(name)}" width="122" height="170"'
            ' loading="lazy" decoding="async"></button>'
        )
    else:
        art = (
            '<div class="thumb-fallback perm-fallback" role="img"'
            f' aria-label="{esc(name)}, no image available">'
            f'<div class="fallback-name">{esc(name)}</div></div>'
        )
    return (
        f'<div class="{" ".join(classes)}">{count_badge}{art}'
        f'<div class="perm-name">{card_link(name, css="zone-card-link")}</div>'
        + (f'<div class="perm-chips">{"".join(chips)}</div>' if chips else "")
        + "</div>"
    )


def build_goldfish(
    store: ReadOnlyStore, run_id: int, *, now: datetime | None = None
) -> dict[str, Any] | None:
    """Assemble every fragment of the watch screen (used by the page and poll)."""
    run = store.run(run_id)
    if run is None:
        return None
    results = store.results_for_run(run_id)
    result = results[0] if results else None
    events = store.events(run_id)
    observations = store.observations(run_id)
    card_names = _run_card_names(run, result)

    live, state = run_life(run, result, events, observations, now=now)
    growth = build_growth(observations, events, result)

    board = reconstruct_board(events)
    names = card_names + [perm.name for perm in board["permanents"]]
    type_lines = _type_lines(store, names)
    groups = group_permanents(board["permanents"], type_lines)
    graph = build_loop_graph(events, growth)

    verdict = run_verdict(run, result, growth, live, state)
    board_html = render_board(board, groups)
    graph_html = render_loop_graph(graph, live)
    loop_html = render_loop_hero(growth, live)
    counters_html = render_counters(growth)
    verdict_html = render_verdict(verdict)
    status_html = render_status(
        run, result, live=live, state=state,
        event_count=len(events), observation_count=len(observations),
    )
    summary_html = render_summary(run, result, live=live, growth=growth)

    seq = _as_int(events[-1].get("seq")) if events else 0
    obs_cursor = _as_int(observations[-1].get("id")) if observations else 0
    return {
        "run": run,
        "result": result,
        "live": live,
        "state": state,
        "verdict": verdict,
        "growth": growth,
        "graph": graph,
        "board": {
            "groups": groups,
            "graveyard": board["graveyard"],
            "permanents": len(board["permanents"]),
        },
        "card_names": card_names,
        "events": events,
        "observations": observations,
        "seq": seq,
        "obs_cursor": obs_cursor,
        "html": {
            "board": board_html,
            "graph": graph_html,
            "loop": loop_html,
            "counters": counters_html,
            "verdict": verdict_html,
            "status": status_html,
            "summary": summary_html,
        },
        "sig": {
            "board": f"{len(board['permanents'])}:{len(board['graveyard'])}:{seq}",
            "graph": f"{graph.get('hub')}:{len(graph.get('edges') or [])}:{graph.get('tokens')}",
            "loop": f"{growth['pass_now']}:{growth['prev_pass']}:{growth['growth_phrase']}:{seq}",
            "counters": repr(sorted((growth.get("resources") or {}).items())),
            "verdict": repr(verdict),
            "status": f"{live}:{state}:{len(events)}",
            "summary": f"{state}:{len(events)}",
        },
    }


def runs_payload(store: ReadOnlyStore, limit: int = 100) -> list[dict[str, Any]]:
    """Runs newest first, in-progress included, with pair names and a live flag."""
    runs = store.list_runs(limit)
    now = datetime.now(UTC)
    out: list[dict[str, Any]] = []
    for run in runs:
        results = store.results_for_run(_as_int(run.get("id")))
        result = results[0] if results else None
        if result is None:
            # The newest of an event, an observation and the run itself; a long
            # but still-active run must not read as stopped.
            activity = max(
                (
                    stamp
                    for stamp in (
                        _parse_ts(run.get("last_event_at")),
                        _parse_ts(run.get("last_observation_at")),
                    )
                    if stamp is not None
                ),
                default=None,
            )
            live, state = run_life(run, None, [], [], now=now, last_activity=activity)
        else:
            live, state = False, "ended"
        cards = run.get("cards") or parse_json(run.get("card_names_json"), []) or []
        out.append(
            {
                "id": run.get("id"),
                "started_at": run.get("started_at"),
                "candidate_key": run.get("candidate_key"),
                "cards": [str(c) for c in cards if c],
                "live": live,
                "state": state,
                "verdict": (result or {}).get("verdict"),
                "iterations": (result or {}).get("iterations"),
                "result_count": run.get("result_count"),
                "observation_count": run.get("observation_count"),
            }
        )
    return out


__all__ = [
    "GROWTH_WORDS",
    "KIND_WORDS",
    "LIVE_GRACE_SECONDS",
    "TYPE_ORDER",
    "build_goldfish",
    "build_growth",
    "build_loop_graph",
    "group_permanents",
    "reconstruct_board",
    "render_board",
    "render_counters",
    "render_loop_graph",
    "render_loop_hero",
    "render_play_by_play",
    "run_card_names",
    "run_life",
    "run_verdict",
    "runs_payload",
]
