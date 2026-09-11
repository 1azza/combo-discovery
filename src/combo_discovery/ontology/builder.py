"""Orchestration + persistence for the predicate ontology (schema v3).

Reads one corpus import, extracts :class:`~combo_discovery.ontology.extractor.CardPredicate`
values, runs the pattern queries in :mod:`combo_discovery.ontology.edges`, and
appends ``card_predicates`` / ``interactions`` / ``combo_hypotheses`` rows.  The
global ``patterns`` vocabulary is ensured (never duplicated) before the run.

Append-only: every derived row is keyed by ``import_id``; re-running the builder
for an already-built import is a no-op unless ``force=True``.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .. import store as store_module
from ..store import ExperimentStore
from .edges import build_edges
from .extractor import (
    CardContext,
    CardPredicate,
    extract_card_predicates,
    load_contexts,
)
from .patterns import PATTERNS
from .patterns.base import CardView, Edge

#: How many top-scoring edges the report keeps in memory for formatting.
_REPORT_TOP = 200


@dataclass
class OntologyReport:
    """Result of one :func:`build_ontology` call."""

    import_id: str
    already_built: bool = False
    predicate_rows: int = 0
    cards_with_predicates: int = 0
    predicates_by_type: dict[str, int] = field(default_factory=dict)
    interactions_total: int = 0
    interactions_by_pattern: dict[str, int] = field(default_factory=dict)
    hypotheses_total: int = 0
    hypotheses_by_pattern: dict[str, int] = field(default_factory=dict)
    top_hypotheses: list[dict[str, Any]] = field(default_factory=list)
    duration_s: float = 0.0

    def format(self, top_n: int = 20) -> str:
        lines = [
            f"import id            : {self.import_id}",
            f"already built        : {self.already_built}",
            f"predicate rows       : {self.predicate_rows}",
            f"cards w/ predicates  : {self.cards_with_predicates}",
            f"interactions         : {self.interactions_total}",
            f"hypotheses           : {self.hypotheses_total}",
            f"duration             : {self.duration_s:.2f}s",
            "",
            "predicates by type:",
        ]
        for predicate, count in sorted(
            self.predicates_by_type.items(), key=lambda kv: (-kv[1], kv[0])
        ):
            lines.append(f"  {predicate:<24} {count}")
        lines.append("")
        lines.append("interactions by pattern:")
        for pattern, count in sorted(self.interactions_by_pattern.items()):
            lines.append(f"  {pattern:<24} {count}")
        lines.append("")
        lines.append(f"top {top_n} hypotheses:")
        for index, hypothesis in enumerate(self.top_hypotheses[:top_n], start=1):
            lines.append(
                f"  {index:>2}. [{hypothesis['pattern']}] "
                f"score={hypothesis['score']:.3f}  {hypothesis['cards']}"
            )
            lines.append(f"      {hypothesis['mechanism']}")
        return "\n".join(lines)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def latest_import_id(conn: sqlite3.Connection) -> str | None:
    """The most recently inserted corpus import."""
    row = conn.execute(
        "SELECT import_id FROM import_runs ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    return row["import_id"] if row is not None else None


def probe_interactions(
    store: ExperimentStore,
    name: str,
    import_id: str | None = None,
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Interactions involving ``name``, highest score first (recall probe)."""
    from ..corpus.importer import normalize_name

    conn = store._conn
    with store_module._DB_LOCK:
        if import_id is None:
            import_id = latest_import_id(conn)
        if import_id is None:
            return []
        rows = conn.execute(
            "SELECT p.name AS pattern, i.direction, i.mechanism, i.score, "
            "s.name AS source_name, t.name AS target_name "
            "FROM interactions i "
            "JOIN patterns p ON p.id = i.pattern_id "
            "JOIN cards s ON s.id = i.source_card_id "
            "JOIN cards t ON t.id = i.target_card_id "
            "WHERE i.import_id = ? AND (s.normalized_name = ? OR t.normalized_name = ?) "
            "ORDER BY i.score DESC, p.name, s.name LIMIT ?",
            (import_id, normalize_name(name), normalize_name(name), limit),
        ).fetchall()
    return [dict(row) for row in rows]


def ensure_patterns(conn: sqlite3.Connection) -> dict[str, int]:
    """Insert any missing pattern definitions and return ``name -> id``."""
    for pattern in PATTERNS:
        conn.execute(
            "INSERT OR IGNORE INTO patterns (name, description, pattern_json, version) "
            "VALUES (?, ?, ?, ?)",
            (
                pattern.name,
                pattern.description,
                json.dumps(pattern.rule, sort_keys=True),
                pattern.version,
            ),
        )
    rows = conn.execute(
        "SELECT id, name FROM patterns WHERE version = 1 ORDER BY id"
    ).fetchall()
    return {row["name"]: int(row["id"]) for row in rows}


def _write(
    conn: sqlite3.Connection,
    import_id: str,
    contexts: dict[int, CardContext],
    predicates: dict[int, list[CardPredicate]],
    edges: list[Edge],
    pattern_ids: dict[str, int],
) -> None:
    predicate_rows = [
        (
            import_id, card.card_id, card.face_index, card.predicate,
            card.as_params_json(), card.as_evidence_json(), card.confidence,
        )
        for card in (p for preds in predicates.values() for p in preds)
    ]
    conn.executemany(
        "INSERT INTO card_predicates (import_id, card_id, face_index, predicate, "
        "params_json, evidence_json, confidence) VALUES (?, ?, ?, ?, ?, ?, ?)",
        predicate_rows,
    )

    now = _utc_now()
    interaction_rows = [
        (
            import_id, edge.source.card_id, edge.target.card_id,
            pattern_ids[edge.pattern], edge.direction, edge.mechanism, edge.score,
            json.dumps(edge.evidence, sort_keys=True, default=str), now,
        )
        for edge in edges
    ]
    conn.executemany(
        "INSERT INTO interactions (import_id, source_card_id, target_card_id, "
        "pattern_id, direction, mechanism, score, evidence_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        interaction_rows,
    )

    hypothesis_rows = [
        (
            import_id, pattern_ids[edge.pattern],
            json.dumps([edge.source.card_id, edge.target.card_id]),
            edge.mechanism, edge.score, "proposed", now,
        )
        for edge in edges
    ]
    conn.executemany(
        "INSERT INTO combo_hypotheses (import_id, pattern_id, card_ids_json, "
        "mechanism, score, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        hypothesis_rows,
    )


def _report(
    import_id: str,
    contexts: dict[int, CardContext],
    predicates: dict[int, list[CardPredicate]],
    edges: list[Edge],
    duration_s: float,
) -> OntologyReport:
    by_type: Counter[str] = Counter()
    rows = 0
    cards_with = 0
    for preds in predicates.values():
        if preds:
            cards_with += 1
        for pred in preds:
            by_type[pred.predicate] += 1
            rows += 1
    by_pattern: Counter[str] = Counter(edge.pattern for edge in edges)
    name_by_id = {card_id: ctx.name for card_id, ctx in contexts.items()}
    # ``edges`` is already sorted by descending score; keep only a bounded slice
    # for the report so a 0.5M-edge build does not materialise 0.5M dicts.
    top = [
        {
            "pattern": edge.pattern,
            "score": edge.score,
            "cards": f"{name_by_id.get(edge.source.card_id, edge.source.card_id)}"
                     f" + {name_by_id.get(edge.target.card_id, edge.target.card_id)}",
            "mechanism": edge.mechanism,
            "direction": edge.direction,
        }
        for edge in edges[:_REPORT_TOP]
    ]
    return OntologyReport(
        import_id=import_id,
        predicate_rows=rows,
        cards_with_predicates=cards_with,
        predicates_by_type=dict(by_type),
        interactions_total=len(edges),
        interactions_by_pattern=dict(by_pattern),
        hypotheses_total=len(edges),
        hypotheses_by_pattern=dict(by_pattern),
        top_hypotheses=top,
        duration_s=duration_s,
    )


def build_ontology(
    store: ExperimentStore,
    import_id: str | None = None,
    *,
    force: bool = False,
    apply_caps: bool = False,
) -> OntologyReport:
    """Build predicates + interaction edges for one corpus import.

    ``apply_caps`` enables the per-pattern fan-out bounds (off by default; the
    structural filters already keep the full graph at ~0.5M edges).  If the
    import already has predicate rows the builder is a no-op unless
    ``force=True`` (rows are append-only, so a forced rebuild appends a second
    snapshot).
    """
    started = time.monotonic()
    conn = store._conn
    with store_module._DB_LOCK:
        if import_id is None:
            import_id = latest_import_id(conn)
        if import_id is None:
            raise ValueError("no corpus import found; run combo-import-cards first")

        existing = conn.execute(
            "SELECT COUNT(*) AS n FROM card_predicates WHERE import_id = ?",
            (import_id,),
        ).fetchone()["n"]
        if existing and not force:
            return _existing_report(conn, import_id, time.monotonic() - started)

        contexts, effects_by_card = load_contexts(conn, import_id)
        predicates = {
            card_id: extract_card_predicates(context, effects_by_card.get(card_id, []))
            for card_id, context in contexts.items()
        }
        views = {
            card_id: CardView.build(
                context, predicates.get(card_id, []), effects_by_card.get(card_id, [])
            )
            for card_id, context in contexts.items()
        }
        built_edges = build_edges(views, apply_caps=apply_caps)

        with conn:
            pattern_ids = ensure_patterns(conn)
            _write(conn, import_id, contexts, predicates, built_edges, pattern_ids)

    return _report(import_id, contexts, predicates, built_edges, time.monotonic() - started)


def _existing_report(
    conn: sqlite3.Connection, import_id: str, duration_s: float
) -> OntologyReport:
    predicate_rows = conn.execute(
        "SELECT predicate, COUNT(*) AS n FROM card_predicates WHERE import_id = ? "
        "GROUP BY predicate",
        (import_id,),
    ).fetchall()
    interactions = conn.execute(
        "SELECT p.name AS name, COUNT(*) AS n FROM interactions i "
        "JOIN patterns p ON p.id = i.pattern_id WHERE i.import_id = ? GROUP BY p.name",
        (import_id,),
    ).fetchall()
    hypotheses = conn.execute(
        "SELECT p.name AS name, COUNT(*) AS n FROM combo_hypotheses h "
        "JOIN patterns p ON p.id = h.pattern_id WHERE h.import_id = ? GROUP BY p.name",
        (import_id,),
    ).fetchall()
    by_pattern = {row["name"]: int(row["n"]) for row in interactions}
    return OntologyReport(
        import_id=import_id,
        already_built=True,
        predicate_rows=sum(int(row["n"]) for row in predicate_rows),
        cards_with_predicates=conn.execute(
            "SELECT COUNT(DISTINCT card_id) AS n FROM card_predicates WHERE import_id = ?",
            (import_id,),
        ).fetchone()["n"],
        predicates_by_type={row["predicate"]: int(row["n"]) for row in predicate_rows},
        interactions_total=sum(by_pattern.values()),
        interactions_by_pattern=by_pattern,
        hypotheses_total=sum(int(row["n"]) for row in hypotheses),
        hypotheses_by_pattern={row["name"]: int(row["n"]) for row in hypotheses},
        top_hypotheses=[],
        duration_s=duration_s,
    )


__all__ = [
    "OntologyReport",
    "build_ontology",
    "ensure_patterns",
    "latest_import_id",
]
