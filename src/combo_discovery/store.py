"""Append-only experiment persistence (SQLite), per PLUMBING_SPEC section 4.

The schema mirrors the spec exactly. This module is append-only by design: it
only ever creates, reads, and inserts rows — there are no statement that
mutates or removes existing rows anywhere in this file, and schema_version is
advanced by inserting a new version row (read back with MAX(version)).

Threading: WorkerPool records games from multiple worker threads into the same
database. SQLite serializes writers itself (WAL + a busy timeout), but a single
connection is not safe for concurrent use, so a module-level lock serializes
every database operation across all ExperimentStore instances in the process.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .generated import forge_env_pb2 as pb

if TYPE_CHECKING:  # pragma: no cover - typing only (avoids an import cycle)
    from .runner import DecisionContext, GameResult

# Current schema version. Bump this and register a migration in _MIGRATIONS.
_SCHEMA_VERSION = 1

# Serializes all DB access; see the module docstring for why.
_DB_LOCK = threading.Lock()

# Tables that export_jsonl may read (name whitelist against SQL injection).
_TABLES = (
    "experiments",
    "games",
    "decisions",
    "events",
    "candidates",
    "adjudications",
)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS experiments (
  id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
  engine_commit TEXT, proto_version INTEGER, policy_version TEXT,
  model_version TEXT, config_json TEXT);
CREATE TABLE IF NOT EXISTS games (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES experiments(id),
  server_game_id INTEGER, seed INTEGER, decks_json TEXT,
  player_types_json TEXT, max_turns INTEGER,
  outcome TEXT, winner INTEGER, reason TEXT,
  turn_count INTEGER, duration_ms INTEGER,
  event_count INTEGER, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  game_row INTEGER NOT NULL REFERENCES games(id),
  decision_id INTEGER, player INTEGER, type TEXT,
  answer_json TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  game_row INTEGER NOT NULL REFERENCES games(id),
  seq INTEGER, turn INTEGER, phase TEXT, type TEXT,
  player INTEGER, card_name TEXT, detail_raw TEXT);
CREATE TABLE IF NOT EXISTS candidates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES experiments(id),
  card_names_json TEXT, status TEXT NOT NULL
    CHECK (status IN ('proposed','verified','refuted','inconclusive')),
  evidence_json TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS adjudications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  candidate_id INTEGER NOT NULL REFERENCES candidates(id),
  verdict TEXT, reviewer TEXT, notes TEXT, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_events_game ON events(game_row, seq);
CREATE INDEX IF NOT EXISTS idx_decisions_game ON decisions(game_row, decision_id);
"""


def _migration_2(conn: sqlite3.Connection) -> None:  # pragma: no cover
    """Stub for the next schema bump: raise until a real migration is written."""
    raise NotImplementedError("no migration registered for schema version 2")


# version -> callable applying the change for that version.
_MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {2: _migration_2}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the schema idempotently and advance schema_version by inserting
    rows (append-only)."""
    conn.executescript(_SCHEMA_SQL)
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    current = row[0] if row is not None and row[0] is not None else None
    if current is None:
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (_SCHEMA_VERSION,))
        return
    while current < _SCHEMA_VERSION:
        nxt = current + 1
        migration = _MIGRATIONS.get(nxt)
        if migration is None:
            raise RuntimeError(f"missing schema migration for version {nxt}")
        migration(conn)
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (nxt,))
        current = nxt


def _jsonable(value: Any) -> Any:
    """Best-effort JSON encoding for answer payloads."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    as_tuple = getattr(value, "as_tuple", None)
    if callable(as_tuple):
        return _jsonable(as_tuple())
    return str(value)


def _answer_to_json(answer: Any) -> str:
    if isinstance(answer, pb.DecisionSubmit):
        return json.dumps(
            {"oneof": answer.WhichOneof("answer") or "", "repr": str(answer)},
            sort_keys=True,
        )
    if isinstance(answer, tuple) and len(answer) == 2:
        arm, payload = answer
        return json.dumps({"arm": str(arm), "payload": _jsonable(payload)}, sort_keys=True)
    return json.dumps({"value": _jsonable(answer)}, sort_keys=True)


def _enum_name(enum_type, value: int) -> str:
    try:
        return enum_type.Name(int(value))
    except ValueError:
        return str(value)


@dataclass(frozen=True)
class DecisionRef:
    """The decision fields the runner retains in GameResult.decision_trace.

    ``player`` is -1 (unknown) when recorded from a trace, which only carries
    (decision_id, decision_type, answer). DecisionContext is also accepted by
    ``record_decision`` and supplies the acting player.
    """

    decision_id: int
    decision_type: int
    player: int = -1


class ExperimentStore:
    """Append-only SQLite store for experiment runs, games, decisions, events."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with _DB_LOCK:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            _ensure_schema(self._conn)
            self._conn.commit()

    # -- experiments --------------------------------------------------------

    def start_experiment(
        self,
        *,
        engine_commit: str,
        proto_version: int,
        policy_version: str,
        model_version: str,
        config: dict,
    ) -> str:
        """Create an experiment row and return its uuid4 run id."""
        run_id = str(uuid.uuid4())
        with _DB_LOCK:
            self._conn.execute(
                "INSERT INTO experiments "
                "(id, created_at, engine_commit, proto_version, policy_version, "
                " model_version, config_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    _utc_now(),
                    engine_commit,
                    int(proto_version),
                    policy_version,
                    model_version,
                    json.dumps(config, sort_keys=True),
                ),
            )
            self._conn.commit()
        return run_id

    # -- games / decisions / events ----------------------------------------

    def record_game(
        self,
        run_id: str,
        result: "GameResult",
        decks: list[tuple[str, str]],
        seed: int,
        player_types: Sequence[int],
        max_turns: int,
    ) -> int:
        """Insert a completed game row and return its row id."""
        with _DB_LOCK:
            cur = self._conn.execute(
                "INSERT INTO games "
                "(run_id, server_game_id, seed, decks_json, player_types_json, "
                " max_turns, outcome, winner, reason, turn_count, duration_ms, "
                " event_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    int(result.game_id),
                    int(seed),
                    json.dumps([[n, p] for n, p in decks]),
                    json.dumps([_enum_name(pb.PlayerType, t) for t in player_types]),
                    int(max_turns),
                    _enum_name(pb.Outcome, result.outcome),
                    int(result.winner),
                    result.reason,
                    int(result.turns),
                    int(result.duration_s * 1000),
                    int(result.n_events),
                    _utc_now(),
                ),
            )
            self._conn.commit()
            assert cur.lastrowid is not None  # set by the INSERT above
            return int(cur.lastrowid)

    def record_decision(
        self,
        game_row: int,
        ctx: "DecisionContext | DecisionRef",
        answer: Any,
        accepted: bool = True,
    ) -> None:
        """Record one decision. Only accepted decisions are persisted (the
        schema has no accepted column); rejected answers are the caller's
        concern. ``ctx`` may be a DecisionContext or a DecisionRef."""
        if not accepted:
            return
        decision_id = int(getattr(ctx, "decision_id"))
        dtype = getattr(ctx, "decision_type")
        player = int(getattr(ctx, "player", -1))
        type_name = _enum_name(pb.DecisionType, dtype) if isinstance(dtype, int) else str(dtype)
        with _DB_LOCK:
            self._conn.execute(
                "INSERT INTO decisions "
                "(game_row, decision_id, player, type, answer_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (game_row, decision_id, player, type_name, _answer_to_json(answer), _utc_now()),
            )
            self._conn.commit()

    def record_events(self, game_row: int, events: Iterable[pb.GameEvent]) -> None:
        """Insert the drained event stream for a game (one row per event)."""
        rows = [
            (
                game_row,
                int(e.seq),
                int(e.turn),
                e.phase,
                e.type,
                int(e.player),
                e.card_name,
                e.detail_raw,
            )
            for e in events
        ]
        if not rows:
            return
        with _DB_LOCK:
            self._conn.executemany(
                "INSERT INTO events "
                "(game_row, seq, turn, phase, type, player, card_name, detail_raw) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            self._conn.commit()

    # -- export -------------------------------------------------------------

    def export_jsonl(self, table: str, path: str | Path) -> None:
        """Write every row of ``table`` to ``path`` as JSON lines."""
        if table not in _TABLES:
            raise ValueError(f"unknown table {table!r}; expected one of {_TABLES}")
        with _DB_LOCK:
            rows = self._conn.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608
        with open(path, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(dict(row), sort_keys=True))
                fh.write("\n")

    def close(self) -> None:
        with _DB_LOCK:
            self._conn.close()
