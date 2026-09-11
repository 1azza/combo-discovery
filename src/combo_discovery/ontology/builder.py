"""Orchestration + persistence for the ontology (schema v3).

Two build paths share one corpus import (each appends its own rows; nothing is
deleted, so the legacy baseline stays measurable):

**algebra (default).**  Extracts per-card predicates, projects them into
ability signatures, builds the scoped interaction graph, enumerates cycles and
names them with the query vocabulary.  Each cycle becomes a ``combo_hypotheses``
row per matched query (``card_ids_json``, mechanism with preconditions, score)
and every pairwise link of every cycle becomes an ``interactions`` row (the
richest proposal surface, which is what the evaluation harness scores).  Query
patterns are registered as ``q:<name>`` rows; legacy pattern rows are never
touched.

**legacy (``mode="legacy"``).**  The original ``patterns`` registry
(``build_edges``): one ``interactions``/``combo_hypotheses`` row per scored
edge.

Both paths are append-only and keyed by ``import_id``; re-running the same mode
for an already-built import is a no-op unless ``force=True``.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import combinations
from typing import Any, Iterable, Sequence

from ..corpus.spellbook import DEFAULT_VINTAGE_FORMAT, VintageLegality
from .. import store as store_module
from ..store import ExperimentStore
from .budget import DEFAULT_MAX_SECONDS, DEFAULT_MAX_STEPS
from .cycles import (
    Combo,
    Graph,
    discover_combos,
    load_motif_weights,
    tight_pool,
)
from .edges import build_edges
from .extractor import (
    CardContext,
    CardPredicate,
    extract_card_predicates,
    load_contexts,
)
from .links import LinkOptions
from .patterns import PATTERNS
from .patterns.base import CardView, Edge
from .ports import AbilitySig, build_signatures
from .queries import QUERIES

#: How many top-scoring edges the report keeps in memory for formatting.
_REPORT_TOP = 200

#: Pattern-name prefix for the algebra query vocabulary.
QUERY_PREFIX = "q:"

MODES = ("algebra", "legacy")


@dataclass
class OntologyReport:
    """Result of one :func:`build_ontology` call."""

    import_id: str
    mode: str = "algebra"
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
    # Algebra-path diagnostics.
    pool_size: int = 0
    links_total: int = 0
    cycles_total: int = 0
    truncated: bool = False
    truncation_reason: str | None = None
    budget_report: dict[str, Any] | None = None
    cost_estimate: dict[str, Any] | None = None
    warnings: tuple[str, ...] = ()

    def format(self, top_n: int = 20) -> str:
        lines = [
            f"import id            : {self.import_id}",
            f"mode                 : {self.mode}",
            f"already built        : {self.already_built}",
            f"predicate rows       : {self.predicate_rows}",
            f"cards w/ predicates  : {self.cards_with_predicates}",
            f"interactions         : {self.interactions_total}",
            f"hypotheses           : {self.hypotheses_total}",
            f"duration             : {self.duration_s:.2f}s",
        ]
        if self.mode == "algebra":
            lines += [
                f"pool size            : {self.pool_size}",
                f"graph links          : {self.links_total}",
                f"cycles found         : {self.cycles_total}",
                f"truncated            : {self.truncated}"
                + (f" ({self.truncation_reason})" if self.truncation_reason else ""),
            ]
            if self.budget_report:
                br = self.budget_report
                lines.append(
                    f"budget               : max_seconds={br.get('max_seconds')} "
                    f"max_steps={br.get('max_steps')} steps={br.get('steps')} "
                    f"elapsed={br.get('elapsed_s')}s"
                )
            if self.cost_estimate:
                ce = self.cost_estimate
                lines.append(
                    f"cost estimate        : pool={ce.get('pool_size')} "
                    f"pairwise={ce.get('pairwise_candidates')} "
                    f"closure_depth={ce.get('closure_depth')}"
                )
            for warning in self.warnings:
                lines.append(f"warning              : {warning}")
        lines += ["", "predicates by type:"]
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
    """The most recent *corpus* import (i.e. one that actually has cards).

    The database also carries non-corpus imports (Scryfall oracle-id backfill,
    Commander Spellbook), so "latest import_runs row" is not the corpus import.
    """
    row = conn.execute(
        "SELECT r.import_id AS import_id FROM import_runs r "
        "WHERE EXISTS (SELECT 1 FROM cards c WHERE c.import_id = r.import_id) "
        "ORDER BY r.rowid DESC LIMIT 1"
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
    """Insert any missing *legacy* pattern definitions and return ``name -> id``.

    This never deletes or renames an existing row, so the legacy baseline stays
    queryable alongside the algebra ``q:`` patterns.
    """
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


def ensure_query_patterns(conn: sqlite3.Connection) -> dict[str, int]:
    """Register the algebra query vocabulary as ``q:<name>`` patterns.

    Returns ``q:name -> id``; legacy rows are untouched.
    """
    for query in QUERIES:
        conn.execute(
            "INSERT OR IGNORE INTO patterns (name, description, pattern_json, version) "
            "VALUES (?, ?, ?, ?)",
            (
                f"{QUERY_PREFIX}{query.name}",
                query.description,
                json.dumps(query.rule, sort_keys=True),
                query.version,
            ),
        )
    rows = conn.execute(
        "SELECT id, name FROM patterns WHERE name LIKE ? ORDER BY id",
        (f"{QUERY_PREFIX}%",),
    ).fetchall()
    return {row["name"]: int(row["id"]) for row in rows}


# ---------------------------------------------------------------------------
# Legacy persistence (unchanged behaviour)
# ---------------------------------------------------------------------------


def _write_predicates(
    conn: sqlite3.Connection,
    import_id: str,
    predicates: dict[int, list[CardPredicate]],
) -> int:
    rows = [
        (
            import_id, card.card_id, card.face_index, card.predicate,
            card.as_params_json(), card.as_evidence_json(), card.confidence,
        )
        for card in (p for preds in predicates.values() for p in preds)
    ]
    conn.executemany(
        "INSERT INTO card_predicates (import_id, card_id, face_index, predicate, "
        "params_json, evidence_json, confidence) VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


def _write(
    conn: sqlite3.Connection,
    import_id: str,
    contexts: dict[int, CardContext],
    predicates: dict[int, list[CardPredicate]],
    edges: list[Edge],
    pattern_ids: dict[str, int],
) -> None:
    _write_predicates(conn, import_id, predicates)

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


# ---------------------------------------------------------------------------
# Algebra persistence
# ---------------------------------------------------------------------------


def _pair_evidence(
    combo: Combo,
    a: int,
    b: int,
    names: dict[int, str],
) -> tuple[str, list[dict[str, Any]]]:
    """Mechanism + evidence JSON items for one card pair of a cycle."""
    links = [l for l in combo.links
             if {l.src.card_id, l.dst.card_id} == {a, b}]
    items: list[dict[str, Any]] = []
    mechanisms: list[str] = []
    for link in links:
        src = names.get(link.src.card_id, link.src.card_name)
        dst = names.get(link.dst.card_id, link.dst.card_name)
        items.append({
            "kind": link.kind,
            "subkind": link.subkind,
            "motif": link.motif,
            "src": src,
            "dst": dst,
            "predicate": link.motif or link.subkind,
        })
        mechanisms.append(f"{src} {link.kind} {dst} ({link.subkind}/{link.motif})")
    items.append({
        "kind": "cycle",
        "patterns": list(combo.patterns),
        "preconditions": list(combo.preconditions),
        "motifs": list(combo.motifs),
        "infinite": combo.infinite,
        "score": combo.score,
    })
    mechanism = "; ".join(mechanisms) if mechanisms else "cycle link"
    if combo.preconditions:
        mechanism += "  preconditions: " + ", ".join(combo.preconditions)
    return mechanism, items


def _write_algebra(
    conn: sqlite3.Connection,
    import_id: str,
    graph: Graph,
    combos: Sequence[Combo],
    names: dict[int, str],
    query_ids: dict[str, int],
) -> tuple[int, int]:
    """Append algebra interactions (pairwise) + hypotheses (per cycle/query).

    Deduplicated per ordered (pattern, card pair) and per (pattern, card set);
    the highest score wins.  Deterministic row order.
    """
    now = _utc_now()

    # interaction key -> row tuple (score kept at index 6 for comparison).
    interactions: dict[tuple[int, int, int], tuple[Any, ...]] = {}
    hypotheses: dict[tuple[int, tuple[int, ...]], tuple[Any, ...]] = {}

    for combo in combos:
        ids = tuple(sorted(set(combo.card_ids)))
        if len(ids) < 2:
            continue
        for query_name in combo.patterns:
            pattern_id = query_ids.get(f"{QUERY_PREFIX}{query_name}")
            if pattern_id is None:
                continue
            mechanism = combo.mechanism
            if combo.preconditions:
                mechanism += "  preconditions: " + ", ".join(combo.preconditions)
            hkey = (pattern_id, ids)
            previous = hypotheses.get(hkey)
            if previous is None or combo.score > previous[4]:
                hypotheses[hkey] = (
                    import_id, pattern_id, json.dumps(list(ids)), mechanism,
                    round(combo.score, 6), "proposed", now,
                )
            for a, b in combinations(ids, 2):
                src, tgt = (a, b) if a < b else (b, a)
                pair_mechanism, evidence = _pair_evidence(combo, a, b, names)
                evidence_json = json.dumps(evidence, sort_keys=True, default=str)
                ikey = (pattern_id, src, tgt)
                row = (
                    import_id, src, tgt, pattern_id, "mutual", pair_mechanism,
                    round(combo.score, 6), evidence_json, now,
                )
                prior = interactions.get(ikey)
                if prior is None or combo.score > prior[6]:
                    interactions[ikey] = row

    if interactions:
        conn.executemany(
            "INSERT INTO interactions (import_id, source_card_id, target_card_id, "
            "pattern_id, direction, mechanism, score, evidence_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [interactions[key] for key in sorted(interactions)],
        )
    if hypotheses:
        conn.executemany(
            "INSERT INTO combo_hypotheses (import_id, pattern_id, card_ids_json, "
            "mechanism, score, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [hypotheses[key] for key in sorted(hypotheses)],
        )
    return len(interactions), len(hypotheses)


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


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
        mode="legacy",
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


def _algebra_report(
    import_id: str,
    contexts: dict[int, CardContext],
    predicates: dict[int, list[CardPredicate]],
    graph: Graph,
    combos: Sequence[Combo],
    interaction_counts: dict[str, int],
    hypothesis_counts: dict[str, int],
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
    top = [
        {
            "pattern": ",".join(combo.patterns),
            "score": combo.score,
            "cards": " + ".join(combo.cards),
            "mechanism": combo.mechanism,
            "direction": "mutual",
        }
        for combo in combos[:_REPORT_TOP]
    ]
    return OntologyReport(
        import_id=import_id,
        mode="algebra",
        predicate_rows=rows,
        cards_with_predicates=cards_with,
        predicates_by_type=dict(by_type),
        interactions_total=sum(interaction_counts.values()),
        interactions_by_pattern=dict(interaction_counts),
        hypotheses_total=sum(hypothesis_counts.values()),
        hypotheses_by_pattern=dict(hypothesis_counts),
        top_hypotheses=top,
        duration_s=duration_s,
        pool_size=len(graph.by_card),
        links_total=len(graph.links),
        cycles_total=len(combos),
        truncated=graph.truncated or bool(getattr(combos, "truncated", False)),
        truncation_reason=graph.truncation_reason
        or getattr(combos, "truncation_reason", None),
        budget_report=graph.budget_report,
        cost_estimate=graph.cost_estimate,
        warnings=graph.warnings,
    )


def _existing_report(
    conn: sqlite3.Connection,
    import_id: str,
    duration_s: float,
    *,
    mode: str = "algebra",
) -> OntologyReport:
    like = f"{QUERY_PREFIX}%" if mode == "algebra" else None
    pattern_clause = "AND p.name LIKE ?" if like else ""
    params: tuple[Any, ...] = (import_id, like) if like else (import_id,)
    predicate_rows = conn.execute(
        "SELECT predicate, COUNT(*) AS n FROM card_predicates WHERE import_id = ? "
        "GROUP BY predicate",
        (import_id,),
    ).fetchall()
    interactions = conn.execute(
        "SELECT p.name AS name, COUNT(*) AS n FROM interactions i "
        "JOIN patterns p ON p.id = i.pattern_id WHERE i.import_id = ? "
        f"{pattern_clause} GROUP BY p.name",
        params,
    ).fetchall()
    hypotheses = conn.execute(
        "SELECT p.name AS name, COUNT(*) AS n FROM combo_hypotheses h "
        "JOIN patterns p ON p.id = h.pattern_id WHERE h.import_id = ? "
        f"{pattern_clause} GROUP BY p.name",
        params,
    ).fetchall()
    by_pattern = {row["name"]: int(row["n"]) for row in interactions}
    return OntologyReport(
        import_id=import_id,
        mode=mode,
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
        warnings=("already built; pass force=True to append a new snapshot",),
    )


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def _load_vintage(names: dict[int, str]):
    if DEFAULT_VINTAGE_FORMAT.is_file():
        return VintageLegality.from_forge_format(DEFAULT_VINTAGE_FORMAT)
    return VintageLegality.permissive()


def _resolve_pool(
    sigs: Sequence[AbilitySig],
    names: dict[int, str],
    *,
    pool: Iterable[int] | None,
    broad_pool: bool,
    legality,
) -> tuple[int, ...]:
    if pool is not None:
        return tuple(sorted({int(cid) for cid in pool}))
    if broad_pool:
        vintage = set()
        for cid, name in names.items():
            if legality is None or legality.is_legal(name):
                vintage.add(cid)
        return tuple(sorted(s.card_id for s in sigs if s.card_id in vintage))
    return tight_pool(sigs, names, legality)


def build_ontology(
    store: ExperimentStore,
    import_id: str | None = None,
    *,
    force: bool = False,
    apply_caps: bool = False,
    mode: str = "algebra",
    pool: Iterable[int] | None = None,
    broad_pool: bool = False,
    options: LinkOptions | None = None,
    max_len: int = 3,
    max_cycles: int = 1_000_000,
    max_seconds: float | None = DEFAULT_MAX_SECONDS,
    max_steps: int | None = DEFAULT_MAX_STEPS,
    allow_over_budget: bool = False,
) -> OntologyReport:
    """Build the interaction ontology for one corpus import.

    ``mode="algebra"`` (default) persists the cycle-query vocabulary and the
    cycles/links derived from the interaction algebra.  ``mode="legacy"``
    reproduces the original pattern-registry build (the baseline).  Both are
    append-only and share ``card_predicates``; the algebra path skips writing
    predicates when they already exist for the import.

    ``options`` is the :class:`~.links.LinkOptions` for the algebra path
    (defaults to the documented safe preset).  ``pool`` / ``broad_pool``
    override the tight loop-machinery default.  ``max_seconds`` / ``max_steps``
    bound the graph build and search; ``allow_over_budget`` permits configs
    above the safe thresholds (with a warning).
    """
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES!r}, got {mode!r}")
    started = time.monotonic()
    conn = store._conn
    with store_module._DB_LOCK:
        if import_id is None:
            import_id = latest_import_id(conn)
        if import_id is None:
            raise ValueError("no corpus import found; run combo-import-cards first")

        predicates_exist = conn.execute(
            "SELECT COUNT(*) AS n FROM card_predicates WHERE import_id = ?",
            (import_id,),
        ).fetchone()["n"] > 0

        if mode == "algebra":
            existing = conn.execute(
                "SELECT COUNT(*) AS n FROM interactions i "
                "JOIN patterns p ON p.id = i.pattern_id "
                "WHERE i.import_id = ? AND p.name LIKE ?",
                (import_id, f"{QUERY_PREFIX}%"),
            ).fetchone()["n"]
            if existing and not force:
                return _existing_report(conn, import_id, time.monotonic() - started,
                                        mode=mode)
        elif predicates_exist and not force:
            return _existing_report(conn, import_id, time.monotonic() - started,
                                    mode=mode)

        contexts, effects_by_card = load_contexts(conn, import_id)
        predicates = {
            card_id: extract_card_predicates(context, effects_by_card.get(card_id, []))
            for card_id, context in contexts.items()
        }

        if mode == "legacy":
            views = {
                card_id: CardView.build(
                    context, predicates.get(card_id, []),
                    effects_by_card.get(card_id, []),
                )
                for card_id, context in contexts.items()
            }
            built_edges = build_edges(views, apply_caps=apply_caps)
            with conn:
                pattern_ids = ensure_patterns(conn)
                _write(conn, import_id, contexts, predicates, built_edges, pattern_ids)
            return _report(import_id, contexts, predicates, built_edges,
                           time.monotonic() - started)

        # -- algebra path --------------------------------------------------
        sigs = build_signatures(contexts, effects_by_card)
        names = {cid: ctx.name for cid, ctx in contexts.items()}
        legality = _load_vintage(names)
        chosen_pool = _resolve_pool(
            sigs, names, pool=pool, broad_pool=broad_pool, legality=legality
        )
        weights = load_motif_weights(conn)
        opts = options or LinkOptions.safe()
        graph, combos = discover_combos(
            sigs, names=names, legality=legality, weights=weights,
            pool=chosen_pool, options=opts, max_len=max_len,
            max_cycles=max_cycles, max_seconds=max_seconds, max_steps=max_steps,
            allow_over_budget=allow_over_budget,
        )
        with conn:
            ensure_patterns(conn)
            query_ids = ensure_query_patterns(conn)
            if not predicates_exist or force:
                _write_predicates(conn, import_id, predicates)
            interaction_counts = Counter()
            hypothesis_counts = Counter()
            _write_algebra(
                conn, import_id, graph, combos, graph.names, query_ids
            )
            for combo in combos:
                for pname in combo.patterns:
                    hypothesis_counts[f"{QUERY_PREFIX}{pname}"] += 1
            for name, pattern_id in query_ids.items():
                interaction_counts[name] = conn.execute(
                    "SELECT COUNT(*) AS n FROM interactions "
                    "WHERE import_id = ? AND pattern_id = ?",
                    (import_id, pattern_id),
                ).fetchone()["n"]
        return _algebra_report(
            import_id, contexts, predicates, graph, combos,
            dict(interaction_counts), dict(hypothesis_counts),
            time.monotonic() - started,
        )


__all__ = [
    "MODES",
    "OntologyReport",
    "QUERY_PREFIX",
    "build_ontology",
    "ensure_patterns",
    "ensure_query_patterns",
    "latest_import_id",
]
