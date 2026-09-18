"""The Gallery: a live kanban of pairings being checked.

Everything here is written for a Magic player. Pairings are described in plain
card words ("free copy + enters-the-battlefield untapper"), never in the
project's internal vocabulary. The board is server-rendered and polled as JSON
so it advances while a sweep runs.

Three load-bearing rules:

* one tile per *pairing* — a pairing tested several times keeps its attempts,
  which the reader can expand; the headline verdict is the latest attempt;
* the Queued column is bounded and paged in SQL (never 468k rows in Python);
* every page change is a plain GET so the board still works without JS, and the
  poll reuses ``location.search`` so it cannot reset the reader's page.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from ..cards import (
    is_etb_untapper,
    is_tap_engine,
    is_untapper,
)
from ..corpus.names import normalize_card_name
from .db import ReadOnlyStore, parse_json
from .images import image_url
from .render import card_link, esc, short_time, strip_motifs

PAGE_SIZE = 12

#: Board columns, in display order: key, heading, empty text.
COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("queued", "Queued", "Waiting to be checked."),
    ("playing", "Playing", "Nothing on the table right now."),
    ("loops", "Loop found", "No loops found yet."),
    ("refuted", "Refuted", "Nothing refuted yet."),
    ("indecided", "Couldn't decide", "Nothing undecided yet."),
)

COLUMN_LABEL = {key: label for key, label, _ in COLUMNS}
COLUMN_KEYS = tuple(key for key, _, _ in COLUMNS)

#: Witness verdict -> board column.
VERDICT_COLUMN = {
    "loops": "loops",
    "no_loop": "refuted",
    "refuted": "refuted",
    "inconclusive": "indecided",
    "error": "indecided",
}

#: Pattern -> the pairing idea in plain Magic words.
PAIRING_IDEAS = {
    "infinite_etb_loop": "free copy + enters-the-battlefield untapper",
    "q:infinite_etb_loop": "free copy + enters-the-battlefield untapper",
    "q:combat_loop": "extra combats + attack untapper",
    "sacrifice_recursion": "sacrifice outlet + recursion",
    "q:sacrifice_loop": "sacrifice outlet + recursion",
    "mana_engine": "mana engine + mana sink",
    "q:mana_loop": "mana engine + mana sink",
    "free_cast_loops": "cast from graveyard + free mana",
    "storm_engine": "storm payoff + cheap spells",
    "color_lock_mill": "colour lock + repeat mill",
    "draw_engine": "life into cards + draw payoff",
    "q:any_cycle": "repeating board state",
}

#: Magic's five colours, plus C for a purely colourless pairing.
COLOUR_LETTERS = "WUBRG"
COLOUR_FILTERS: tuple[tuple[str, str], ...] = (
    ("W", "White"),
    ("U", "Blue"),
    ("B", "Black"),
    ("R", "Red"),
    ("G", "Green"),
    ("C", "Colourless"),
)

TYPE_FILTERS: tuple[str, ...] = (
    "Creature",
    "Instant",
    "Sorcery",
    "Enchantment",
    "Artifact",
    "Planeswalker",
    "Land",
)

BASIC_LANDS = frozenset({"Plains", "Island", "Swamp", "Mountain", "Forest", "Wastes"})

_BRACES = re.compile(r"[{}]")
_COPY_TOKEN = re.compile(
    r"create (?:a|one or more) tokens? that(?:'s| is| are) (?:a |an )?cop(?:y|ies)"
)
_EXTRA_COMBAT = re.compile(r"additional combat phase")
_EXTRA_TURN = re.compile(r"extra turn|additional turn")
_SACRIFICE = re.compile(r"\bsacrifice (?:a|an|another|one or more)\b")
_RECURSION = re.compile(r"graveyard[^.]{0,40}(?:to the battlefield|your hand)")
_MANA_ADD = re.compile(r"add \{")
_DRAW = re.compile(r"draw (?:a|two|three|four|five|x|that many) cards?")
_MILL = re.compile(r"\bmill\b")
_TUTOR = re.compile(r"search your library")
_LIFE_DRAIN = re.compile(r"lose[s]? \d* ?life|you gain .* life")


# ---------------------------------------------------------------------------
# card attributes (derived: the corpus leaves ``colors`` and ``set_code`` empty)
# ---------------------------------------------------------------------------


def card_colours(mana_cost: Any) -> str:
    """Colour letters present in the cost, e.g. "UR" (empty for colourless)."""
    text = _BRACES.sub(" ", str(mana_cost or "")).upper()
    found = [c for c in COLOUR_LETTERS if re.search(rf"(?<![A-Z]){c}(?![A-Z])", text)]
    return "".join(found)


def card_types(type_line: Any) -> set[str]:
    text = str(type_line or "").lower()
    return {t for t in TYPE_FILTERS if t.lower() in text}


def card_attr(card: dict[str, Any]) -> dict[str, Any]:
    cost = card.get("mana_cost")
    return {
        "id": card.get("id"),
        "name": card.get("name") or f"#{card.get('id')}",
        "mana_cost": cost or "",
        "type_line": card.get("type_line") or "",
        "colours": card_colours(cost),
        "types": card_types(card.get("type_line")),
    }


# ---------------------------------------------------------------------------
# filters (colour + type only: mana value cannot be derived reliably here, and
# the corpus carries no set/year, so no control is shown for either)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Filters:
    colours: tuple[str, ...] = ()
    type: str = ""

    @classmethod
    def from_query(cls, query: dict[str, list[str]]) -> Filters:
        raw = (query.get("color") or query.get("colour") or [""])[0]
        colours = tuple(
            c for c in raw.upper().split(",") if c and c in COLOUR_LETTERS + "C"
        )
        kind = (query.get("type") or [""])[0]
        return cls(
            colours=colours,
            type=kind if kind in TYPE_FILTERS else "",
        )

    @property
    def active(self) -> bool:
        return bool(self.colours or self.type)

    def matches(self, attrs: Sequence[dict[str, Any]]) -> bool:
        if not attrs:
            return not self.active
        if self.colours:
            union: set[str] = set()
            for attr in attrs:
                union.update(attr.get("colours") or "")
            chosen = set(self.colours)
            if "C" in chosen:
                ok = not union or bool(union & (chosen - {"C"}))
            else:
                ok = bool(union & chosen)
            if not ok:
                return False
        if self.type and not any(self.type in (a.get("types") or set()) for a in attrs):
            return False
        return True


#: EXISTS clause matching either card of a hypothesis, used to push filters
#: into SQL so paging is exact (no fetch-then-slice).
_CARD_EXISTS = (
    "EXISTS (SELECT 1 FROM json_each(h.card_ids_json) je"
    " JOIN cards c ON c.id = CAST(je.value AS INTEGER) WHERE {cond})"
)


def _filter_clauses(filters: Filters) -> tuple[list[str], list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if filters.colours:
        conds: list[str] = []
        for letter in filters.colours:
            if letter == "C":
                conds.append("UPPER(COALESCE(c.mana_cost, '')) NOT GLOB '*[WUBRG]*'")
            else:
                conds.append("UPPER(COALESCE(c.mana_cost, '')) LIKE ?")
                params.append(f"%{letter}%")
        clauses.append(_CARD_EXISTS.format(cond=" OR ".join(conds)))
    if filters.type:
        clauses.append(_CARD_EXISTS.format(cond="c.type_line LIKE ? ESCAPE '\\'"))
        params.append(f"%{filters.type}%")
    return clauses, params


# ---------------------------------------------------------------------------
# pairing idea
# ---------------------------------------------------------------------------


def pairing_idea(pattern_name: Any, description: Any = "") -> str:
    name = str(pattern_name or "")
    if name in PAIRING_IDEAS:
        return PAIRING_IDEAS[name]
    text = strip_motifs(description or "").strip().rstrip(".")
    if text:
        return text
    return "idea not recorded"


def card_role(card: dict[str, Any]) -> str:
    """A plain Magic word for what the card is doing in the pairing."""
    oracle = card.get("oracle_text") or ""
    low = oracle.lower()
    type_line = card.get("type_line") or ""
    if _COPY_TOKEN.search(low):
        return "copy engine"
    if is_etb_untapper(type_line, oracle):
        return "enters-the-battlefield untapper"
    if is_untapper(type_line, oracle):
        return "untapper"
    if _EXTRA_COMBAT.search(low):
        return "extra combats"
    if _EXTRA_TURN.search(low):
        return "extra turns"
    if is_tap_engine(oracle, type_line):
        return "tap engine"
    if _RECURSION.search(low):
        return "recursion"
    if _SACRIFICE.search(low):
        return "sacrifice outlet"
    if _MANA_ADD.search(low):
        return "mana engine"
    if _TUTOR.search(low):
        return "tutor"
    if _MILL.search(low):
        return "mill"
    if _DRAW.search(low):
        return "draw"
    if _LIFE_DRAIN.search(low):
        return "life drain"
    return ""


def _roles_idea(cards: Sequence[dict[str, Any]]) -> str:
    roles = [card.get("role") for card in cards if card.get("role")]
    unique = list(dict.fromkeys(roles))
    if len(unique) >= 2:
        return f"{unique[0]} + {unique[1]}"
    if unique:
        return f"{unique[0]} + another piece"
    return ""


# ---------------------------------------------------------------------------
# queries
# ---------------------------------------------------------------------------


def _chunks(values: Sequence[Any], size: int = 500) -> list[list[Any]]:
    return [list(values[i : i + size]) for i in range(0, len(values), size)]


def _cards_by_ids(store: ReadOnlyStore, ids: Sequence[int]) -> dict[int, dict[str, Any]]:
    wanted = sorted({int(i) for i in ids})
    out: dict[int, dict[str, Any]] = {}
    for chunk in _chunks(wanted):
        placeholders = ",".join("?" * len(chunk))
        rows = store.query(
            "SELECT id, name, mana_cost, type_line, oracle_text"
            f" FROM cards WHERE id IN ({placeholders})",
            tuple(chunk),
        )
        for row in rows:
            out[int(row["id"])] = row
    return out


def _cards_by_name(
    store: ReadOnlyStore, names: Sequence[str]
) -> dict[str, dict[str, Any]]:
    wanted = sorted({normalize_card_name(n) for n in names if n})
    out: dict[str, dict[str, Any]] = {}
    for chunk in _chunks(wanted):
        placeholders = ",".join("?" * len(chunk))
        rows = store.query(
            "SELECT id, name, mana_cost, type_line, oracle_text"
            f" FROM cards WHERE normalized_name IN ({placeholders})",
            tuple(chunk),
        )
        for row in rows:
            out[normalize_card_name(row["name"])] = row
    return out


def _completed_ids(store: ReadOnlyStore) -> set[int]:
    """Hypothesis ids that already have a result, so Queued can skip them."""
    rows = store.query(
        "SELECT DISTINCT candidate_key FROM witness_results"
        " WHERE candidate_kind = 'pair' AND candidate_key GLOB '[0-9]*'"
    )
    out: set[int] = set()
    for row in rows:
        try:
            out.add(int(row["candidate_key"]))
        except (TypeError, ValueError):
            continue
    return out


def _queued_where(exclude: set[int], filters: Filters) -> tuple[str, list[Any]]:
    clauses = ["h.status = 'proposed'"]
    params: list[Any] = []
    if exclude:
        ordered = sorted(exclude)
        clauses.append(f"h.id NOT IN ({','.join('?' * len(ordered))})")
        params.extend(ordered)
    extra, extra_params = _filter_clauses(filters)
    clauses.extend(extra)
    params.extend(extra_params)
    return " AND ".join(clauses), params


def _queued_count(
    store: ReadOnlyStore, exclude: set[int], filters: Filters
) -> int:
    where, params = _queued_where(exclude, filters)
    row = store.one(f"SELECT COUNT(*) AS n FROM combo_hypotheses h WHERE {where}", params)
    return int(row["n"]) if row else 0


def _queued_page(
    store: ReadOnlyStore,
    exclude: set[int],
    filters: Filters,
    *,
    limit: int,
    offset: int,
) -> list[dict[str, Any]]:
    where, params = _queued_where(exclude, filters)
    return store.query(
        "SELECT h.id, h.card_ids_json, h.mechanism, h.score,"
        " p.name AS pattern, p.description AS pattern_description"
        " FROM combo_hypotheses h"
        " LEFT JOIN patterns p ON p.id = h.pattern_id"
        f" WHERE {where}"
        " ORDER BY h.score DESC, h.id LIMIT ? OFFSET ?",
        (*params, int(limit), int(offset)),
    )


def _result_rows(store: ReadOnlyStore) -> list[dict[str, Any]]:
    return store.query(
        """
        SELECT wr.id AS result_id, wr.run_id, wr.verdict, wr.created_at,
               wr.iterations, wr.card_names_json,
               r.candidate_key AS run_candidate_key,
               r.card_names_json AS run_card_names_json,
               h.id AS hypothesis_id, h.card_ids_json, h.mechanism,
               p.name AS pattern, p.description AS pattern_description
        FROM witness_results wr
        LEFT JOIN witness_runs r ON r.id = wr.run_id
        LEFT JOIN combo_hypotheses h ON h.id = CAST(wr.candidate_key AS INTEGER)
        LEFT JOIN patterns p ON p.id = h.pattern_id
        ORDER BY wr.id
        """
    )


def _playing_rows(store: ReadOnlyStore) -> list[dict[str, Any]]:
    if not store.has_table("witness_runs"):
        return []
    return store.query(
        """
        SELECT r.id AS run_id, r.started_at, r.candidate_key,
               r.card_names_json AS run_card_names_json, r.scenario_json
        FROM witness_runs r
        LEFT JOIN witness_results wr ON wr.run_id = r.id
        WHERE wr.id IS NULL
        ORDER BY r.id DESC
        """
    )


def _scenario_card_names(scenario_json: Any) -> list[str]:
    data = parse_json(scenario_json, {})
    if not isinstance(data, dict):
        return []
    names: list[str] = []
    for player in data.get("players") or []:
        if not isinstance(player, dict):
            continue
        for permanent in player.get("battlefield") or []:
            if not isinstance(permanent, dict):
                continue
            name = permanent.get("name")
            if name and name not in BASIC_LANDS and name not in names:
                names.append(str(name))
    return names


# ---------------------------------------------------------------------------
# card views + grouping
# ---------------------------------------------------------------------------


def _card_view(card: dict[str, Any]) -> dict[str, Any]:
    attr = card_attr(card)
    name = attr["name"]
    attr["role"] = card_role(card)
    small = image_url(name, size="small") if name else None
    large = image_url(name, size="large") if name else None
    attr["image"] = small
    attr["image_large"] = large or small
    return attr


def _resolve_cards(
    ids: Sequence[int],
    names: Sequence[str],
    by_id: dict[int, dict[str, Any]],
    by_name: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    cards = [by_id[i] for i in ids if i in by_id]
    if cards:
        return cards
    out: list[dict[str, Any]] = []
    for name in names:
        if not name:
            continue
        found = by_name.get(normalize_card_name(name))
        out.append(
            found
            or {"id": None, "name": str(name), "mana_cost": "", "type_line": "", "oracle_text": ""}
        )
    return out


def _pair_key(names: Sequence[str], fallback: Any) -> str:
    norm = sorted({normalize_card_name(n) for n in names if n})
    if len(norm) >= 2:
        return "|".join(norm)
    if fallback:
        return f"ck:{fallback}"
    return ""


def _digest(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]


def _names_for_row(row: dict[str, Any]) -> list[str]:
    names = parse_json(row.get("card_names_json"), []) or []
    if not names:
        names = parse_json(row.get("run_card_names_json"), []) or []
    return [str(n) for n in names if n]


def _result_groups(store: ReadOnlyStore, filters: Filters) -> dict[str, list[dict[str, Any]]]:
    """One tile per pairing; attempts collected and left in run order."""
    rows = _result_rows(store)
    by_id = _cards_by_ids(store, _all_card_ids(rows))
    by_name = _cards_by_name(store, _all_card_names(rows))

    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        key = _pair_key(_names_for_row(row), row.get("run_candidate_key"))
        if not key:
            key = f"run:{row.get('run_id')}"
        buckets.setdefault(key, []).append(row)

    grouped: dict[str, list[dict[str, Any]]] = {key: [] for key in COLUMN_KEYS}
    for key, attempts in buckets.items():
        attempts.sort(key=lambda r: r.get("result_id") or 0)
        latest = attempts[-1]
        ids = []
        for row in reversed(attempts):
            if row.get("card_ids_json"):
                ids = _int_ids(row["card_ids_json"])
                break
        cards = [_card_view(c) for c in _resolve_cards(ids, _names_for_row(latest), by_id, by_name)]
        if not cards:
            continue
        column = VERDICT_COLUMN.get(str(latest.get("verdict")), "indecided")
        tile = _tile_from_group(key, column, cards, attempts, latest)
        if filters.matches(cards):
            grouped[column].append(tile)
    for key in COLUMN_KEYS:
        grouped[key].sort(key=_latest_sort_key)
    return grouped


def _latest_sort_key(tile: dict[str, Any]) -> tuple[int, str]:
    return (int(tile.get("latest_id") or 0), tile.get("key") or "")


def _int_ids(raw: Any) -> list[int]:
    out: list[int] = []
    for value in parse_json(raw, []) or []:
        try:
            out.append(int(value))
        except (TypeError, ValueError):
            continue
    return out


def _attempt_view(row: dict[str, Any]) -> dict[str, Any]:
    column = VERDICT_COLUMN.get(str(row.get("verdict")), "indecided")
    return {
        "result_id": row.get("result_id"),
        "run_id": row.get("run_id"),
        "column": column,
        "label": COLUMN_LABEL[column],
        "iterations": row.get("iterations"),
        "when": short_time(row.get("created_at")),
    }


def _tile_from_group(
    key: str,
    status: str,
    cards: Sequence[dict[str, Any]],
    attempts: Sequence[dict[str, Any]],
    latest: dict[str, Any],
) -> dict[str, Any]:
    views = [_attempt_view(row) for row in attempts]
    columns = {view["column"] for view in views}
    pattern = next(
        (row.get("pattern") for row in reversed(attempts) if row.get("pattern")), ""
    )
    description = next(
        (
            row.get("pattern_description")
            for row in reversed(attempts)
            if row.get("pattern_description")
        ),
        "",
    )
    idea = pairing_idea(pattern, description) if pattern else ""
    if not idea:
        idea = _roles_idea(cards) or pairing_idea(None, description)
    return {
        "key": f"p:{_digest(key)}",
        "status": status,
        "status_label": COLUMN_LABEL[status],
        "cards": list(cards),
        "idea": idea,
        "attempts": views,
        "count": len(views),
        "mixed": len(columns) > 1,
        "latest_id": latest.get("result_id"),
        "href": f"/run/{latest.get('run_id')}",
        "href_label": "Open latest run" if len(views) > 1 else "Open run",
        "when": short_time(latest.get("created_at")),
        "detail": strip_motifs(latest.get("mechanism") or ""),
    }


def _queued_tile(
    row: dict[str, Any],
    by_id: dict[int, dict[str, Any]],
) -> dict[str, Any] | None:
    cards = [
        _card_view(c)
        for c in _resolve_cards(_int_ids(row.get("card_ids_json")), [], by_id, {})
    ]
    if not cards:
        return None
    pattern = row.get("pattern")
    idea = pairing_idea(pattern, row.get("pattern_description")) if pattern else ""
    if not idea:
        idea = _roles_idea(cards) or pairing_idea(None, row.get("pattern_description"))
    return {
        "key": f"q:{row.get('id')}",
        "status": "queued",
        "status_label": COLUMN_LABEL["queued"],
        "cards": cards,
        "idea": idea,
        "attempts": [],
        "count": 0,
        "mixed": False,
        "latest_id": 0,
        "href": f"/candidate/{row.get('id')}",
        "href_label": "Open pairing",
        "when": "",
        "detail": strip_motifs(row.get("mechanism") or ""),
    }


def _playing_tile(
    row: dict[str, Any],
    by_name: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    names = parse_json(row.get("run_card_names_json"), []) or []
    if not names:
        names = _scenario_card_names(row.get("scenario_json"))
    cards = [
        _card_view(c)
        for c in _resolve_cards([], [str(n) for n in names], {}, by_name)
    ]
    if not cards:
        return None
    return {
        "key": f"r:{row.get('run_id')}",
        "status": "playing",
        "status_label": COLUMN_LABEL["playing"],
        "cards": cards,
        "idea": _roles_idea(cards) or "on the table now",
        "attempts": [],
        "count": 0,
        "mixed": False,
        "latest_id": 0,
        "href": f"/run/{row.get('run_id')}",
        "href_label": "Watch",
        "when": short_time(row.get("started_at")),
        "detail": "",
    }


def _all_card_ids(rows: Sequence[dict[str, Any]]) -> list[int]:
    ids: list[int] = []
    for row in rows:
        ids.extend(_int_ids(row.get("card_ids_json")))
    return ids


def _all_card_names(rows: Sequence[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for row in rows:
        names.extend(_names_for_row(row))
    return names


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def _card_thumb(card: dict[str, Any]) -> str:
    name = card.get("name") or "Unknown card"
    if card.get("image"):
        inner = (
            f'<img src="{esc(card["image"])}" alt="{esc(name)}"'
            ' width="122" height="170" loading="lazy" decoding="async">'
        )
        return (
            f'<button type="button" class="thumb" data-large="{esc(card.get("image_large"))}"'
            f' aria-label="View {esc(name)} larger"{_ENLARGE_HINT}>{inner}</button>'
        )
    pips = "".join(
        f'<span class="pip pip-{esc(c)}"></span>' for c in (card.get("colours") or "")
    ) or '<span class="pip pip-C"></span>'
    return (
        '<div class="thumb thumb-fallback" role="img"'
        f' aria-label="{esc(name)}, no image available">'
        f'<div class="fallback-name">{esc(name)}</div>'
        f'<div class="fallback-type">{esc(card.get("type_line") or "")}</div>'
        f'<div class="fallback-cost">{esc(card.get("mana_cost") or "")}</div>'
        f'<div class="pips">{pips}</div>'
        "</div>"
    )


_ENLARGE_HINT = ' title="Click to see the full card"'


def _attempts_html(tile: dict[str, Any]) -> str:
    attempts = tile.get("attempts") or []
    if len(attempts) <= 1:
        return ""
    items = "".join(
        f'<li class="try"><span class="chip chip-{esc(a["column"])}">{esc(a["label"])}</span>'
        f'<span class="try-meta">{esc(a["iterations"])} passes'
        f'<span class="faint"> · </span>{esc(a["when"])}</span>'
        f'<a class="try-link" href="/run/{esc(a["run_id"])}">run {esc(a["run_id"])} →</a></li>'
        for a in reversed(attempts)
    )
    return (
        f'<details class="tile-tries"><summary>{len(attempts)} attempts</summary>'
        f'<ul class="tries">{items}</ul></details>'
    )


def render_tile(tile: dict[str, Any]) -> str:
    status = tile["status"]
    card_names = " + ".join(
        card_link(card.get("name"), css="tile-card-link") for card in tile["cards"]
    )
    mixed = '<span class="tile-mixed">mixed results</span>' if tile.get("mixed") else ""
    detail = ""
    if tile.get("detail"):
        detail = (
            '<details class="tile-more"><summary>Why these two?</summary>'
            f'<p>{esc(tile["detail"])}</p>'
            "</details>"
        )
    when = f'<time class="tile-when">{esc(tile["when"])}</time>' if tile.get("when") else ""
    thumbs = "".join(_card_thumb(card) for card in tile["cards"])
    return (
        f'<article class="tile tile-{esc(status)}" data-key="{esc(tile["key"])}">'
        f'<div class="tile-art">{thumbs}</div>'
        '<div class="tile-body">'
        f'<p class="tile-cards">{card_names}</p>'
        f'<p class="tile-idea">{esc(tile["idea"])}</p>'
        f"{_attempts_html(tile)}{detail}</div>"
        '<footer class="tile-foot">'
        f'<span class="chip chip-{esc(status)}">{esc(tile["status_label"])}</span>'
        f"{mixed}{when}"
        f'<a class="tile-open" href="{esc(tile["href"])}">{esc(tile["href_label"])} →</a>'
        "</footer>"
        "</article>"
    )


def _pager_html(column: dict[str, Any]) -> str:
    count = column.get("count") or 0
    if count <= 0:
        return ""
    start, end = column.get("start", 0), column.get("end", 0)
    pages = max(1, column.get("pages", 1))
    page = column.get("page", 1)
    showing = (
        f"showing {start}\u2013{end} of {count:,}"
        if count > (column.get("showing") or 0)
        else f"showing {count:,}"
    )
    nav = ""
    if pages > 1:
        prev = (
            f'<a class="page-link" rel="prev" href="{esc(column["prev_href"])}">← prev</a>'
            if column.get("prev_href")
            else '<span class="page-link disabled">← prev</span>'
        )
        nxt = (
            f'<a class="page-link" rel="next" href="{esc(column["next_href"])}">next →</a>'
            if column.get("next_href")
            else '<span class="page-link disabled">next →</span>'
        )
        nav = (
            f'<span class="col-page-nav">{prev}'
            f'<span class="col-page-num">page {page} of {pages:,}</span>{nxt}</span>'
        )
    return f'<div class="col-page"><span class="col-showing">{showing}</span>{nav}</div>'


def render_column(column: dict[str, Any]) -> str:
    key = column["key"]
    body = column.get("body_html")
    if not body:
        body = f'<p class="col-empty">{esc(column.get("empty", "Nothing here yet."))}</p>'
    count = column.get("count") or 0
    return (
        f'<header class="col-head">'
        f'<h2 class="col-title" id="col-{esc(key)}-title">{esc(column["label"])}</h2>'
        f'<span class="col-count" data-count="{esc(key)}">{count:,}</span>'
        "</header>"
        f'<div class="board-body" data-body="{esc(key)}">{body}</div>'
        f"{_pager_html(column)}"
    )


def render_board(columns: Sequence[dict[str, Any]], tab: str) -> str:
    parts: list[str] = []
    for column in columns:
        key = column["key"]
        inner = column.get("html") or render_column(column)
        parts.append(
            f'<section class="board-col col-{esc(key)}" data-col="{esc(key)}"'
            f' data-sig="{esc(column.get("sig", ""))}"'
            f' aria-labelledby="col-{esc(key)}-title">{inner}</section>'
        )
    return f'<div class="board" id="gallery-board" data-active="{esc(tab)}">{"".join(parts)}</div>'


def render_tabs(tabs: Sequence[dict[str, Any]]) -> str:
    parts: list[str] = []
    for tab in tabs:
        active = " active" if tab.get("active") else ""
        current = ' aria-current="page"' if tab.get("active") else ""
        parts.append(
            f'<a class="status-tab tab-{esc(tab["key"])}{active}" href="{esc(tab["href"])}"'
            f'{current}><span class="tab-mark" aria-hidden="true"></span>'
            f'<span class="tab-label">{esc(tab["label"])}</span>'
            f'<span class="tab-count" data-tab-count="{esc(tab["key"])}">'
            f'{int(tab.get("count") or 0):,}</span></a>'
        )
    return f'<nav class="status-tabs" aria-label="Status">{"".join(parts)}</nav>'


# ---------------------------------------------------------------------------
# query-string helpers
# ---------------------------------------------------------------------------


def _clean_query(query: dict[str, list[str]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key in ("color", "type", "tab"):
        value = (query.get(key) or [""])[0]
        if value:
            out[key] = value
    return out


def _page_param(query: dict[str, list[str]]) -> int:
    try:
        return max(1, int((query.get("page") or ["1"])[0]))
    except (TypeError, ValueError):
        return 1


def _href(params: dict[str, str], **overrides: Any) -> str:
    merged = dict(params)
    for key, value in overrides.items():
        if value in (None, "", 0):
            merged.pop(key, None)
        else:
            merged[key] = str(value)
    qs = urlencode(merged)
    return f"/?{qs}" if qs else "/"


def _slice(items: Sequence[Any], page: int, per_page: int) -> tuple[list[Any], dict[str, int]]:
    total = len(items)
    offset = (page - 1) * per_page
    window = list(items[offset : offset + per_page])
    return window, {
        "count": total,
        "page": page,
        "pages": max(1, -(-total // per_page)),
        "start": offset + 1 if total else 0,
        "end": min(offset + len(window), total),
    }


def _default_tab(counts: dict[str, int]) -> str:
    if counts.get("playing"):
        return "playing"
    if counts.get("loops"):
        return "loops"
    for key in ("refuted", "indecided", "queued"):
        if counts.get(key):
            return key
    return "queued"


# ---------------------------------------------------------------------------
# payload
# ---------------------------------------------------------------------------


def build_gallery(
    store: ReadOnlyStore,
    query: dict[str, list[str]] | None = None,
    *,
    per_page: int = PAGE_SIZE,
) -> dict[str, Any]:
    """Assemble the board: totals, one page of tiles per column, pager links."""
    query = query or {}
    params = _clean_query(query)
    filters = Filters.from_query(query)
    page = _page_param(query)

    # -- results: one tile per pairing, latest attempt decides the column ----
    grouped = _result_groups(store, filters)
    counts = {key: len(grouped[key]) for key in COLUMN_KEYS if key not in {"queued", "playing"}}

    # -- playing: schema v8 identity, so no result row == in progress --------
    playing_rows = _playing_rows(store)
    playing_by_name = _cards_by_name(
        store,
        [
            name
            for row in playing_rows
            for name in (parse_json(row.get("run_card_names_json"), []) or [])
            or _scenario_card_names(row.get("scenario_json"))
        ],
    )
    playing_tiles = [
        tile
        for tile in (_playing_tile(row, playing_by_name) for row in playing_rows)
        if tile is not None and filters.matches(tile["cards"])
    ]
    counts["playing"] = len(playing_tiles)

    # -- queued: paged in SQL, excluding anything already run ----------------
    exclude = _completed_ids(store)
    for row in playing_rows:
        key = row.get("candidate_key")
        if key and str(key).isdigit():
            exclude.add(int(key))
    counts["queued"] = _queued_count(store, exclude, filters)
    queued_offset = (page - 1) * per_page
    queued_rows = _queued_page(
        store, exclude, filters, limit=per_page, offset=queued_offset
    )
    queued_by_id = _cards_by_ids(store, _all_card_ids(queued_rows))
    queued_tiles = [
        tile
        for tile in (_queued_tile(row, queued_by_id) for row in queued_rows)
        if tile is not None
    ]

    # -- page slice for the small columns ------------------------------------
    paged = {
        "queued": (
            queued_tiles,
            {
                "count": counts["queued"],
                "page": page,
                "pages": max(1, -(-counts["queued"] // per_page)),
                "start": queued_offset + 1 if counts["queued"] else 0,
                "end": min(queued_offset + len(queued_tiles), counts["queued"]),
            },
        ),
        "playing": _slice(playing_tiles, page, per_page),
        "loops": _slice(grouped["loops"], page, per_page),
        "refuted": _slice(grouped["refuted"], page, per_page),
        "indecided": _slice(grouped["indecided"], page, per_page),
    }

    active_tab = (query.get("tab") or [""])[0]
    if active_tab not in COLUMN_KEYS:
        active_tab = _default_tab(counts)

    columns: list[dict[str, Any]] = []
    for key, label, empty in COLUMNS:
        tiles, meta = paged[key]
        pages = meta["pages"]
        clamped = min(page, pages)
        column = {
            "key": key,
            "label": label,
            "empty": empty,
            "count": meta["count"],
            "showing": len(tiles),
            "page": clamped,
            "pages": pages,
            "start": meta["start"],
            "end": meta["end"],
            "body_html": "".join(render_tile(tile) for tile in tiles),
            "prev_href": _href(params, page=clamped - 1) if clamped > 1 else "",
            "next_href": _href(params, page=clamped + 1) if clamped < pages else "",
        }
        # ``html`` is the whole column (head + tiles + pager) so the poll can
        # swap it for the server-rendered version byte-for-byte.
        column["html"] = render_column(column)
        column["sig"] = f"{meta['count']}:{len(column['html'])}:{clamped}"
        columns.append(column)

    tabs = [
        {
            "key": column["key"],
            "label": column["label"],
            "count": column["count"],
            "href": _href(params, tab=column["key"], page=None),
            "active": column["key"] == active_tab,
        }
        for column in columns
    ]

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "tab": active_tab,
        "tabs": tabs,
        "filters": {"colours": list(filters.colours), "type": filters.type},
        "columns": columns,
        "counts": counts,
        "page": page,
    }


__all__ = [
    "COLUMNS",
    "COLUMN_LABEL",
    "COLUMN_KEYS",
    "COLOUR_FILTERS",
    "Filters",
    "PAIRING_IDEAS",
    "PAGE_SIZE",
    "TYPE_FILTERS",
    "VERDICT_COLUMN",
    "build_gallery",
    "card_attr",
    "card_colours",
    "card_role",
    "card_types",
    "pairing_idea",
    "render_board",
    "render_column",
    "render_tabs",
    "render_tile",
]
