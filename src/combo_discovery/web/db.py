"""Read-only access to the research database.

The web console must never mutate the store. Every connection is opened with a
``file:...?mode=ro`` URI *and* ``PRAGMA query_only=ON`` as a belt-and-braces
guard, so even a coding mistake in a handler cannot write. A single connection
is shared behind a lock because :class:`http.server.ThreadingHTTPServer`
dispatches each request on its own thread and SQLite connections are not safe
for concurrent use.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import quote

#: Resource keys the observation table always shows, in the requested order.
RESOURCE_COLUMNS = (
    "tokens",
    "permanents",
    "mana",
    "life",
    "graveyard",
    "library",
    "hand",
)


def open_read_only(path: str | Path) -> sqlite3.Connection:
    """Open ``path`` read-only. Raises :class:`sqlite3.OperationalError` if absent."""
    resolved = Path(path).expanduser().resolve()
    uri = f"file:{quote(str(resolved))}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # Wait for a live writer instead of failing instantly on a locked WAL.
    conn.execute("PRAGMA busy_timeout = 3000")
    conn.execute("PRAGMA query_only = ON")
    return conn


def parse_json(value: Any, default: Any = None) -> Any:
    """Best-effort JSON decode; returns ``default`` on anything unparseable."""
    if value is None:
        return default
    if isinstance(value, (list, dict, int, float, bool)):
        return value
    try:
        return json.loads(str(value))
    except (ValueError, TypeError):
        return default


def like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class ReadOnlyStore:
    """A small, defensive, read-only view over the append-only store."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(Path(path))
        self.last_error: str | None = None
        self._lock = threading.RLock()
        self._conn = open_read_only(path)

    # -- low level ----------------------------------------------------------

    def close(self) -> None:
        with suppress(sqlite3.Error):
            with self._lock:
                self._conn.close()

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            try:
                return [dict(row) for row in self._conn.execute(sql, tuple(params))]
            except sqlite3.Error as exc:  # pragma: no cover - defensive
                self.last_error = f"{type(exc).__name__}: {exc}"
                return []

    def one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def has_table(self, name: str) -> bool:
        return bool(
            self.one(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (name,),
            )
        )

    # -- witness runs / results / observations ------------------------------

    def run(self, run_id: int) -> dict[str, Any] | None:
        return self.one("SELECT * FROM witness_runs WHERE id = ?", (int(run_id),))

    def results_for_run(self, run_id: int) -> list[dict[str, Any]]:
        return self.query(
            "SELECT * FROM witness_results WHERE run_id = ? ORDER BY id",
            (int(run_id),),
        )

    def results_for_candidate(self, candidate_key: str) -> list[dict[str, Any]]:
        if not self.has_table("witness_results"):
            return []
        rows = self.query(
            """
            SELECT wr.*, r.started_at, r.engine_commit, r.policy_version,
                   r.seeds_json, r.params_json
            FROM witness_results wr
            LEFT JOIN witness_runs r ON r.id = wr.run_id
            WHERE wr.candidate_key = ?
            ORDER BY wr.id DESC
            """,
            (str(candidate_key),),
        )
        for row in rows:
            row["cards"] = parse_json(row.get("card_names_json"), []) or []
        return rows

    def observations(self, run_id: int) -> list[dict[str, Any]]:
        rows = self.query(
            "SELECT * FROM witness_observations WHERE run_id = ? ORDER BY id",
            (int(run_id),),
        )
        for row in rows:
            row["resources"] = parse_json(row.get("resources_json"), {}) or {}
        return rows

    def observations_after(
        self, run_id: int, after_id: int, limit: int = 500
    ) -> list[dict[str, Any]]:
        """Observations for a run with ``id`` greater than a cursor, oldest first."""
        rows = self.query(
            "SELECT * FROM witness_observations WHERE run_id = ? AND id > ?"
            " ORDER BY id LIMIT ?",
            (int(run_id), int(after_id), int(limit)),
        )
        for row in rows:
            row["resources"] = parse_json(row.get("resources_json"), {}) or {}
        return rows

    def events(self, run_id: int) -> list[dict[str, Any]]:
        """Card-level narration rows for a run, in ``seq`` order."""
        rows = self.query(
            "SELECT * FROM witness_events WHERE run_id = ? ORDER BY seq",
            (int(run_id),),
        )
        for row in rows:
            row["detail"] = parse_json(row.get("detail_json"), {}) or {}
        return rows

    def events_after(
        self, run_id: int, after_seq: int, limit: int = 500
    ) -> list[dict[str, Any]]:
        """Narration rows with ``seq`` greater than a cursor, in ``seq`` order."""
        rows = self.query(
            "SELECT * FROM witness_events WHERE run_id = ? AND seq > ?"
            " ORDER BY seq LIMIT ?",
            (int(run_id), int(after_seq), int(limit)),
        )
        for row in rows:
            row["detail"] = parse_json(row.get("detail_json"), {}) or {}
        return rows

    def list_runs(self, limit: int = 100) -> list[dict[str, Any]]:
        if not self.has_table("witness_runs"):
            return []
        rows = self.query(
            """
            SELECT r.*,
                   (SELECT COUNT(*) FROM witness_results wr WHERE wr.run_id = r.id)
                       AS result_count,
                   (SELECT COUNT(*) FROM witness_observations o WHERE o.run_id = r.id)
                       AS observation_count,
                   (SELECT MAX(created_at) FROM witness_events e WHERE e.run_id = r.id)
                       AS last_event_at,
                   (SELECT MAX(created_at) FROM witness_observations o WHERE o.run_id = r.id)
                       AS last_observation_at
            FROM witness_runs r
            ORDER BY r.id DESC
            LIMIT ?
            """,
            (int(limit),),
        )
        for row in rows:
            row["seeds"] = parse_json(row.get("seeds_json"), []) or []
            row["params"] = parse_json(row.get("params_json"), {}) or {}
            row["cards"] = parse_json(row.get("card_names_json"), []) or []
        return rows

    # -- candidates ---------------------------------------------------------

    def hypothesis(self, hypothesis_id: int) -> dict[str, Any] | None:
        row = self.one(
            """
            SELECT h.id, h.import_id, h.pattern_id, h.card_ids_json, h.mechanism,
                   h.score, h.status, h.created_at,
                   p.name AS pattern, p.description AS pattern_description
            FROM combo_hypotheses h
            LEFT JOIN patterns p ON p.id = h.pattern_id
            WHERE h.id = ?
            """,
            (int(hypothesis_id),),
        )
        if row is None:
            return None
        return self._decorate_hypothesis(row)

    def _decorate_hypothesis(self, row: dict[str, Any]) -> dict[str, Any]:
        ids: list[int] = []
        for value in parse_json(row.get("card_ids_json"), []) or []:
            try:
                ids.append(int(value))
            except (TypeError, ValueError):
                continue
        names = self.card_names(ids)
        row["card_ids"] = ids
        row["cards"] = [{"id": cid, "name": names.get(cid, f"#{cid}")} for cid in ids]
        row["card_names"] = [card["name"] for card in row["cards"]]
        return row

    def interactions_for_pair(
        self, card_a: int, card_b: int
    ) -> list[dict[str, Any]]:
        """Distinct proposed interactions between two cards (either direction)."""
        if not self.has_table("interactions"):
            return []
        rows = self.query(
            """
            SELECT i.id, i.source_card_id, i.target_card_id, i.pattern_id,
                   i.direction, i.mechanism, i.score, i.evidence_json,
                   p.name AS pattern
            FROM interactions i
            LEFT JOIN patterns p ON p.id = i.pattern_id
            WHERE (i.source_card_id = ? AND i.target_card_id = ?)
               OR (i.source_card_id = ? AND i.target_card_id = ?)
            ORDER BY i.score DESC, i.id
            """,
            (int(card_a), int(card_b), int(card_b), int(card_a)),
        )
        seen: set[tuple[Any, Any, Any]] = set()
        out: list[dict[str, Any]] = []
        for row in rows:
            key = (row["source_card_id"], row["target_card_id"], row["pattern_id"])
            if key in seen:
                continue
            seen.add(key)
            row["evidence"] = parse_json(row.get("evidence_json"), []) or []
            out.append(row)
        return out

    def known_pair_state(self, pair_hash: str) -> str:
        """``known`` / ``contained`` / ``unknown`` for a normalised pair hash."""
        if not pair_hash or not self.has_table("known_combo_pairs"):
            return "unknown"
        row = self.one(
            "SELECT MAX(is_full_variant) AS full FROM known_combo_pairs"
            " WHERE pair_hash = ?",
            (pair_hash,),
        )
        if row is None or row["full"] is None:
            return "unknown"
        return "known" if row["full"] else "contained"

    # -- cards --------------------------------------------------------------

    def card_names(self, ids: Sequence[int]) -> dict[int, str]:
        wanted = sorted({int(i) for i in ids})
        if not wanted or not self.has_table("cards"):
            return {}
        placeholders = ",".join("?" * len(wanted))
        rows = self.query(
            f"SELECT id, name FROM cards WHERE id IN ({placeholders})",
            tuple(wanted),
        )
        return {int(row["id"]): row["name"] for row in rows}

    def cards(self, ids: Sequence[int]) -> list[dict[str, Any]]:
        wanted = sorted({int(i) for i in ids})
        if not wanted or not self.has_table("cards"):
            return []
        placeholders = ",".join("?" * len(wanted))
        return self.query(
            "SELECT id, name, normalized_name, mana_cost, type_line, oracle_text,"
            f" set_code, rarity FROM cards WHERE id IN ({placeholders}) ORDER BY name",
            tuple(wanted),
        )


__all__ = [
    "RESOURCE_COLUMNS",
    "ReadOnlyStore",
    "like_escape",
    "open_read_only",
    "parse_json",
]
