"""The Gallery: a live kanban of pairings being checked.

Everything here is written for a Magic player. Pairings are described in plain
card words ("free copy + enters-the-battlefield untapper"), never in the
project's internal vocabulary. The board is server-rendered and polled as JSON
so it advances while a sweep runs.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..cards import (
    is_activated_copy_engine,
    is_etb_untapper,
    is_tap_engine,
    is_untapper,
)
from ..corpus.names import normalize_card_name
from .db import ReadOnlyStore, parse_json
from .images import image_url
from .render import card_link, esc, short_time, strip_motifs

#: Board columns, in display order: key, heading, empty text.
COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("queued", "Queued", "Waiting to be checked."),
    ("playing", "Playing", "Nothing on the table right now."),
    ("loops", "Loop found", "No loops found yet."),
    ("refuted", "Refuted", "Nothing refuted yet."),
    ("indecided", "Couldn't decide", "Nothing undecided yet."),
)

COLUMN_LABEL = {key: label for key, label, _ in COLUMNS}

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

MV_FILTERS: tuple[str, ...] = ("0", "1", "2", "3", "4", "5+")

BASIC_LANDS = frozenset({"Plains", "Island", "Swamp", "Mountain", "Forest", "Wastes"})

_BRACES = re.compile(r"[{}]")


# ---------------------------------------------------------------------------
# card attributes (derived: the corpus leaves ``colors`` and ``set_code`` empty)
# ---------------------------------------------------------------------------


def mana_value(mana_cost: Any) -> int:
    """Mana value from the corpus' space-separated cost ("2 G", "U R", "no cost")."""
    text = _BRACES.sub(" ", str(mana_cost or "")).strip()
    if not text or text.lower() in {"no cost", "none"}:
        return 0
    total = 0
    for token in re.split(r"\s+", text):
        token = token.strip("{} ").upper()
        if not token:
            continue
        if token.isdigit():
            total += int(token)
            continue
        if token == "X":
            continue
        hybrid = re.match(r"^(\d+)/", token)
        if hybrid:
            total += int(hybrid.group(1))
            continue
        total += 1
    return total


def mv_bucket(value: int) -> str:
    return "5+" if value >= 5 else str(value)


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
    mv = mana_value(cost)
    return {
        "id": card.get("id"),
        "name": card.get("name") or f"#{card.get('id')}",
        "mana_cost": cost or "",
        "type_line": card.get("type_line") or "",
        "mv": mv,
        "mv_bucket": mv_bucket(mv),
        "colours": card_colours(cost),
        "types": card_types(card.get("type_line")),
    }


# ---------------------------------------------------------------------------
# filters
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Filters:
    colours: tuple[str, ...] = ()
    mv: str = ""
    type: str = ""

    @classmethod
    def from_query(cls, query: dict[str, list[str]]) -> Filters:
        raw = (query.get("color") or query.get("colour") or [""])[0]
        colours = tuple(
            c for c in raw.upper().split(",") if c and c in COLOUR_LETTERS + "C"
        )
        mv = (query.get("mv") or [""])[0]
        kind = (query.get("type") or [""])[0]
        return cls(
            colours=colours,
            mv=mv if mv in MV_FILTERS else "",
            type=kind if kind in TYPE_FILTERS else "",
        )

    @property
    def active(self) -> bool:
        return bool(self.colours or self.mv or self.type)

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
        if self.mv and not any(a.get("mv_bucket") == self.mv for a in attrs):
            return False
        if self.type and not any(self.type in (a.get("types") or set()) for a in attrs):
            return False
        return True


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
    return "pairing to check"


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


def _completed_keys(store: ReadOnlyStore) -> set[str]:
    rows = store.query(
        "SELECT DISTINCT candidate_key FROM witness_results"
        " WHERE candidate_kind = 'pair' AND candidate_key IS NOT NULL"
    )
    return {str(row["candidate_key"]) for row in rows}


def _total_hypotheses(store: ReadOnlyStore) -> int:
    row = store.one("SELECT COUNT(*) AS n FROM combo_hypotheses")
    return int(row["n"]) if row else 0


def _result_rows(store: ReadOnlyStore) -> list[dict[str, Any]]:
    return store.query(
        """
        SELECT wr.id AS result_id, wr.run_id, wr.candidate_key, wr.verdict,
               wr.created_at, wr.iterations, wr.card_names_json,
               h.id AS hypothesis_id, h.card_ids_json, h.mechanism, h.score,
               p.name AS pattern, p.description AS pattern_description
        FROM witness_results wr
        LEFT JOIN combo_hypotheses h ON h.id = CAST(wr.candidate_key AS INTEGER)
        LEFT JOIN patterns p ON p.id = h.pattern_id
        ORDER BY wr.id DESC
        """
    )


def _queued_rows(store: ReadOnlyStore, limit: int) -> list[dict[str, Any]]:
    return store.query(
        """
        SELECT h.id, h.card_ids_json, h.mechanism, h.score,
               p.name AS pattern, p.description AS pattern_description
        FROM combo_hypotheses h
        LEFT JOIN patterns p ON p.id = h.pattern_id
        WHERE h.status = 'proposed'
        ORDER BY h.score DESC, h.id
        LIMIT ?
        """,
        (int(limit),),
    )


def _playing_rows(store: ReadOnlyStore, limit: int) -> list[dict[str, Any]]:
    if not store.has_table("witness_runs"):
        return []
    return store.query(
        """
        SELECT r.id AS run_id, r.started_at, r.scenario_json
        FROM witness_runs r
        LEFT JOIN witness_results wr ON wr.run_id = r.id
        WHERE wr.id IS NULL
        ORDER BY r.id DESC
        LIMIT ?
        """,
        (int(limit),),
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
# tiles
# ---------------------------------------------------------------------------


def _card_role(card: dict[str, Any]) -> str:
    """A plain Magic word for what the card is doing in the pairing."""
    oracle = card.get("oracle_text") or ""
    type_line = card.get("type_line") or ""
    if is_activated_copy_engine(oracle):
        return "copy engine"
    if is_etb_untapper(type_line, oracle):
        return "enters-the-battlefield untapper"
    if is_untapper(type_line, oracle):
        return "untapper"
    if is_tap_engine(oracle, type_line):
        return "tap engine"
    return ""


def _card_view(card: dict[str, Any]) -> dict[str, Any]:
    attr = card_attr(card)
    name = attr["name"]
    attr["role"] = _card_role(card)
    small = image_url(name, size="small") if name else None
    large = image_url(name, size="large") if name else None
    attr["image"] = small
    attr["image_large"] = large or small
    return attr


def _tile(
    *,
    key: str,
    status: str,
    cards: Sequence[dict[str, Any]],
    idea: str,
    href: str,
    href_label: str,
    when: str = "",
    detail: str = "",
) -> dict[str, Any]:
    return {
        "key": key,
        "status": status,
        "status_label": COLUMN_LABEL.get(status, status),
        "cards": list(cards),
        "idea": idea,
        "href": href,
        "href_label": href_label,
        "when": when,
        "detail": detail,
    }


def _resolve_cards(
    row: dict[str, Any],
    by_id: dict[int, dict[str, Any]],
    by_name: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Prefer the hypothesis' card ids, else fall back to the recorded names."""
    ids = [
        int(value)
        for value in parse_json(row.get("card_ids_json"), []) or []
        if str(value).lstrip("-").isdigit()
    ]
    cards = [by_id[i] for i in ids if i in by_id]
    if cards:
        return cards
    names = parse_json(row.get("card_names_json"), []) or []
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


def _roles_idea(cards: Sequence[dict[str, Any]]) -> str:
    roles = [card.get("role") for card in cards if card.get("role")]
    if len(roles) >= 2:
        return f"{roles[0]} + {roles[1]}"
    if roles:
        return f"{roles[0]} + another piece"
    return ""


def _make_tile(
    row: dict[str, Any],
    by_id: dict[int, dict[str, Any]],
    by_name: dict[str, dict[str, Any]],
    *,
    status: str,
) -> dict[str, Any] | None:
    cards = [_card_view(card) for card in _resolve_cards(row, by_id, by_name)]
    if not cards:
        return None
    idea = pairing_idea(row.get("pattern"), row.get("pattern_description"))
    if not row.get("pattern"):
        idea = _roles_idea(cards) or idea
    is_result = status != "queued"
    hypothesis_id = row.get("hypothesis_id") or row.get("id")
    href = (
        f"/run/{row.get('run_id')}"
        if is_result and row.get("run_id") is not None
        else f"/candidate/{hypothesis_id}"
    )
    tile_key = f"h:{hypothesis_id}" if hypothesis_id else f"r:{row.get('run_id')}"
    return _tile(
        key=tile_key,
        status=status,
        cards=cards,
        idea=idea,
        href=href,
        href_label="Open run" if is_result else "Open pairing",
        when=short_time(row.get("created_at")) if is_result else "",
        detail=strip_motifs(row.get("mechanism") or ""),
    )


def _playing_tile(row: dict[str, Any], cardmap: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    names = _scenario_card_names(row.get("scenario_json"))
    cards = [
        _card_view(cardmap[normalize_card_name(name)])
        for name in names
        if normalize_card_name(name) in cardmap
    ]
    if not cards:
        cards = [
            _card_view({"id": None, "name": name, "mana_cost": "", "type_line": ""})
            for name in names
        ]
    if not cards:
        return None
    return _tile(
        key=f"r:{row.get('run_id')}",
        status="playing",
        cards=cards,
        idea=_roles_idea(cards) or "on the table now",
        href=f"/run/{row.get('run_id')}",
        href_label="Watch",
        when=short_time(row.get("started_at")),
    )


def _filter_tiles(
    tiles: Sequence[dict[str, Any]], filters: Filters, limit: int
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for tile in tiles:
        if not filters.matches(tile["cards"]):
            continue
        out.append(tile)
        if len(out) >= limit:
            break
    return out


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
            f' aria-label="View {esc(name)} larger">{inner}</button>'
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


def render_tile(tile: dict[str, Any]) -> str:
    status = tile["status"]
    card_names = " + ".join(
        card_link(card.get("name"), css="tile-card-link") for card in tile["cards"]
    )
    detail = ""
    if tile.get("detail"):
        detail = (
            '<details class="tile-more"><summary>Why these two?</summary>'
            f'<p>{esc(tile["detail"])}</p>'
            '<p><a href="' + esc(tile["href"]) + '">See the full test →</a></p>'
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
        f"{detail}</div>"
        '<footer class="tile-foot">'
        f'<span class="chip chip-{esc(status)}">{esc(tile["status_label"])}</span>'
        f"{when}"
        f'<a class="tile-open" href="{esc(tile["href"])}">{esc(tile["href_label"])} →</a>'
        "</footer>"
        "</article>"
    )


def render_board(columns: Sequence[dict[str, Any]]) -> str:
    parts: list[str] = []
    for column in columns:
        key = column["key"]
        body = column.get("html")
        if not body:
            body = f'<p class="col-empty">{esc(column.get("empty", "Nothing here yet."))}</p>'
        parts.append(
            f'<section class="board-col col-{esc(key)}" data-col="{esc(key)}"'
            f' aria-labelledby="col-{esc(key)}-title">'
            '<header class="col-head">'
            f'<h2 class="col-title" id="col-{esc(key)}-title">{esc(column["label"])}</h2>'
            f'<span class="col-count" data-count="{esc(key)}">{esc(column["count"])}</span>'
            "</header>"
            f'<div class="board-body" data-body="{esc(key)}">{body}</div>'
            "</section>"
        )
    return f'<div class="board" id="gallery-board">{"".join(parts)}</div>'


# ---------------------------------------------------------------------------
# payload
# ---------------------------------------------------------------------------


def build_gallery(
    store: ReadOnlyStore,
    query: dict[str, list[str]] | None = None,
    *,
    per_column: int = 18,
) -> dict[str, Any]:
    """Assemble the board: counts, tiles and pre-rendered HTML per column."""
    query = query or {}
    filters = Filters.from_query(query)
    # The swept hypotheses are the highest scoring ones, so a shallow scan would
    # return almost nothing once they have results: scan deep enough to fill
    # Queued even after the top of the list has been checked.
    queued_scan = per_column * (40 if filters.active else 25)

    # -- results (loops / refuted / couldn't decide) ------------------------
    result_rows = _result_rows(store)
    result_by_id = _cards_by_ids(store, _all_card_ids(result_rows))
    result_by_name = _cards_by_name(store, _all_card_names(result_rows))
    grouped: dict[str, list[dict[str, Any]]] = {key: [] for key, _, _ in COLUMNS}
    counts = {key: 0 for key, _, _ in COLUMNS}
    for row in result_rows:
        column = VERDICT_COLUMN.get(str(row.get("verdict")), "indecided")
        tile = _make_tile(row, result_by_id, result_by_name, status=column)
        if tile is None:
            continue
        counts[column] += 1
        if filters.matches(tile["cards"]):
            grouped[column].append(tile)

    # -- queued -------------------------------------------------------------
    completed = _completed_keys(store)
    total = _total_hypotheses(store)
    counts["queued"] = max(0, total - len(completed))
    queued_rows = [
        row for row in _queued_rows(store, queued_scan) if str(row["id"]) not in completed
    ]
    queued_by_id = _cards_by_ids(store, _all_card_ids(queued_rows))
    queued_tiles = [
        tile
        for tile in (
            _make_tile(row, queued_by_id, {}, status="queued") for row in queued_rows
        )
        if tile is not None
    ]

    # -- playing ------------------------------------------------------------
    playing_rows = _playing_rows(store, 12)
    playing_names = [
        name for row in playing_rows for name in _scenario_card_names(row.get("scenario_json"))
    ]
    playing_cards = _cards_by_name(store, playing_names)
    playing_tiles = [
        tile
        for tile in (_playing_tile(row, playing_cards) for row in playing_rows)
        if tile is not None
    ]
    counts["playing"] = len(playing_tiles)

    # A pairing being played should not also sit in Queued.
    playing_pairs = {
        frozenset(normalize_card_name(c["name"]) for c in tile["cards"])
        for tile in playing_tiles
    }
    queued_tiles = [
        tile
        for tile in queued_tiles
        if frozenset(normalize_card_name(c["name"]) for c in tile["cards"]) not in playing_pairs
    ]

    grouped["queued"] = _filter_tiles(queued_tiles, filters, per_column)
    grouped["playing"] = _filter_tiles(playing_tiles, filters, per_column)
    for key in ("loops", "refuted", "indecided"):
        grouped[key] = grouped[key][:per_column]

    columns: list[dict[str, Any]] = []
    for key, label, empty in COLUMNS:
        tiles = grouped[key]
        columns.append(
            {
                "key": key,
                "label": label,
                "empty": empty,
                "count": counts.get(key, 0),
                "showing": len(tiles),
                "html": "".join(render_tile(tile) for tile in tiles),
            }
        )

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "filters": {"colours": list(filters.colours), "mv": filters.mv, "type": filters.type},
        "columns": columns,
        "counts": counts,
    }


def _all_card_ids(rows: Sequence[dict[str, Any]]) -> list[int]:
    ids: list[int] = []
    for row in rows:
        for value in parse_json(row.get("card_ids_json"), []) or []:
            try:
                ids.append(int(value))
            except (TypeError, ValueError):
                continue
    return ids


def _all_card_names(rows: Sequence[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for row in rows:
        for value in parse_json(row.get("card_names_json"), []) or []:
            if value:
                names.append(str(value))
    return names


__all__ = [
    "COLUMNS",
    "COLUMN_LABEL",
    "COLOUR_FILTERS",
    "Filters",
    "MV_FILTERS",
    "PAIRING_IDEAS",
    "TYPE_FILTERS",
    "VERDICT_COLUMN",
    "build_gallery",
    "card_attr",
    "card_colours",
    "card_types",
    "mana_value",
    "mv_bucket",
    "pairing_idea",
    "render_board",
    "render_tile",
]
