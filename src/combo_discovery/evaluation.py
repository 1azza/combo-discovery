"""Evaluation harness: our ontology proposals vs known-combo ground truth.

Tier A ground truth is Commander Spellbook (``known_combo_pairs``); Tier B is
independent deck co-occurrence (``observed_pairs``, unpopulated this round).

Verdicts (per proposed pair):

* ``known_pair`` — the pair is an exact Spellbook 2-card variant;
* ``contained_in_known`` — the pair appears inside a larger known variant;
* ``unmatched`` — not in Tier A (a *candidate*, not a claim of novelty);
* ``missed`` — a known exact variant we did not propose.

Precision is defined explicitly as ``known_pair / proposed`` (the strict,
exact-variant definition); ``contained_in_known`` is reported separately and
folded into ``precision_incl_partial``.  Recall is
``known_pair / (known_pair + missed)``.

Novelty policy: :func:`novelty_status` never returns "novel".  Absent from
Tier A and Tier B it returns ``needs_second_source``; only a Tier-B hit makes a
pair ``observed``.  A discovery claim requires two independent sources.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import store as store_module
from .cards import utc_now as _utc_now
from .corpus.names import normalize_card_name, pair_hash
from .store import ExperimentStore

SOURCE_A = "commander_spellbook"


# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PairVerdict:
    verdict: str
    pattern: str | None
    source_card_id: int
    target_card_id: int
    source_name: str
    target_name: str
    pair_hash: str
    score: float = 0.0
    known_combo_id: int | None = None
    evidence_json: str | None = None
    hypothesis_id: int | None = None


@dataclass
class Metrics:
    scope: str
    key: str
    pattern: str | None
    true_positives: int = 0
    partials: int = 0
    false_positives: int = 0
    missed: int = 0
    precision: float = 0.0
    recall: float = 0.0
    f1: float = 0.0
    precision_incl_partial: float = 0.0
    precision_at_k: dict[int, float] = field(default_factory=dict)
    #: Row-level proposal count before unique-pair deduplication (a pair matched
    #: by several queries is counted once per query).  ``true_positives`` etc. are
    #: unique-pair numbers; this preserves the raw surface size for reporting.
    raw_proposals: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope, "key": self.key, "pattern": self.pattern,
            "true_positives": self.true_positives, "partials": self.partials,
            "false_positives": self.false_positives, "missed": self.missed,
            "precision": self.precision, "recall": self.recall, "f1": self.f1,
            "precision_incl_partial": self.precision_incl_partial,
            "precision_at_k": {str(k): v for k, v in self.precision_at_k.items()},
            "raw_proposals": self.raw_proposals,
        }


@dataclass
class MetricsReport:
    aggregate: Metrics
    by_pattern: dict[str, Metrics] = field(default_factory=dict)
    by_card: dict[str, Metrics] = field(default_factory=dict)
    k_values: tuple[int, ...] = ()


@dataclass
class DiagnosticReport:
    false_positive_clusters: list[tuple[str, int]] = field(default_factory=list)
    miss_clusters: list[tuple[str, int]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _latest_import(conn: sqlite3.Connection, *, source: str | None = None) -> str | None:
    if source is None:
        row = conn.execute(
            "SELECT import_id FROM interactions ORDER BY id DESC LIMIT 1"
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT import_id FROM import_runs WHERE scryfall_source = ? "
            "ORDER BY rowid DESC LIMIT 1",
            (source,),
        ).fetchone()
    return row["import_id"] if row is not None else None


def _load_card_names(conn: sqlite3.Connection) -> dict[int, str]:
    return {
        int(row["id"]): str(row["normalized_name"] or "")
        for row in conn.execute("SELECT id, normalized_name FROM cards")
    }


def _load_known_hashes(
    conn: sqlite3.Connection, known_import_id: str | None
) -> tuple[set[str], set[str], dict[str, int]]:
    any_set: set[str] = set()
    full_set: set[str] = set()
    combo_by_hash: dict[str, int] = {}
    sql = "SELECT pair_hash, is_full_variant, combo_id FROM known_combo_pairs"
    params: tuple[Any, ...] = ()
    if known_import_id:
        sql += " WHERE import_id = ?"
        params = (known_import_id,)
    for row in conn.execute(sql, params):
        h = str(row["pair_hash"])
        any_set.add(h)
        combo_by_hash.setdefault(h, int(row["combo_id"]))
        if int(row["is_full_variant"]):
            full_set.add(h)
    return any_set, full_set, combo_by_hash


def _load_missed(
    conn: sqlite3.Connection,
    known_import_id: str | None,
    proposed_hashes: set[str],
) -> list[PairVerdict]:
    """Known exact 2-card variants (Tier A) that we did not propose."""
    sql = (
        "SELECT kc.id AS combo_id, kcc.normalized_name AS name "
        "FROM known_combos kc JOIN known_combo_cards kcc ON kcc.combo_id = kc.id "
        "WHERE kc.n_uses = 2 AND kc.n_requires = 0 AND kcc.role = 'use'"
    )
    params: tuple[Any, ...] = ()
    if known_import_id:
        sql += " AND kc.import_id = ?"
        params = (known_import_id,)
    uses: dict[int, list[str]] = defaultdict(list)
    for row in conn.execute(sql + " ORDER BY kc.id, kcc.id", params):
        uses[int(row["combo_id"])].append(str(row["name"]))
    missed: list[PairVerdict] = []
    for combo_id, names in uses.items():
        names = sorted({n for n in names if n})
        if len(names) != 2:
            continue
        h = pair_hash(names[0], names[1])
        if h in proposed_hashes:
            continue
        missed.append(PairVerdict(
            verdict="missed", pattern=None, source_card_id=0, target_card_id=0,
            source_name=names[0], target_name=names[1], pair_hash=h,
            known_combo_id=combo_id,
        ))
    return missed


def _load_proposals(
    conn: sqlite3.Connection,
    ontology_import_id: str | None,
    pattern: str | None,
    patterns: Iterable[str] | None = None,
) -> list[tuple[int, str, int, int, float]]:
    """Proposal pairs from ``interactions`` (richest: carries evidence_json).

    ``patterns`` (if given) restricts to a set of pattern names; this is how the
    legacy registry and the algebra ``q:*`` vocabulary are scored separately
    when both live under the same corpus import.
    """
    sql = (
        "SELECT i.id AS id, p.name AS pattern, i.source_card_id AS source_card_id, "
        "i.target_card_id AS target_card_id, i.score AS score "
        "FROM interactions i JOIN patterns p ON p.id = i.pattern_id"
    )
    clauses = []
    params: list[Any] = []
    if ontology_import_id:
        clauses.append("i.import_id = ?")
        params.append(ontology_import_id)
    if pattern:
        clauses.append("p.name = ?")
        params.append(pattern)
    if patterns is not None:
        names = sorted({str(name) for name in patterns})
        if not names:
            return []
        placeholders = ",".join("?" for _ in names)
        clauses.append(f"p.name IN ({placeholders})")  # noqa: S608 - fixed placeholders
        params.extend(names)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY i.id"
    return [
        (
            int(row["id"]), str(row["pattern"]), int(row["source_card_id"]),
            int(row["target_card_id"]), float(row["score"] or 0.0),
        )
        for row in conn.execute(sql, tuple(params))
    ]


def classify_pairs(
    store: ExperimentStore,
    *,
    card: str | None = None,
    pattern: str | None = None,
    patterns: Iterable[str] | None = None,
    ontology_import_id: str | None = None,
    known_import_id: str | None = None,
) -> list[PairVerdict]:
    """Classify every proposal for a scope and list missed exact known variants.

    ``pattern`` restricts to one pattern; ``patterns`` restricts to a set (used
    to score the legacy registry and the algebra ``q:*`` vocabulary separately).
    """
    conn = store._conn
    with store_module._DB_LOCK:
        ontology_import_id = ontology_import_id or _latest_import(conn)
        known_import_id = known_import_id or _latest_import(conn, source=SOURCE_A)
        card_names = _load_card_names(conn)
        proposals = _load_proposals(conn, ontology_import_id, pattern, patterns)
        any_set, full_set, combo_by_hash = _load_known_hashes(conn, known_import_id)

    card_filter_id: int | None = None
    if card:
        target = normalize_card_name(card)
        card_filter_id = next(
            (cid for cid, name in card_names.items() if name == target), None
        )
        if card_filter_id is None:
            return []

    verdicts: list[PairVerdict] = []
    proposed_hashes: set[str] = set()
    for hyp_id, pat, src_id, tgt_id, score in proposals:
        if card_filter_id is not None and card_filter_id not in (src_id, tgt_id):
            continue
        src_name = card_names.get(src_id, "")
        tgt_name = card_names.get(tgt_id, "")
        if not src_name or not tgt_name:
            continue
        h = pair_hash(src_name, tgt_name)
        proposed_hashes.add(h)
        if h in full_set:
            verdict = "known_pair"
        elif h in any_set:
            verdict = "contained_in_known"
        else:
            verdict = "unmatched"
        verdicts.append(PairVerdict(
            verdict=verdict, pattern=pat, source_card_id=src_id, target_card_id=tgt_id,
            source_name=src_name, target_name=tgt_name, pair_hash=h, score=score,
            known_combo_id=combo_by_hash.get(h), hypothesis_id=hyp_id,
        ))

    with store_module._DB_LOCK:
        missed = _load_missed(conn, known_import_id, proposed_hashes)
    if card_filter_id is not None:
        wanted = card_names.get(card_filter_id, "")
        missed = [m for m in missed if wanted in (m.source_name, m.target_name)]
    verdicts.extend(missed)
    return verdicts


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def _dedupe_pairs(subset: list[PairVerdict]) -> list[PairVerdict]:
    """Collapse a pair proposed under several queries into one verdict.

    The interaction row-level aggregate double-counts a pair that matches more
    than one query (a cyclic pair is often both ``q:any_cycle`` and a named
    loop).  Metrics are computed on unique pairs; the occurrence with the best
    score wins, and ties are broken deterministically by the pattern name and
    the ordered card names so the ranking is stable.
    """
    best: dict[str, PairVerdict] = {}
    for verdict in subset:
        current = best.get(verdict.pair_hash)
        key = (
            verdict.score, verdict.pattern or "",
            verdict.source_name, verdict.target_name,
        )
        if current is None:
            best[verdict.pair_hash] = verdict
            continue
        current_key = (
            current.score, current.pattern or "",
            current.source_name, current.target_name,
        )
        if key > current_key:
            best[verdict.pair_hash] = verdict
    return list(best.values())


def _verdict_rank(verdict: PairVerdict) -> tuple[Any, ...]:
    """Deterministic ordering: score descending, then a stable key.

    The stable key makes precision@k reproducible when two pairs share a score
    (the old ``-score`` sort fell back to insertion order).
    """
    return (
        -verdict.score,
        verdict.source_name,
        verdict.target_name,
        verdict.pattern or "",
    )


def _metrics_for(
    subset: list[PairVerdict],
    missed: int,
    *,
    scope: str,
    key: str,
    pattern: str | None,
    k_values: Iterable[int],
) -> Metrics:
    raw_proposals = len(subset)
    subset = _dedupe_pairs(subset)
    tp = sum(1 for v in subset if v.verdict == "known_pair")
    partials = sum(1 for v in subset if v.verdict == "contained_in_known")
    fp = sum(1 for v in subset if v.verdict == "unmatched")
    proposed = len(subset)
    precision = tp / proposed if proposed else 0.0
    recall = tp / (tp + missed) if (tp + missed) else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    ordered = sorted(subset, key=_verdict_rank)
    at_k: dict[int, float] = {}
    for k in k_values:
        top = ordered[:k]
        at_k[k] = (sum(1 for v in top if v.verdict == "known_pair") / len(top)) if top else 0.0
    return Metrics(
        scope=scope, key=key, pattern=pattern, true_positives=tp, partials=partials,
        false_positives=fp, missed=missed, precision=round(precision, 4),
        recall=round(recall, 4), f1=round(f1, 4),
        precision_incl_partial=round((tp + partials) / proposed, 4) if proposed else 0.0,
        precision_at_k={k: round(v, 4) for k, v in at_k.items()},
        raw_proposals=raw_proposals,
    )


def metrics(
    verdicts: list[PairVerdict], *, k_values: Iterable[int] = (10, 25, 50, 100)
) -> MetricsReport:
    """Precision@k, recall and F1 aggregate, per pattern and per card.

    ``missed`` for a pattern/card scope counts known exact variants whose two
    cards both appear in that scope's proposed card universe (the pattern/card
    "saw" both cards but did not propose the pair).  Aggregate ``missed`` is the
    full known exact-variant set not proposed.
    """
    k_values = tuple(k_values)
    proposals = [v for v in verdicts if v.verdict != "missed"]
    missed = [v for v in verdicts if v.verdict == "missed"]

    aggregate = _metrics_for(
        proposals, len(missed), scope="aggregate", key="all", pattern=None, k_values=k_values
    )

    by_pattern: dict[str, Metrics] = {}
    grouped: dict[str, list[PairVerdict]] = defaultdict(list)
    for v in proposals:
        grouped[v.pattern or ""].append(v)
    for pattern_name, subset in grouped.items():
        universe = {v.source_name for v in subset} | {v.target_name for v in subset}
        # A pattern is accountable for a missed known pair when it proposed at
        # least one of the pair's cards (it "saw" the card but missed the combo).
        missed_p = sum(
            1 for m in missed
            if m.source_name in universe or m.target_name in universe
        )
        by_pattern[pattern_name] = _metrics_for(
            subset, missed_p, scope="pattern", key=pattern_name, pattern=pattern_name,
            k_values=k_values,
        )

    by_card_lists: dict[str, list[PairVerdict]] = defaultdict(list)
    for v in proposals:
        by_card_lists[v.source_name].append(v)
        by_card_lists[v.target_name].append(v)
    missed_by_card: Counter[str] = Counter()
    for m in missed:
        missed_by_card[m.source_name] += 1
        missed_by_card[m.target_name] += 1
    by_card: dict[str, Metrics] = {}
    for name in sorted(set(by_card_lists) | set(missed_by_card)):
        by_card[name] = _metrics_for(
            by_card_lists.get(name, []), missed_by_card.get(name, 0),
            scope="card", key=name, pattern=None, k_values=k_values,
        )

    return MetricsReport(
        aggregate=aggregate, by_pattern=by_pattern, by_card=by_card, k_values=k_values
    )


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def _predicates_from_evidence(evidence_json: str | None) -> tuple[str, ...]:
    try:
        evidence = json.loads(evidence_json or "[]")
    except (ValueError, TypeError):
        return ()
    predicates = {
        str(item.get("predicate"))
        for item in evidence
        if isinstance(item, dict) and item.get("predicate")
    }
    return tuple(sorted(predicates))


def diagnostics(
    store: ExperimentStore,
    verdicts: list[PairVerdict],
    *,
    top: int = 20,
    max_fp_samples: int = 50_000,
) -> DiagnosticReport:
    """Cluster false positives and misses so systematic rule bugs stand out.

    False-positive evidence is read from ``interactions`` in batches (up to
    ``max_fp_samples`` false positives) rather than carried in memory.
    """
    fp_ids = [
        v.hypothesis_id for v in verdicts
        if v.verdict == "unmatched" and v.hypothesis_id is not None
    ][:max_fp_samples]
    fp_counter: Counter[str] = Counter()
    conn = store._conn
    with store_module._DB_LOCK:
        for start in range(0, len(fp_ids), 900):
            batch = fp_ids[start:start + 900]
            placeholders = ",".join("?" for _ in batch)
            for row in conn.execute(
                f"SELECT evidence_json FROM interactions WHERE id IN ({placeholders})",  # noqa: S608
                tuple(batch),
            ):
                predicates = _predicates_from_evidence(str(row["evidence_json"] or "[]"))
                fp_counter[" + ".join(predicates) if predicates else "(no predicate evidence)"] += 1

    missed_combos = {
        v.known_combo_id for v in verdicts
        if v.verdict == "missed" and v.known_combo_id is not None
    }
    miss_counter: Counter[str] = Counter()
    if missed_combos:
        with store_module._DB_LOCK:
            for combo_id in missed_combos:
                row = conn.execute(
                    "SELECT produces_json, requires_json FROM known_combos WHERE id = ?",
                    (combo_id,),
                ).fetchone()
                if row is None:
                    continue
                try:
                    produces = json.loads(row["produces_json"] or "[]")
                except (ValueError, TypeError):
                    produces = []
                for item in produces:
                    feature = (
                        (item.get("feature") or {}).get("name")
                        if isinstance(item, dict)
                        else None
                    )
                    if feature:
                        miss_counter[f"produces: {feature}"] += 1
                try:
                    requires = json.loads(row["requires_json"] or "[]")
                except (ValueError, TypeError):
                    requires = []
                for item in requires:
                    template = (
                        (item.get("template") or {}).get("name")
                        if isinstance(item, dict)
                        else None
                    )
                    if template:
                        miss_counter[f"requires: {template}"] += 1

    return DiagnosticReport(
        false_positive_clusters=fp_counter.most_common(top),
        miss_clusters=miss_counter.most_common(top),
    )


# ---------------------------------------------------------------------------
# Novelty policy
# ---------------------------------------------------------------------------


def _hash_present(conn: sqlite3.Connection, table: str, pair_hash_value: str) -> bool:
    if table not in ("known_combo_pairs", "observed_pairs"):
        raise ValueError(table)
    row = conn.execute(
        f"SELECT 1 FROM {table} WHERE pair_hash = ? LIMIT 1",  # noqa: S608
        (pair_hash_value,),
    ).fetchone()
    return row is not None


def novelty_status(store: ExperimentStore, pair) -> str:
    """``known`` | ``observed`` | ``needs_second_source`` -- never ``novel``.

    Two independent sources are required before a pair may be called novel; with
    Tier B unpopulated, anything absent from Tier A is only a candidate.
    """
    if isinstance(pair, PairVerdict):
        h = pair.pair_hash
    else:
        name_a, name_b = pair
        h = pair_hash(normalize_card_name(name_a), normalize_card_name(name_b))
    conn = store._conn
    with store_module._DB_LOCK:
        if _hash_present(conn, "known_combo_pairs", h):
            return "known"
        if _hash_present(conn, "observed_pairs", h):
            return "observed"
    return "needs_second_source"


# ---------------------------------------------------------------------------
# Tier B interface (independent deck co-occurrence; no scraper this round)
# ---------------------------------------------------------------------------


def record_observed_deck(
    store: ExperimentStore,
    *,
    source: str,
    source_id: str | None = None,
    url: str | None = None,
    commander: str | None = None,
    format: str | None = None,
    cards: Iterable[str] = (),
    import_id: str | None = None,
) -> int:
    """Record one independently observed deck and its card pairs.

    This is the Tier-B ingestion interface (planned: Archidekt, MTGTop8).  It
    writes ``observed_decks`` / ``observed_deck_cards`` and expands the deck into
    all card pairs in ``observed_pairs``; no scraper is wired up this round.
    """
    import uuid as _uuid

    conn = store._conn
    with store_module._DB_LOCK:
        if import_id is None:
            import_id = str(_uuid.uuid4())
            conn.execute(
                "INSERT INTO import_runs (import_id, started_at, scryfall_source, notes) "
                "VALUES (?, ?, ?, ?)",
                (import_id, _utc_now(), f"tier_b:{source}",
                 json.dumps({"source": source, "tier": "B"}, sort_keys=True)),
            )
        cur = conn.execute(
            "INSERT INTO observed_decks (source, source_id, url, commander, format, "
            "fetched_at, import_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (source, source_id, url, commander, format, _utc_now(), import_id),
        )
        deck_id = int(cur.lastrowid or 0)
        names: list[str] = []
        card_rows: list[tuple[Any, ...]] = []
        for name in cards:
            normalized = normalize_card_name(name)
            if not normalized:
                continue
            names.append(normalized)
            card_rows.append((deck_id, name, normalized, None, 1))
        if card_rows:
            conn.executemany(
                "INSERT INTO observed_deck_cards (deck_id, card_name, normalized_name, "
                "oracle_id, quantity) VALUES (?, ?, ?, ?, ?)",
                card_rows,
            )
        unique = sorted(set(names))
        pair_rows = [
            (deck_id, pair_hash(unique[i], unique[j]), source, import_id)
            for i in range(len(unique))
            for j in range(i + 1, len(unique))
        ]
        if pair_rows:
            conn.executemany(
                "INSERT INTO observed_pairs (deck_id, pair_hash, source, import_id) "
                "VALUES (?, ?, ?, ?)",
                pair_rows,
            )
        conn.commit()
    return deck_id


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def persist_evaluation(
    store: ExperimentStore,
    report: MetricsReport,
    verdicts: list[PairVerdict],
    *,
    card_filter: str | None = None,
    known_import_id: str | None = None,
    ontology_import_id: str | None = None,
    params: dict[str, Any] | None = None,
    notes: str | None = None,
) -> int:
    """Append one evaluation run + result rows; return the run id."""
    conn = store._conn
    with store_module._DB_LOCK:
        cur = conn.execute(
            "INSERT INTO evaluation_runs (started_at, card_filter, known_import_id, "
            "ontology_import_id, params_json, notes) VALUES (?, ?, ?, ?, ?, ?)",
            (
                _utc_now(), card_filter, known_import_id, ontology_import_id,
                json.dumps(params or {}, sort_keys=True), notes,
            ),
        )
        run_id = int(cur.lastrowid or 0)
        rows: list[tuple[Any, ...]] = []

        def add(metric: Metrics) -> None:
            rows.append((
                run_id, metric.scope, metric.key, metric.pattern,
                metric.true_positives, metric.partials, metric.false_positives,
                metric.missed, metric.precision, metric.recall, metric.f1,
                json.dumps(metric.as_dict(), sort_keys=True),
            ))

        add(report.aggregate)
        for metric in report.by_pattern.values():
            add(metric)
        for metric in report.by_card.values():
            add(metric)
        conn.executemany(
            "INSERT INTO evaluation_results (run_id, scope, key, pattern, "
            "true_positives, partials, false_positives, missed, precision, recall, f1, "
            "details_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    return run_id


# ---------------------------------------------------------------------------
# CLI (thin)
# ---------------------------------------------------------------------------


def _print_metrics(report: MetricsReport, top: int) -> None:
    a = report.aggregate
    print(
        f"aggregate: proposed={a.true_positives + a.partials + a.false_positives} "
        f"known_pair={a.true_positives} contained={a.partials} "
        f"unmatched={a.false_positives} missed={a.missed} "
        f"precision={a.precision:.3f} recall={a.recall:.3f} f1={a.f1:.3f}"
    )
    if a.precision_at_k:
        print("  precision@k: " + "  ".join(
            f"@{k}={v:.3f}" for k, v in sorted(a.precision_at_k.items())
        ))
    print("\nby pattern:")
    for name, m in sorted(report.by_pattern.items()):
        print(
            f"  {name:22} proposed={m.true_positives + m.partials + m.false_positives:>7} "
            f"known={m.true_positives:>6} contained={m.partials:>6} "
            f"unmatched={m.false_positives:>7} missed={m.missed:>5} "
            f"P={m.precision:.3f} R={m.recall:.3f} F1={m.f1:.3f}"
        )
    if top:
        top_cards = sorted(
            report.by_card.items(), key=lambda kv: -(kv[1].true_positives + kv[1].false_positives)
        )[:top]
        print(f"\ntop {top} cards by proposals:")
        for name, m in top_cards:
            print(
                f"  {name:34} proposed={m.true_positives + m.partials + m.false_positives:>6} "
                f"known={m.true_positives:>5} unmatched={m.false_positives:>6} "
                f"missed={m.missed:>4} P={m.precision:.3f} R={m.recall:.3f}"
            )


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="combo-evaluate",
        description="Compare ontology proposals against Commander Spellbook ground truth.",
    )
    parser.add_argument("--db", default="./research.db")
    parser.add_argument("--card", default=None, help="restrict to one card (name)")
    parser.add_argument("--pattern", default=None, help="restrict to one pattern")
    parser.add_argument("--top", type=int, default=20, help="top cards to show")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    parser.add_argument("--no-persist", action="store_true", help="do not write evaluation rows")
    args = parser.parse_args(argv)

    store = ExperimentStore(Path(args.db))
    try:
        started = time.monotonic()
        verdicts = classify_pairs(
            store, card=args.card, pattern=args.pattern
        )
        report = metrics(verdicts)
        diag = diagnostics(store, verdicts)
        if not args.no_persist:
            persist_evaluation(
                store, report, verdicts, card_filter=args.card,
                known_import_id=_latest_import(store._conn, source=SOURCE_A),
                ontology_import_id=_latest_import(store._conn),
                params={"pattern": args.pattern},
            )
    finally:
        store.close()

    if args.json:
        print(json.dumps({
            "aggregate": report.aggregate.as_dict(),
            "by_pattern": {k: v.as_dict() for k, v in report.by_pattern.items()},
            "by_card": {k: v.as_dict() for k, v in report.by_card.items()},
            "diagnostics": {
                "false_positive_clusters": diag.false_positive_clusters,
                "miss_clusters": diag.miss_clusters,
            },
        }, indent=2, sort_keys=True))
        return 0

    _print_metrics(report, args.top)
    if diag.false_positive_clusters:
        print("\nfalse-positive clusters (predicate sets):")
        for label, count in diag.false_positive_clusters:
            print(f"  {count:>7}  {label}")
    if diag.miss_clusters:
        print("\nmiss clusters (known-combo produces/requires):")
        for label, count in diag.miss_clusters:
            print(f"  {count:>7}  {label}")
    print(f"\nduration: {time.monotonic() - started:.2f}s")
    return 0


__all__ = [
    "DiagnosticReport",
    "Metrics",
    "MetricsReport",
    "PairVerdict",
    "SOURCE_A",
    "classify_pairs",
    "diagnostics",
    "main",
    "metrics",
    "novelty_status",
    "persist_evaluation",
    "record_observed_deck",
]
