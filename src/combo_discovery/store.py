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
_SCHEMA_VERSION = 4

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
    "import_runs",
    "cards",
    "card_faces",
    "card_aliases",
    "card_scripts",
    "card_effects",
    "corpus_coverage",
    "patterns",
    "card_predicates",
    "interactions",
    "combo_hypotheses",
    "card_oracle_ids",
    "known_combos",
    "known_combo_cards",
    "known_combo_pairs",
    "known_aliases",
    "observed_decks",
    "observed_deck_cards",
    "observed_pairs",
    "evaluation_runs",
    "evaluation_results",
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

# Schema v2 (card corpus) lives in its own constant so it can be folded into
# the base schema for fresh databases *and* replayed by the v1 -> v2 migration.
_CORPUS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS import_runs (
  import_id TEXT PRIMARY KEY,
  started_at TEXT NOT NULL,
  forge_root TEXT,
  forge_commit TEXT,
  scryfall_source TEXT NOT NULL DEFAULT 'forge_script',
  scryfall_sha256 TEXT,
  scryfall_download_uri TEXT,
  notes TEXT);
CREATE TABLE IF NOT EXISTS cards (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  import_id TEXT NOT NULL REFERENCES import_runs(import_id),
  script_path TEXT,
  file_sha256 TEXT NOT NULL,
  name TEXT NOT NULL,
  normalized_name TEXT NOT NULL,
  mana_cost TEXT, type_line TEXT, oracle_text TEXT, colors TEXT,
  pt TEXT, loyalty TEXT, defense TEXT,
  set_code TEXT, rarity TEXT, collector_number TEXT,
  scryfall_oracle_id TEXT, layout TEXT,
  alternate_mode TEXT, meld_pair TEXT, copy_face_from TEXT,
  effect_count INTEGER NOT NULL DEFAULT 0,
  face_count INTEGER NOT NULL DEFAULT 1,
  is_multiface INTEGER NOT NULL DEFAULT 0,
  parse_ok INTEGER NOT NULL DEFAULT 1);
CREATE INDEX IF NOT EXISTS idx_cards_import ON cards(import_id);
CREATE INDEX IF NOT EXISTS idx_cards_name ON cards(normalized_name);
CREATE TABLE IF NOT EXISTS card_faces (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  import_id TEXT NOT NULL REFERENCES import_runs(import_id),
  card_id INTEGER NOT NULL REFERENCES cards(id),
  face_index INTEGER NOT NULL,
  name TEXT, mana_cost TEXT, type_line TEXT, oracle_text TEXT,
  colors TEXT, pt TEXT, loyalty TEXT, defense TEXT, text TEXT,
  marker TEXT, has_scryfall INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_faces_card ON card_faces(card_id);
CREATE TABLE IF NOT EXISTS card_aliases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  import_id TEXT NOT NULL REFERENCES import_runs(import_id),
  card_id INTEGER NOT NULL REFERENCES cards(id),
  alias TEXT NOT NULL,
  normalized_alias TEXT NOT NULL,
  alias_kind TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_aliases_card ON card_aliases(card_id);
CREATE INDEX IF NOT EXISTS idx_aliases_norm ON card_aliases(normalized_alias);
CREATE TABLE IF NOT EXISTS card_scripts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  import_id TEXT NOT NULL REFERENCES import_runs(import_id),
  card_id INTEGER NOT NULL REFERENCES cards(id),
  script_path TEXT,
  file_sha256 TEXT NOT NULL,
  line_count INTEGER NOT NULL DEFAULT 0,
  face_count INTEGER NOT NULL DEFAULT 1,
  effect_count INTEGER NOT NULL DEFAULT 0,
  svar_count INTEGER NOT NULL DEFAULT 0,
  ability_count INTEGER NOT NULL DEFAULT 0,
  alternate_mode TEXT,
  parse_ok INTEGER NOT NULL DEFAULT 1,
  parse_error_count INTEGER NOT NULL DEFAULT 0,
  error_json TEXT,
  raw TEXT);
CREATE INDEX IF NOT EXISTS idx_scripts_card ON card_scripts(card_id);
CREATE TABLE IF NOT EXISTS card_effects (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  import_id TEXT NOT NULL REFERENCES import_runs(import_id),
  card_id INTEGER NOT NULL REFERENCES cards(id),
  script_id INTEGER REFERENCES card_scripts(id),
  face_index INTEGER NOT NULL DEFAULT 0,
  effect_kind TEXT NOT NULL,
  verb_or_mode TEXT NOT NULL,
  ability_type TEXT,
  zone TEXT,
  is_optional INTEGER NOT NULL DEFAULT 0,
  is_svar INTEGER NOT NULL DEFAULT 0,
  svar_name TEXT,
  description TEXT,
  line_no INTEGER,
  params_json TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS idx_effects_card ON card_effects(card_id);
CREATE INDEX IF NOT EXISTS idx_effects_verb ON card_effects(verb_or_mode);
CREATE INDEX IF NOT EXISTS idx_effects_import ON card_effects(import_id);
CREATE TABLE IF NOT EXISTS corpus_coverage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  import_id TEXT NOT NULL REFERENCES import_runs(import_id),
  metric TEXT NOT NULL,
  key TEXT,
  value INTEGER NOT NULL DEFAULT 0,
  detail_json TEXT);
CREATE INDEX IF NOT EXISTS idx_coverage_import ON corpus_coverage(import_id);
CREATE INDEX IF NOT EXISTS idx_coverage_metric ON corpus_coverage(metric);
"""


def _migration_2(conn: sqlite3.Connection) -> None:
    """Schema v2: the Layer-2 card corpus tables.

    All tables are keyed by ``import_id`` (an FK to ``import_runs``) so every
    import appends a self-contained, immutable snapshot.  The importer only ever
    inserts; re-importing is a new ``import_runs`` row plus new child rows.
    """
    conn.executescript(_CORPUS_SCHEMA_SQL)


# Schema v3 (predicate ontology + interaction graph).  ``patterns`` is the
# global controlled vocabulary (not import-scoped); every derived row is keyed
# by ``import_id`` and appended, never mutated.
_ONTOLOGY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS patterns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  description TEXT,
  pattern_json TEXT NOT NULL DEFAULT '{}',
  version INTEGER NOT NULL DEFAULT 1,
  UNIQUE (name, version));
CREATE TABLE IF NOT EXISTS card_predicates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  import_id TEXT NOT NULL REFERENCES import_runs(import_id),
  card_id INTEGER NOT NULL REFERENCES cards(id),
  face_index INTEGER NOT NULL DEFAULT 0,
  predicate TEXT NOT NULL,
  params_json TEXT NOT NULL DEFAULT '{}',
  evidence_json TEXT NOT NULL DEFAULT '[]',
  confidence REAL NOT NULL DEFAULT 1.0);
CREATE INDEX IF NOT EXISTS idx_predicates_import ON card_predicates(import_id);
CREATE INDEX IF NOT EXISTS idx_predicates_card ON card_predicates(card_id);
CREATE INDEX IF NOT EXISTS idx_predicates_pred ON card_predicates(predicate);
CREATE TABLE IF NOT EXISTS interactions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  import_id TEXT NOT NULL REFERENCES import_runs(import_id),
  source_card_id INTEGER NOT NULL REFERENCES cards(id),
  target_card_id INTEGER NOT NULL REFERENCES cards(id),
  pattern_id INTEGER NOT NULL REFERENCES patterns(id),
  direction TEXT NOT NULL DEFAULT 'one_way'
    CHECK (direction IN ('one_way','mutual')),
  mechanism TEXT,
  score REAL NOT NULL DEFAULT 0,
  evidence_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_interactions_import ON interactions(import_id);
CREATE INDEX IF NOT EXISTS idx_interactions_pattern ON interactions(pattern_id);
CREATE INDEX IF NOT EXISTS idx_interactions_source ON interactions(source_card_id);
CREATE INDEX IF NOT EXISTS idx_interactions_target ON interactions(target_card_id);
CREATE TABLE IF NOT EXISTS combo_hypotheses (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  import_id TEXT NOT NULL REFERENCES import_runs(import_id),
  pattern_id INTEGER NOT NULL REFERENCES patterns(id),
  card_ids_json TEXT NOT NULL,
  mechanism TEXT,
  score REAL NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'proposed'
    CHECK (status IN ('proposed','verified','refuted','inconclusive')),
  created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_hypotheses_import ON combo_hypotheses(import_id);
CREATE INDEX IF NOT EXISTS idx_hypotheses_pattern ON combo_hypotheses(pattern_id);
CREATE INDEX IF NOT EXISTS idx_hypotheses_status_score
  ON combo_hypotheses(status, score);
"""


def _migration_3(conn: sqlite3.Connection) -> None:
    """Schema v3: predicate vocabulary, per-card predicates, interaction edges
    and combo hypotheses (all append-only, corpus rows keyed by ``import_id``)."""
    conn.executescript(_ONTOLOGY_SCHEMA_SQL)


# Schema v4 (known-combo ground truth + evaluation harness).
#
# Tier A is Commander Spellbook (curated, ``known_*``); Tier B is independent
# deck co-occurrence (``observed_*``; placeholders this round).  ``card_oracle_ids``
# is a name-independent bridge from Forge cards to Spellbook/Scryfall oracle ids.
# All rows are appended; nothing here mutates the corpus.
_GROUND_TRUTH_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS card_oracle_ids (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  import_id TEXT NOT NULL REFERENCES import_runs(import_id),
  card_id INTEGER NOT NULL REFERENCES cards(id),
  oracle_id TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'scryfall',
  imported_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_oracle_ids_oracle ON card_oracle_ids(oracle_id);
CREATE INDEX IF NOT EXISTS idx_oracle_ids_card ON card_oracle_ids(card_id);

CREATE TABLE IF NOT EXISTS known_combos (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,
  source_id TEXT NOT NULL UNIQUE,
  source_version TEXT,
  source_timestamp TEXT,
  fetched_at TEXT NOT NULL,
  status TEXT,
  bracket_tag TEXT,
  identity TEXT,
  popularity INTEGER,
  variant_count INTEGER,
  mana_needed TEXT,
  mana_value_needed REAL,
  easy_prereqs TEXT,
  notable_prereqs TEXT,
  description TEXT,
  notes TEXT,
  spoiler INTEGER,
  legalities_json TEXT,
  produces_json TEXT,
  requires_json TEXT,
  n_uses INTEGER,
  n_requires INTEGER,
  n_produces INTEGER,
  canonical_hash TEXT,
  import_id TEXT NOT NULL REFERENCES import_runs(import_id));
CREATE INDEX IF NOT EXISTS idx_known_combos_source ON known_combos(source, source_id);
CREATE INDEX IF NOT EXISTS idx_known_combos_hash ON known_combos(canonical_hash);
CREATE INDEX IF NOT EXISTS idx_known_combos_import ON known_combos(import_id);

CREATE TABLE IF NOT EXISTS known_combo_cards (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  combo_id INTEGER NOT NULL REFERENCES known_combos(id),
  role TEXT NOT NULL CHECK (role IN ('use','require')),
  raw_name TEXT,
  normalized_name TEXT,
  oracle_id TEXT,
  spellbook_card_id INTEGER,
  template_id INTEGER,
  quantity INTEGER,
  used_face INTEGER,
  zone_locations TEXT,
  must_be_commander INTEGER,
  card_state TEXT,
  import_id TEXT NOT NULL REFERENCES import_runs(import_id));
CREATE INDEX IF NOT EXISTS idx_known_cards_combo ON known_combo_cards(combo_id);
CREATE INDEX IF NOT EXISTS idx_known_cards_norm ON known_combo_cards(normalized_name);
CREATE INDEX IF NOT EXISTS idx_known_cards_oracle ON known_combo_cards(oracle_id);

CREATE TABLE IF NOT EXISTS known_combo_pairs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  combo_id INTEGER NOT NULL REFERENCES known_combos(id),
  pair_hash TEXT NOT NULL,
  is_full_variant INTEGER NOT NULL DEFAULT 0,
  source TEXT NOT NULL,
  import_id TEXT NOT NULL REFERENCES import_runs(import_id));
CREATE INDEX IF NOT EXISTS idx_known_pairs_hash ON known_combo_pairs(pair_hash);
CREATE INDEX IF NOT EXISTS idx_known_pairs_combo ON known_combo_pairs(combo_id);
CREATE INDEX IF NOT EXISTS idx_known_pairs_import ON known_combo_pairs(import_id);

CREATE TABLE IF NOT EXISTS known_aliases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  alias_id TEXT NOT NULL,
  canonical_id TEXT,
  import_id TEXT NOT NULL REFERENCES import_runs(import_id));
CREATE INDEX IF NOT EXISTS idx_known_aliases_alias ON known_aliases(alias_id);

-- Tier B (independent deck co-occurrence).  Intentionally empty this round.
CREATE TABLE IF NOT EXISTS observed_decks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,
  source_id TEXT,
  url TEXT,
  commander TEXT,
  format TEXT,
  fetched_at TEXT NOT NULL,
  import_id TEXT NOT NULL REFERENCES import_runs(import_id));
CREATE INDEX IF NOT EXISTS idx_observed_decks_source ON observed_decks(source, source_id);

CREATE TABLE IF NOT EXISTS observed_deck_cards (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  deck_id INTEGER NOT NULL REFERENCES observed_decks(id),
  card_name TEXT,
  normalized_name TEXT,
  oracle_id TEXT,
  quantity INTEGER);
CREATE INDEX IF NOT EXISTS idx_observed_deck_cards_deck ON observed_deck_cards(deck_id);

CREATE TABLE IF NOT EXISTS observed_pairs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  deck_id INTEGER NOT NULL REFERENCES observed_decks(id),
  pair_hash TEXT NOT NULL,
  source TEXT NOT NULL,
  import_id TEXT NOT NULL REFERENCES import_runs(import_id));
CREATE INDEX IF NOT EXISTS idx_observed_pairs_hash ON observed_pairs(pair_hash);

CREATE TABLE IF NOT EXISTS evaluation_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL,
  card_filter TEXT,
  known_import_id TEXT,
  ontology_import_id TEXT,
  params_json TEXT,
  notes TEXT);

CREATE TABLE IF NOT EXISTS evaluation_results (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL REFERENCES evaluation_runs(id),
  scope TEXT NOT NULL CHECK (scope IN ('aggregate','card','pattern')),
  key TEXT,
  pattern TEXT,
  true_positives INTEGER,
  partials INTEGER,
  false_positives INTEGER,
  missed INTEGER,
  precision REAL,
  recall REAL,
  f1 REAL,
  details_json TEXT);
CREATE INDEX IF NOT EXISTS idx_eval_results_run ON evaluation_results(run_id);
CREATE INDEX IF NOT EXISTS idx_eval_results_scope ON evaluation_results(scope, key);
"""


def _migration_4(conn: sqlite3.Connection) -> None:
    """Schema v4: known-combo ground truth (Tier A/B) + evaluation harness."""
    conn.executescript(_GROUND_TRUTH_SCHEMA_SQL)


# version -> callable applying the change for that version.
_MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    2: _migration_2,
    3: _migration_3,
    4: _migration_4,
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the base schema idempotently and advance schema_version by
    inserting rows (append-only).

    Fresh databases are stamped version 1 and then stepped through every
    migration, so the v1 -> v2 corpus tables are created through exactly the
    same path as an upgrade.
    """
    conn.executescript(_SCHEMA_SQL)
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    current = row[0] if row is not None and row[0] is not None else None
    if current is None:
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (1,))
        current = 1
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

    # -- adjudications ------------------------------------------------------

    def record_adjudication(
        self,
        candidate_id: int,
        verdict: str,
        *,
        reviewer: str = "tui",
        notes: str = "",
        allow_unlinked: bool = True,
    ) -> int:
        """Append an adjudication row and return its id.

        The ``candidates`` and ``combo_hypotheses`` tables are not linked yet,
        so callers may pass a ``combo_hypotheses.id``. With ``allow_unlinked``
        the foreign-key check on ``candidate_id`` is suspended for this single
        append (an explicit bridge until the tables are joined); the module
        write lock keeps that pragma flip from interleaving with another write.
        """
        with _DB_LOCK:
            if allow_unlinked:
                self._conn.execute("PRAGMA foreign_keys=OFF")
            try:
                cur = self._conn.execute(
                    "INSERT INTO adjudications "
                    "(candidate_id, verdict, reviewer, notes, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (int(candidate_id), verdict, reviewer, notes, _utc_now()),
                )
                self._conn.commit()
            finally:
                if allow_unlinked:
                    self._conn.execute("PRAGMA foreign_keys=ON")
            assert cur.lastrowid is not None  # set by the INSERT above
            return int(cur.lastrowid)

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
