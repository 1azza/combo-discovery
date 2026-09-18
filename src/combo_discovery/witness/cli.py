"""``combo-witness`` CLI surface.

Split out of ``combo_discovery.witness``; behaviour is unchanged.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ..env import ForgeEnvClient
from .driver import DEFAULT_WITNESS_DECKS, run_witness
from .persist import persist_witness, start_witness_recording
from .policy import WitnessPolicy
from .scenario import LinkPlan, build_scenario, synthetic_cycle

# ---------------------------------------------------------------------------
# Candidate loading + CLI
# ---------------------------------------------------------------------------


@dataclass
class Candidate:
    """A combo hypothesis resolved to card names and a synthetic line."""

    cards: tuple[str, ...]
    card_ids: tuple[int, ...] = ()
    kind: str = "pair"
    key: str = ""
    pattern: str = ""
    mechanism: str = ""
    infinite: bool = False
    type_lines: tuple[str, ...] = ()
    oracle_texts: tuple[str, ...] = ()

    @property
    def links(self) -> list[LinkPlan]:
        return synthetic_cycle(self.cards)


def _parse_card_ids(raw: Any) -> list[int]:
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    if isinstance(raw, (list, tuple)):
        ids: list[int] = []
        for value in raw:
            try:
                ids.append(int(value))
            except (TypeError, ValueError):
                continue
        return ids
    return []


def load_candidate(
    store: Any, *, candidate: str | None = None, card: str | None = None
) -> Candidate:
    """Resolve ``--candidate`` (hypothesis id) or ``--card`` (name) from the DB.

    Raises ``LookupError`` when nothing matches.  The returned candidate uses a
    synthetic ping-pong line; callers with the ontology graph should build the
    real ``Combo`` instead.
    """
    conn = store._conn
    import_id: str | None = None
    row: Any = None
    if candidate is not None:
        row = conn.execute(
            "SELECT h.id, h.import_id, h.card_ids_json, h.mechanism, p.name AS pattern "
            "FROM combo_hypotheses h LEFT JOIN patterns p ON p.id = h.pattern_id "
            "WHERE h.id = ?",
            (int(candidate),),
        ).fetchone()
        if row is None:
            raise LookupError(f"no combo_hypotheses row with id={candidate}")
        import_id = row["import_id"]
    elif card is not None:
        normal = str(card).strip().lower()
        rows = conn.execute(
            "SELECT h.id, h.import_id, h.card_ids_json, h.mechanism, p.name AS pattern "
            "FROM combo_hypotheses h LEFT JOIN patterns p ON p.id = h.pattern_id "
            "ORDER BY h.score DESC"
        ).fetchall()
        hit: Any = None
        for candidate_row in rows:
            ids = _parse_card_ids(candidate_row["card_ids_json"])
            names = _names_for_ids(conn, candidate_row["import_id"], ids)
            if normal in {n.lower() for n in names}:
                hit = candidate_row
                break
        if hit is None:
            raise LookupError(f"no combo hypothesis contains card {card!r}")
        row = hit
        import_id = row["import_id"]
    else:
        raise ValueError("load_candidate needs candidate=<id> or card=<name>")

    ids = _parse_card_ids(row["card_ids_json"])
    names = _names_for_ids(conn, import_id, ids)
    type_lines = _type_lines_for_ids(conn, import_id, ids)
    oracle_texts = _oracle_texts_for_ids(conn, import_id, ids)
    return Candidate(
        cards=tuple(names),
        card_ids=tuple(ids),
        kind="pair" if len(names) <= 2 else "cycle",
        key=str(row["id"]),
        pattern=str(row["pattern"] or ""),
        mechanism=str(row["mechanism"] or ""),
        type_lines=tuple(type_lines),
        oracle_texts=tuple(oracle_texts),
    )


def _names_for_ids(conn: Any, import_id: str | None, ids: Sequence[int]) -> list[str]:
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    sql = (
        f"SELECT id, name FROM cards WHERE id IN ({placeholders}) "  # noqa: S608
    )
    params: list[Any] = [int(i) for i in ids]
    if import_id is not None:
        sql += "AND import_id = ?"
        params.append(import_id)
    rows = conn.execute(sql, params).fetchall()
    by_id = {int(r["id"]): str(r["name"]) for r in rows}
    return [by_id[i] for i in ids if i in by_id]


def _type_lines_for_ids(
    conn: Any, import_id: str | None, ids: Sequence[int]
) -> list[str]:
    """Type lines for ``ids`` in the same order/filter as :func:`_names_for_ids`."""
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    sql = (
        f"SELECT id, type_line FROM cards WHERE id IN ({placeholders}) "  # noqa: S608
    )
    params: list[Any] = [int(i) for i in ids]
    if import_id is not None:
        sql += "AND import_id = ?"
        params.append(import_id)
    rows = conn.execute(sql, params).fetchall()
    by_id = {int(r["id"]): str(r["type_line"] or "") for r in rows}
    return [by_id[i] for i in ids if i in by_id]


def _oracle_texts_for_ids(
    conn: Any, import_id: str | None, ids: Sequence[int]
) -> list[str]:
    """Oracle texts for ``ids`` in the same order/filter as :func:`_names_for_ids`."""
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    sql = (
        f"SELECT id, oracle_text FROM cards WHERE id IN ({placeholders}) "  # noqa: S608
    )
    params: list[Any] = [int(i) for i in ids]
    if import_id is not None:
        sql += "AND import_id = ?"
        params.append(import_id)
    rows = conn.execute(sql, params).fetchall()
    by_id = {int(r["id"]): str(r["oracle_text"] or "") for r in rows}
    return [by_id[i] for i in ids if i in by_id]


def _parse_seeds(raw: str) -> list[int]:
    return [int(part) for part in str(raw).split(",") if part.strip()]


def _parse_decks(raw: str | None) -> list[tuple[str, str]] | None:
    if not raw:
        return None
    decks: list[tuple[str, str]] = []
    for part in str(raw).split(","):
        if not part.strip():
            continue
        if "=" in part:
            name, path = part.split("=", 1)
        else:
            name, path = part.strip(), part.strip()
        decks.append((name.strip(), path.strip()))
    return decks or None


def main(argv: list[str] | None = None) -> int:
    """``combo-witness`` CLI: stage/verify a candidate combo loop."""
    import argparse
    from pathlib import Path

    from ..store import ExperimentStore

    parser = argparse.ArgumentParser(
        prog="combo-witness",
        description="Scenario-driven witness search over a candidate combo.",
    )
    parser.add_argument("--db", default="./research.db", help="SQLite research DB")
    parser.add_argument("--candidate", default=None, help="combo_hypotheses.id")
    parser.add_argument("--card", default=None, help="card name (find a hypothesis)")
    parser.add_argument("--seeds", default="1", help="comma-separated seeds")
    parser.add_argument("--max-iterations", type=int, default=4)
    parser.add_argument("--max-decisions", type=int, default=256)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="print the canonical scenario without contacting a harness",
    )
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=50051)
    parser.add_argument(
        "--decks", default=None,
        help="comma-separated name=path deck specs for a live run",
    )
    parser.add_argument("--persist", action="store_true", help="append the result to the DB")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)
    seeds = _parse_seeds(args.seeds)

    store = ExperimentStore(Path(args.db))
    try:
        combo = load_candidate(store, candidate=args.candidate, card=args.card)
    finally:
        store.close()

    scenario = build_scenario(combo)
    if args.dry_run:
        if args.json:
            print(json.dumps(scenario.canonical(), indent=2, sort_keys=True))
        else:
            print(f"candidate {combo.key} [{combo.pattern}]: {' + '.join(combo.cards)}")
            print(f"scenario hash: {scenario.scenario_hash()}")
            print(json.dumps(scenario.canonical(), indent=2, sort_keys=True))
        return 0

    decks = _parse_decks(args.decks) or DEFAULT_WITNESS_DECKS
    policy = WitnessPolicy(combo)

    # With --persist the witness_runs row is started *before* the run and a
    # recorder streams each observation into witness_observations as it is
    # captured, so a UI can watch the run live.  The result row is appended at
    # the end (same run id).
    params = {
        "max_iterations": int(args.max_iterations),
        "max_decisions": int(args.max_decisions),
    }
    store = ExperimentStore(Path(args.db)) if args.persist else None
    run_id: int | None = None
    recorder = None
    if store is not None:
        run_id, recorder = start_witness_recording(
            store,
            scenario=scenario,
            seeds=seeds,
            params=params,
            candidate_key=combo.key,
            card_names=combo.cards,
        )

    try:
        with ForgeEnvClient(host=args.host, port=args.port) as client:
            client.connect()
            result = run_witness(
                client,
                scenario,
                policy,
                seeds=seeds,
                max_iterations=args.max_iterations,
                max_decisions=args.max_decisions,
                decks=decks,
                candidate_kind=combo.kind,
                candidate_key=combo.key,
                card_names=combo.cards,
                infinite=combo.infinite,
                recorder=recorder,
            )
        result_id: int | None = None
        if store is not None:
            run_id, result_id = persist_witness(
                store, result, params=params, run_id=run_id
            )
    finally:
        if store is not None:
            store.close()

    if args.json:
        print(json.dumps({
            "verdict": result.verdict,
            "iterations": result.iterations,
            "signatures": result.signatures,
            "resource_deltas": result.resource_deltas,
            "state_hash_before": result.state_hash_before,
            "state_hash_after": result.state_hash_after,
            "event_start_seq": result.event_start_seq,
            "event_end_seq": result.event_end_seq,
            "evidence": result.evidence,
            "error": result.error,
            "run_id": run_id,
            "result_id": result_id,
        }, indent=2, sort_keys=True))
    else:
        print(f"verdict: {result.verdict}  iterations: {result.iterations}")
        if result.error:
            print(f"error: {result.error}")
        for i, delta in enumerate(result.resource_deltas):
            changed = {k: v for k, v in delta.items() if v}
            print(f"  iteration {i}: {result.signatures[i][:12]}  {changed}")
        if run_id is not None:
            print(f"persisted: run={run_id} result={result_id}")
    return 0
