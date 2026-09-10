"""Data + run-config binding for the TUI.

Everything the console shows comes from the existing library modules:

* reads go through :class:`StoreBinding`, a small read-only view over the same
  SQLite file the append-only :class:`~combo_discovery.store.ExperimentStore`
  owns. It opens its own connection (``PRAGMA query_only``) so UI polling can
  never interfere with the worker thread recording games.
* runs are executed by :meth:`~combo_discovery.pool.WorkerPool.map_games` with
  ``store=`` / ``run_id=``, exactly as the researcher would from the CLI.
* policies are the real :func:`~combo_discovery.runner.default_policy` and
  :class:`~combo_discovery.goldfish.GoldfishPolicy`.

The module is deliberately free of Textual imports: it holds the logic the
tests exercise directly (config validation, store binding, worker probing).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import DEFAULT_CONFIG, Config
from ..env import ForgeEnvClient, ForgeEnvError, HarnessConnectionError, ProtocolMismatchError
from ..goldfish import GoldfishPolicy
from ..pool import WorkerPool
from ..runner import Answer, DecisionContext, default_policy
from ..store import ExperimentStore

# Short connect timeout for worker health probes. The pool's own clients use the
# library default (30s); probing first means the UI never blocks for minutes on
# absent servers and can show connection guidance instead.
PROBE_TIMEOUT = 1.0

# Reasonable upper bound for a TUI-launched batch (keeps the seeds list small).
MAX_SEEDS = 100_000

POLICY_LABELS: dict[str, str] = {
    "default": "default_policy",
    "goldfish": "GoldfishPolicy",
}


class RunCancelled(RuntimeError):
    """Raised inside a policy when the operator cancels an active run."""


# ---------------------------------------------------------------------------
# Run configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunConfig:
    """A fully validated experiment batch configuration."""

    deck_a: tuple[str, str]
    deck_b: tuple[str, str]
    policy_name: str
    seed_start: int
    seed_count: int
    n_workers: int
    base_port: int
    host: str = "localhost"
    max_turns: int = 0
    timeout_seconds: int = 0

    @property
    def seeds(self) -> list[int]:
        return list(range(self.seed_start, self.seed_start + self.seed_count))

    @property
    def deck_pair(self) -> tuple[tuple[str, str], tuple[str, str]]:
        return (self.deck_a, self.deck_b)

    @property
    def total(self) -> int:
        return self.seed_count

    def experiment_config(self) -> dict[str, Any]:
        """The JSON blob recorded on the experiment row."""
        return {
            "policy": self.policy_name,
            "seed_start": self.seed_start,
            "seed_count": self.seed_count,
            "n_workers": self.n_workers,
            "base_port": self.base_port,
            "host": self.host,
            "max_turns": self.max_turns,
            "timeout_seconds": self.timeout_seconds,
            "decks": [list(self.deck_a), list(self.deck_b)],
        }


def _coerce_int(value: Any, label: str, errors: list[str]) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):  # bool is an int subclass; reject it
        errors.append(f"{label} must be a number")
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if text == "":
        errors.append(f"{label} is required")
        return None
    try:
        return int(text)
    except ValueError:
        errors.append(f"{label} must be a number")
        return None


def build_run_config(
    deck_a: tuple[str, str] | None,
    deck_b: tuple[str, str] | None,
    policy_name: str,
    seed_start: Any,
    seed_count: Any,
    n_workers: Any,
    base_port: Any,
    *,
    host: str = "localhost",
    max_turns: Any = 0,
    timeout_seconds: Any = 0,
) -> tuple[RunConfig | None, list[str]]:
    """Validate raw form values and return ``(config, errors)``.

    ``config`` is ``None`` whenever ``errors`` is non-empty, so callers can
    render the messages directly.
    """
    errors: list[str] = []
    if deck_a is None:
        errors.append("pick a deck for seat A")
    if deck_b is None:
        errors.append("pick a deck for seat B")
    if policy_name not in POLICY_LABELS:
        errors.append(f"unknown policy {policy_name!r}")

    start = _coerce_int(seed_start, "seed start", errors)
    count = _coerce_int(seed_count, "seed count", errors)
    workers = _coerce_int(n_workers, "workers", errors)
    port = _coerce_int(base_port, "base port", errors)
    turns = _coerce_int(max_turns, "max turns", errors)
    timeout = _coerce_int(timeout_seconds, "timeout", errors)

    if start is not None and start < 0:
        errors.append("seed start must be >= 0")
    if count is not None:
        if count < 1:
            errors.append("seed count must be >= 1")
        elif count > MAX_SEEDS:
            errors.append(f"seed count capped at {MAX_SEEDS}")
    if workers is not None and workers < 1:
        errors.append("workers must be >= 1")
    if port is not None and not (0 < port < 65536):
        errors.append("base port must be between 1 and 65535")
    if turns is not None and turns < 0:
        errors.append("max turns must be >= 0")
    if timeout is not None and timeout < 0:
        errors.append("timeout must be >= 0")

    if errors or deck_a is None or deck_b is None or count is None or workers is None or port is None:
        return None, errors
    return (
        RunConfig(
            deck_a=deck_a,
            deck_b=deck_b,
            policy_name=policy_name,
            seed_start=start if start is not None else 1,
            seed_count=count,
            n_workers=workers,
            base_port=port,
            host=host,
            max_turns=turns if turns is not None else 0,
            timeout_seconds=timeout if timeout is not None else 0,
        ),
        [],
    )


def discover_decks(decks_dir: str | Path) -> list[tuple[str, str]]:
    """Return ``(stem, path)`` for every ``.dck`` file, sorted by name."""
    directory = Path(decks_dir)
    if not directory.is_dir():
        return []
    found = [(p.stem, str(p)) for p in directory.glob("*.dck") if p.is_file()]
    return sorted(found, key=lambda item: item[0].lower())


def resolve_policy(name: str):
    """Return a fresh policy callable for ``name``."""
    if name == "goldfish":
        return GoldfishPolicy()
    return default_policy


class CancellablePolicy:
    """Wrap a policy so the operator can stop a run between decisions."""

    def __init__(self, inner, cancel_event: threading.Event):
        self._inner = inner
        self._cancel = cancel_event
        self._reset = getattr(inner, "new_game", None)

    def new_game(self) -> None:
        if callable(self._reset):
            self._reset()

    def __call__(self, ctx: DecisionContext) -> Answer:
        if self._cancel.is_set():
            raise RunCancelled("run cancelled by operator")
        return self._inner(ctx)


# ---------------------------------------------------------------------------
# Worker probing + pool construction
# ---------------------------------------------------------------------------


@dataclass
class WorkerProbe:
    """Result of a cheap liveness sweep across ``base_port + i``."""

    host: str
    base_port: int
    results: list[tuple[int, bool, str]] = field(default_factory=list)

    @property
    def reachable(self) -> list[int]:
        return [port for port, ok, _ in self.results if ok]

    @property
    def ok_count(self) -> int:
        return len(self.reachable)

    @property
    def total(self) -> int:
        return len(self.results)

    def status_for(self, port: int) -> str:
        for p, ok, detail in self.results:
            if p == port:
                return "reachable" if ok else "unreachable"
        return "unknown"


def probe_workers(
    host: str, base_port: int, n_workers: int, *, timeout: float = PROBE_TIMEOUT
) -> WorkerProbe:
    """Connect-check each worker port with a short timeout (never raises)."""
    probe = WorkerProbe(host=host, base_port=base_port)
    for i in range(max(0, int(n_workers))):
        port = base_port + i
        client = ForgeEnvClient(host=host, port=port, timeout=timeout)
        try:
            client.connect()
        except ProtocolMismatchError:
            probe.results.append((port, False, "protocol mismatch"))
        except ForgeEnvError:
            probe.results.append((port, False, "unreachable"))
        except Exception as exc:  # noqa: BLE001 - a probe must never crash the UI
            probe.results.append((port, False, type(exc).__name__))
        else:
            probe.results.append((port, True, "reachable"))
        finally:
            with suppress(Exception):
                client.close()
    return probe


def best_contiguous_run(ports: Sequence[int]) -> tuple[int, int] | None:
    """Longest run of consecutive ports, as ``(start, length)``.

    ``WorkerPool`` addresses workers as ``base_port + i``, so a pool can only be
    built over a contiguous range. Picking the longest run means the pool uses
    the most live workers without waiting on absent ones.
    """
    ordered = sorted(set(int(p) for p in ports))
    if not ordered:
        return None
    best = [ordered[0]]
    current = [ordered[0]]
    for port in ordered[1:]:
        if port == current[-1] + 1:
            current.append(port)
        else:
            if len(current) > len(best):
                best = current
            current = [port]
    if len(current) > len(best):
        best = current
    return best[0], len(best)


def no_harness_message(probe: WorkerProbe) -> str:
    last = probe.base_port + max(0, probe.total - 1)
    return (
        "No live harness found.\n\n"
        f"Probed {probe.host}:{probe.base_port}-{last} "
        f"({probe.total} port{'s' if probe.total != 1 else ''}) "
        f"with a {PROBE_TIMEOUT:.0f}s connect.\n\n"
        "Start the Java harness, then press ctrl+t to re-check:\n\n"
        "  java -Djava.awt.headless=true -jar forge-harness-*.jar "
        f"--port {probe.base_port}\n\n"
        "The corpus and recorded experiments are fully browsable offline."
    )


def build_pool(probe: WorkerProbe | None, *, config: Config = DEFAULT_CONFIG) -> WorkerPool:
    """Build a WorkerPool over the longest live contiguous port run."""
    if probe is None:
        raise HarnessConnectionError(
            "no worker probe has run yet — check the harness first (ctrl+t)"
        )
    run = best_contiguous_run(probe.reachable)
    if run is None:
        raise HarnessConnectionError(no_harness_message(probe))
    start, length = run
    return WorkerPool(n_workers=length, base_port=start, host=probe.host, config=config)


def pool_snapshot(pool: WorkerPool | None) -> list[dict[str, Any]]:
    """Defensive snapshot of a pool's health, safe to call from the UI thread."""
    if pool is None:
        return []
    healthy = list(getattr(pool, "_healthy", []))
    fails = list(getattr(pool, "_fail_counts", []))
    rows: list[dict[str, Any]] = []
    for i in range(int(getattr(pool, "n_workers", 0))):
        rows.append(
            {
                "index": i,
                "port": int(getattr(pool, "base_port", 0)) + i,
                "healthy": bool(healthy[i]) if i < len(healthy) else False,
                "failures": int(fails[i]) if i < len(fails) else 0,
            }
        )
    return rows


@dataclass
class ActiveRun:
    """UI-side state for a batch currently being executed."""

    run_id: str
    config: RunConfig
    started_at: float
    cancelled: bool = False
    finished: bool = False
    error: str | None = None


# ---------------------------------------------------------------------------
# Read-only store view
# ---------------------------------------------------------------------------

# Candidate source column names for the (importer-owned) cards table. The first
# match in each group wins, so the TUI tolerates a few plausible schemas.
_CARD_COLUMNS: dict[str, tuple[str, ...]] = {
    "name": ("name", "card_name"),
    "type_line": ("type_line", "type", "types"),
    "mana": ("mana_cost", "mana", "mana_value", "cmc"),
    "oracle": ("oracle_text", "oracle", "text"),
    "effects": ("effects_json", "effects", "effect_count", "n_effects"),
    "set_code": ("set_code", "set"),
    "rarity": ("rarity",),
}


@dataclass(frozen=True)
class CardSchema:
    """Which columns the ``cards`` table actually provides."""

    name: str
    type_line: str | None = None
    mana: str | None = None
    oracle: str | None = None
    effects: str | None = None
    set_code: str | None = None
    rarity: str | None = None


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def parse_json_list(value: Any) -> list[Any]:
    """Best-effort parse of a JSON list/object column into a list."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    text = str(value).strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return []
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        return list(parsed.items())
    return [parsed]


def effects_count(value: Any) -> int | None:
    """Derive an extracted-effects count from a loose column value."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        try:
            return int(float(text))
        except ValueError:
            return None
    if isinstance(parsed, list):
        return len(parsed)
    if isinstance(parsed, dict):
        return len(parsed)
    if isinstance(parsed, bool):
        return None
    if isinstance(parsed, int):
        return parsed
    return None


def pretty_json(value: Any) -> str:
    """Pretty-print a JSON-ish value for the evidence pane."""
    if value is None:
        return ""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return value
    try:
        return json.dumps(value, indent=2, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def short_id(value: str | None, length: int = 8) -> str:
    if not value:
        return "—"
    return value[:length]


def fmt_ms(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "—"
    seconds = float(value) / 1000.0
    if seconds < 1:
        return f"{value:.0f}ms"
    return f"{seconds:.1f}s"


def fmt_duration(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


class StoreBinding:
    """Read-only queries over the experiment SQLite store.

    The append-only :class:`ExperimentStore` owns the schema; this binding
    ensures it exists (so an empty database is a graceful first-run state) and
    then reads through a separate ``query_only`` connection. Every method is
    resilient: a missing table or malformed row yields an empty result and sets
    :attr:`last_error` rather than raising into the UI.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.last_error: str | None = None
        # Creating (and immediately closing) the store guarantees the schema,
        # including for a first run with no database file on disk.
        ExperimentStore(self.path).close()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with suppress(sqlite3.Error):
            self._conn.execute("PRAGMA query_only = ON")
        self._card_schema: CardSchema | None = None
        self._card_schema_loaded = False

    # -- low level ----------------------------------------------------------

    def close(self) -> None:
        with suppress(sqlite3.Error):
            self._conn.close()

    def _rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        try:
            return self._conn.execute(sql, params).fetchall()
        except sqlite3.Error as exc:  # pragma: no cover - defensive
            self.last_error = f"{type(exc).__name__}: {exc}"
            return []

    def _one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        rows = self._rows(sql, params)
        return rows[0] if rows else None

    def has_table(self, name: str) -> bool:
        return bool(
            self._rows(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
            )
        )

    def _columns(self, table: str) -> list[str]:
        # `table` is always an internal constant, never user input.
        return [str(row["name"]) for row in self._rows(f"PRAGMA table_info({table})")]

    def _count(self, table: str) -> int:
        if not self.has_table(table):
            return 0
        row = self._one(f"SELECT COUNT(*) AS n FROM {table}")
        return int(row["n"]) if row is not None else 0

    # -- headline counts ----------------------------------------------------

    def counts(self) -> dict[str, int]:
        return {
            "experiments": self._count("experiments"),
            "games": self._count("games"),
            "events": self._count("events"),
            "candidates": self._count("candidates"),
        }

    # -- experiments / games / events --------------------------------------

    def list_experiments(self, limit: int = 300) -> list[dict[str, Any]]:
        if not self.has_table("experiments"):
            return []
        rows = self._rows(
            """
            SELECT e.id, e.created_at, e.engine_commit, e.proto_version,
                   e.policy_version, e.model_version, e.config_json,
                   (SELECT COUNT(*) FROM games g WHERE g.run_id = e.id) AS game_count,
                   (SELECT COUNT(*) FROM candidates c WHERE c.run_id = e.id)
                       AS candidate_count
            FROM experiments e
            ORDER BY e.created_at DESC, e.rowid DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in rows]

    def experiment(self, run_id: str) -> dict[str, Any] | None:
        row = self._one(
            """
            SELECT e.id, e.created_at, e.engine_commit, e.proto_version,
                   e.policy_version, e.model_version, e.config_json,
                   (SELECT COUNT(*) FROM games g WHERE g.run_id = e.id) AS game_count,
                   (SELECT COUNT(*) FROM candidates c WHERE c.run_id = e.id)
                       AS candidate_count
            FROM experiments e WHERE e.id = ?
            """,
            (run_id,),
        )
        return dict(row) if row is not None else None

    def games_for_run(self, run_id: str, limit: int = 2000) -> list[dict[str, Any]]:
        rows = self._rows(
            """
            SELECT id, seed, outcome, winner, reason, turn_count, duration_ms,
                   event_count, created_at
            FROM games WHERE run_id = ? ORDER BY id LIMIT ?
            """,
            (run_id, limit),
        )
        return [dict(row) for row in rows]

    def recent_games(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._rows(
            """
            SELECT g.id, g.run_id, g.seed, g.outcome, g.winner, g.reason,
                   g.turn_count, g.duration_ms, g.event_count, g.created_at
            FROM games g ORDER BY g.id DESC LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in rows]

    def completed_seeds(self, run_id: str) -> set[int]:
        rows = self._rows("SELECT seed FROM games WHERE run_id = ?", (run_id,))
        return {int(row["seed"]) for row in rows if row["seed"] is not None}

    def event_tail(
        self,
        *,
        since_id: int = 0,
        run_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        sql = [
            "SELECT e.id, e.seq, e.turn, e.phase, e.type, e.player, e.card_name,",
            "       e.detail_raw, g.seed AS seed, g.run_id AS run_id",
            "FROM events e JOIN games g ON g.id = e.game_row",
        ]
        params: list[Any] = []
        if run_id:
            sql.append("WHERE g.run_id = ? AND e.id > ?")
            params.extend([run_id, since_id])
        else:
            sql.append("WHERE e.id > ?")
            params.append(since_id)
        sql.append("ORDER BY e.id ASC LIMIT ?")
        params.append(limit)
        rows = self._rows(" ".join(sql), tuple(params))
        return [dict(row) for row in rows]

    # -- candidates ---------------------------------------------------------

    def list_candidates(self, limit: int = 300) -> list[dict[str, Any]]:
        if not self.has_table("candidates"):
            return []
        rows = self._rows(
            """
            SELECT c.id, c.run_id, c.card_names_json, c.status, c.evidence_json,
                   c.created_at,
                   (SELECT COUNT(*) FROM adjudications a WHERE a.candidate_id = c.id)
                       AS verdict_count
            FROM candidates c
            ORDER BY c.created_at DESC, c.id DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in rows]

    def candidate(self, candidate_id: int) -> dict[str, Any] | None:
        row = self._one(
            """
            SELECT c.id, c.run_id, c.card_names_json, c.status, c.evidence_json,
                   c.created_at,
                   (SELECT COUNT(*) FROM adjudications a WHERE a.candidate_id = c.id)
                       AS verdict_count
            FROM candidates c WHERE c.id = ?
            """,
            (candidate_id,),
        )
        return dict(row) if row is not None else None

    def adjudications(self, candidate_id: int) -> list[dict[str, Any]]:
        rows = self._rows(
            """
            SELECT id, candidate_id, verdict, reviewer, notes, created_at
            FROM adjudications WHERE candidate_id = ? ORDER BY id
            """,
            (candidate_id,),
        )
        return [dict(row) for row in rows]

    # -- cards (importer-owned table; may be absent) ------------------------

    def card_schema(self) -> CardSchema | None:
        if self._card_schema_loaded:
            return self._card_schema
        self._card_schema_loaded = True
        if not self.has_table("cards"):
            self._card_schema = None
            return None
        actual = {name.lower(): name for name in self._columns("cards")}
        mapped: dict[str, str | None] = {}
        for key, candidates in _CARD_COLUMNS.items():
            mapped[key] = next((actual[c] for c in candidates if c in actual), None)
        if mapped.get("name") is None:
            self._card_schema = None
        else:
            self._card_schema = CardSchema(**mapped)  # type: ignore[arg-type]
        return self._card_schema

    def _card_select(self, schema: CardSchema) -> str:
        parts = ["rowid AS _rowid"]
        for key in ("name", "type_line", "mana", "oracle", "effects", "set_code", "rarity"):
            column = getattr(schema, key)
            parts.append(f"{column} AS {key}" if column else f"NULL AS {key}")
        return ", ".join(parts)

    @staticmethod
    def _card_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["_rowid"]),
            "name": row["name"],
            "type_line": row["type_line"],
            "mana": row["mana"],
            "oracle": row["oracle"],
            "effects": effects_count(row["effects"]),
            "set_code": row["set_code"],
            "rarity": row["rarity"],
        }

    def list_cards(self, query: str = "", limit: int = 300) -> list[dict[str, Any]]:
        schema = self.card_schema()
        if schema is None:
            return []
        sql = f"SELECT {self._card_select(schema)} FROM cards"
        params: list[Any] = []
        if query.strip():
            sql += f" WHERE {schema.name} LIKE ? ESCAPE '\\'"
            params.append(f"%{_like_escape(query.strip())}%")
        sql += f" ORDER BY {schema.name} COLLATE NOCASE LIMIT ?"
        params.append(limit)
        return [self._card_row(row) for row in self._rows(sql, tuple(params))]

    def card(self, card_id: int) -> dict[str, Any] | None:
        schema = self.card_schema()
        if schema is None:
            return None
        row = self._one(
            f"SELECT {self._card_select(schema)} FROM cards WHERE rowid = ?",
            (card_id,),
        )
        return self._card_row(row) if row is not None else None

    def card_count(self, query: str = "") -> int:
        schema = self.card_schema()
        if schema is None:
            return 0
        if query.strip():
            row = self._one(
                f"SELECT COUNT(*) AS n FROM cards WHERE {schema.name} LIKE ? ESCAPE '\\'",
                (f"%{_like_escape(query.strip())}%",),
            )
        else:
            row = self._one("SELECT COUNT(*) AS n FROM cards")
        return int(row["n"]) if row is not None else 0


__all__ = [
    "ActiveRun",
    "CancellablePolicy",
    "CardSchema",
    "MAX_SEEDS",
    "POLICY_LABELS",
    "PROBE_TIMEOUT",
    "RunCancelled",
    "RunConfig",
    "StoreBinding",
    "WorkerProbe",
    "best_contiguous_run",
    "build_pool",
    "build_run_config",
    "discover_decks",
    "effects_count",
    "fmt_duration",
    "fmt_ms",
    "no_harness_message",
    "parse_json_list",
    "pool_snapshot",
    "pretty_json",
    "probe_workers",
    "resolve_policy",
    "short_id",
]
