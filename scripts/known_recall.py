"""Measure witness recall on KNOWN Commander Spellbook combos.

This is the "how good is our verifier, really" instrument. It runs the witness
verifier on *known* exact 2-card combos (not on our own generated candidates) and
reports how many it confirms, split by whether the copy engine is **activated**
(a ``{T}``/tap cost the witness policy can drive) or **triggered**
(attack/ETB/loyalty, which the current policy cannot drive).

The point is to separate two very different failures:

* ``refuted`` / ``no_loop`` on a known combo -> a **false negative** (the verifier
  actively says a real combo does not loop);
* ``inconclusive`` -> we never got a verdict (stall, staging gap, undriveable).

Usage:
    uv run python scripts/known_recall.py --mode home --start 0 --count 20
    uv run python scripts/known_recall.py --mode all  --start 0 --count 50
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

from combo_discovery import batch as B
from combo_discovery import witness as W
from combo_discovery.env import ForgeEnvClient
from combo_discovery.research_config import load_config
from combo_discovery.search import witness_with_search
from combo_discovery.store import ExperimentStore

DEFAULT_DECKS = ["A=decks/goldfish_A.dck", "B=decks/goldfish_B.dck"]

#: A copy engine the policy can drive: an activated ``{T}`` ability that creates
#: a token copy. Triggered engines (attack/ETB/loyalty) are not drivable today.
_ACTIVATED_COPY = re.compile(r"\{t\}.*(create a token|copy)", re.IGNORECASE)


def _is_copy(text: str) -> bool:
    return "create a token" in text and "copy" in text


def _is_untap(text: str) -> bool:
    return "untap" in text


def _engine_kind(a: str, b: str, oracle: dict[str, str]) -> str:
    for x in (a, b):
        text = oracle.get(x, "")
        if _is_copy(text):
            return "activated" if _ACTIVATED_COPY.search(text) else "triggered"
    return "none"


def _is_home_class(a: str, b: str, oracle: dict[str, str]) -> bool:
    """True when the pair is a copy engine plus an untapper (the ``home`` class)."""
    return (
        (_is_copy(oracle.get(a, "")) and _is_untap(oracle.get(b, "")))
        or (_is_copy(oracle.get(b, "")) and _is_untap(oracle.get(a, "")))
    )


def _known_2card_pairs(conn: sqlite3.Connection) -> list[tuple[int, str, str]]:
    two = (
        "select combo_id from known_combo_cards group by combo_id "
        "having sum(role='use') = 2 and sum(role='require') = 0"
    )
    rows = conn.execute(
        "select kcc.combo_id, kcc.normalized_name from known_combo_cards kcc "
        f"join ({two}) t on t.combo_id = kcc.combo_id where kcc.role = 'use'"
    ).fetchall()
    grouped: dict[int, list[str]] = defaultdict(list)
    for combo_id, name in rows:
        grouped[combo_id].append(name)
    return [
        (combo_id, names[0], names[1])
        for combo_id, names in grouped.items()
        if len(names) == 2
    ]


def _max_run_id(db: str) -> int:
    """Highest ``witness_runs.id`` currently in ``db`` (0 when absent/empty)."""
    conn = sqlite3.connect(db)
    try:
        row = conn.execute("select coalesce(max(id), 0) from witness_runs").fetchone()
        return int(row[0] or 0)
    finally:
        conn.close()


def _annotate_run_notes(db: str, keys: list[str], notes: str, min_run_id: int) -> int:
    """Stamp the corpus manifest onto the run rows this sweep just created.

    ``witness_runs.notes`` is empty for instrument runs, so the manifest is not
    otherwise recoverable from the database.  Only rows with ``id > min_run_id``
    (i.e. created after this sweep started) and a ``candidate_key`` in ``keys``
    are touched, and only when their ``notes`` is still empty, so older runs and
    unrelated writers are never clobbered.  No schema change; returns rows updated.
    """
    if not keys:
        return 0
    placeholders = ",".join("?" * len(keys))
    conn = sqlite3.connect(db)
    try:
        cur = conn.execute(
            "UPDATE witness_runs SET notes = ? "
            f"WHERE id > ? AND candidate_key IN ({placeholders}) "
            "AND (notes IS NULL OR notes = '')",
            [notes, int(min_run_id), *keys],
        )
        updated = int(cur.rowcount or 0)
        conn.commit()
        return updated
    finally:
        conn.close()


def _recall_block(results: list[dict]) -> dict:
    """Overall and per-engine recall for the results (loops / tested)."""
    counts = Counter(r["verdict"] for r in results)
    overall = counts.get("loops", 0) / len(results) if results else 0.0
    by_engine: dict[str, dict] = {}
    for engine in ("activated", "triggered", "none"):
        subset = [r for r in results if r["engine"] == engine]
        if not subset:
            continue
        sub_counts = Counter(r["verdict"] for r in subset)
        by_engine[engine] = {
            "n": len(subset),
            "loops": sub_counts.get("loops", 0),
            "recall": round(sub_counts.get("loops", 0) / len(subset), 4),
            "verdicts": dict(sub_counts),
        }
    home = [r for r in results if r.get("home_class")]
    home_block: dict | None = None
    if home:
        hc = Counter(r["verdict"] for r in home)
        home_block = {
            "n": len(home),
            "loops": hc.get("loops", 0),
            "recall": round(hc.get("loops", 0) / len(home), 4),
            "verdicts": dict(hc),
        }
    return {"n": len(results), "loops": counts.get("loops", 0),
            "recall": round(overall, 4), "verdicts": dict(counts),
            "by_engine": by_engine, "home_class": home_block}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="known-recall",
        description="Witness recall on known exact 2-card combos.",
    )
    parser.add_argument("--db", default="research.db")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=50051)
    parser.add_argument("--mode", choices=("home", "all"), default="home")
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--decks", default=",".join(DEFAULT_DECKS))
    parser.add_argument("--out", default=None)
    parser.add_argument(
        "--search", action="store_true",
        help="route inconclusive runs through the bounded search fallback",
    )
    parser.add_argument(
        "--persist", action="store_true",
        help="stream observations + verdicts into the witness tables (off by default)",
    )
    parser.add_argument(
        "--workers", type=int, default=1,
        help="harness workers (1 = today's serial path; N uses ports port..port+N-1)",
    )
    parser.add_argument(
        "--spawn", action="store_true",
        help="spawn the worker harness servers before the sweep (implies --workers>1)",
    )
    parser.add_argument(
        "--note", default="",
        help="free-text label stored in the manifest and the run-row notes",
    )
    args = parser.parse_args(argv)
    # Engine metadata for persisted runs (research.toml falls back to defaults).
    cfg = load_config()

    decks = []
    for part in args.decks.split(","):
        name, _, path = part.partition("=")
        decks.append((name or path, os.path.abspath(path or name)))

    conn = sqlite3.connect(args.db)
    oracle = {
        r[0]: (r[1] or "")
        for r in conn.execute("select normalized_name, lower(oracle_text) from cards")
    }
    type_lines = {
        r[0]: (r[1] or "")
        for r in conn.execute("select normalized_name, type_line from cards")
    }
    display = {
        r[0]: r[1]
        for r in conn.execute("select normalized_name, name from cards")
    }

    selected = []
    for combo_id, a, b in _known_2card_pairs(conn):
        if a not in oracle or b not in oracle:
            continue
        if args.mode == "home" and not _is_home_class(a, b, oracle):
            continue
        selected.append((combo_id, a, b))
    selected.sort()
    batch = selected[args.start:args.start + args.count]
    total_available = len(selected)
    print(
        f"mode={args.mode} total={total_available} "
        f"running {args.start}..{args.start + len(batch)}"
    )

    out_path = Path(args.out) if args.out else Path(
        f"/tmp/opencode/known_recall_{args.mode}_{args.start}.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # The manifest records *which* corpus slice this run covered.  It is written
    # into the JSON output and (when persisting) stamped onto the run rows' notes,
    # so a later reader can reconstruct the run without guesswork.
    started_at = datetime.now(UTC).isoformat()
    max_run_id_before = _max_run_id(args.db) if args.persist else 0
    manifest: dict = {
        "script": "known_recall",
        "mode": args.mode,
        "start": args.start,
        "count": len(batch),
        "requested_count": args.count,
        "total_available": total_available,
        "decks": [{"name": name, "path": path} for name, path in decks],
        "workers": args.workers,
        "spawn": bool(args.spawn),
        "persist": bool(args.persist),
        "search": bool(args.search),
        "ports": [args.port + i for i in range(max(1, args.workers))],
        "engine_commit": cfg.engine_commit,
        "proto_version": cfg.proto_version,
        "started_at": started_at,
        "wall_seconds": None,
        "note": args.note,
        "max_run_id_before": max_run_id_before,
        "run_rows_annotated": 0,
    }

    # Only touch the database when asked: the default path stays write-free.
    # ``--workers 1`` (the default) keeps the original serial code path exactly.
    store = (
        ExperimentStore(Path(args.db))
        if (args.persist and args.workers <= 1)
        else None
    )
    results: list[dict] = []

    def _write_out() -> None:
        out_path.write_text(
            json.dumps({"manifest": manifest, "results": results}, indent=1)
        )

    wall_seconds: float | None = None
    try:
        if args.workers <= 1:
            with ForgeEnvClient(host=args.host, port=args.port) as client:
                client.connect()
                t0 = time.monotonic()
                for combo_id, a, b in batch:
                    combo = W.Candidate(
                        cards=(display.get(a, a), display.get(b, b)),
                        type_lines=(type_lines.get(a, ""), type_lines.get(b, "")),
                        kind="pair", key=str(combo_id), pattern="known_pair",
                    )
                    scenario = W.build_scenario(combo)
                    policy = W.WitnessPolicy(combo)
                    engine = _engine_kind(a, b, oracle)
                    run_id = None
                    recorder = None
                    if store is not None:
                        run_id, recorder = W.start_witness_recording(
                            store, scenario=scenario, seeds=[1],
                            params={"max_iterations": 6, "max_decisions": 256},
                            engine_commit=cfg.engine_commit,
                            proto_version=cfg.proto_version,
                            candidate_key=combo.key, card_names=combo.cards,
                        )
                    persisted_res: W.WitnessResult | None = None
                    try:
                        if args.search:
                            # witness_with_search runs two games and does not thread a
                            # recorder through, so its observations are persisted in
                            # bulk at the end (below) rather than streamed live.
                            res = witness_with_search(
                                client, scenario, combo,
                                target=combo.cards[0], seeds=[1], max_iterations=6,
                                max_decisions=256, decks=decks,
                                search_nodes=200, search_depth=60,
                            )
                            diag = (res.evidence or {}).get("diagnostics", {})
                        else:
                            res = W.run_witness(
                                client, scenario, policy,
                                seeds=[1], max_iterations=6, max_decisions=256,
                                decks=decks,
                                candidate_kind=combo.kind, candidate_key=combo.key,
                                card_names=combo.cards, infinite=combo.infinite,
                                recorder=recorder,
                            )
                            diag = policy.diagnostics()
                        persisted_res = res
                        evidence = res.evidence or {}
                        record = {
                            "combo_id": combo_id,
                            "cards": [display.get(a, a), display.get(b, b)],
                            "engine": engine,
                            "home_class": _is_home_class(a, b, oracle),
                            "verdict": res.verdict,
                            "executed": int(diag.get("executed_actions", 0)),
                            "reason": (
                                evidence.get("kind") or evidence.get("reason")
                                or res.error
                            ),
                        }
                    except Exception as exc:  # noqa: BLE001
                        record = {
                            "combo_id": combo_id,
                            "cards": [display.get(a, a), display.get(b, b)],
                            "engine": engine,
                            "home_class": _is_home_class(a, b, oracle),
                            "verdict": "error",
                            "executed": 0,
                            "reason": str(exc),
                        }
                        if store is not None and run_id is not None:
                            # Never leave the run row without a result.
                            W.persist_error_result(
                                store, run_id, scenario=scenario, error=exc,
                                candidate_kind=combo.kind, candidate_key=combo.key,
                                card_names=combo.cards,
                            )
                            run_id = None
                    if store is not None and run_id is not None and persisted_res is not None:
                        W.persist_witness(store, persisted_res, run_id=run_id)
                    results.append(record)
                    # survive interruptions
                    _write_out()
                    print(
                        f"  {combo_id:7d} [{engine:10s}] {record['verdict']:12s} "
                        f"exec={record['executed']:2d} {record['cards'][0]} + "
                        f"{record['cards'][1]}  {record['reason']}"
                    )
                wall_seconds = time.monotonic() - t0
        else:
            if args.search:
                parser.error("--search cannot be combined with --workers > 1")
            batch_pairs = [
                (display.get(a, a), display.get(b, b)) for _cid, a, b in batch
            ]
            # Mirror the serial Candidate exactly: type lines only (the serial
            # path deliberately passes no oracle_texts) and the combo id as key.
            # Without this, graveyard-gated cards (delirium) would stage a
            # supporting graveyard only on the parallel path and flip verdicts.
            normalized = {name: norm for norm, name in display.items()}
            combo_by_pair = {
                (display.get(a, a), display.get(b, b)): combo_id
                for combo_id, a, b in batch
            }

            def _known_candidate(engine: str, partner: str) -> W.Candidate:
                n1 = normalized.get(engine, engine)
                n2 = normalized.get(partner, partner)
                return W.Candidate(
                    cards=(engine, partner),
                    type_lines=(type_lines.get(n1, ""), type_lines.get(n2, "")),
                    kind="pair",
                    key=str(combo_by_pair.get((engine, partner), "")),
                    pattern="known_pair",
                )

            t0 = time.monotonic()
            records = B.run_batch(
                batch_pairs,
                workers=args.workers,
                host=args.host,
                base_port=args.port,
                spawn=args.spawn,
                db=args.db,
                persist=args.persist,
                decks=decks,
                seeds=[1],
                max_iterations=6,
                max_decisions=256,
                candidate_builder=_known_candidate,
            )
            wall_seconds = time.monotonic() - t0
            for (combo_id, a, b), rec in zip(batch, records, strict=True):
                record = {
                    "combo_id": combo_id,
                    "cards": [display.get(a, a), display.get(b, b)],
                    "engine": _engine_kind(a, b, oracle),
                    "home_class": _is_home_class(a, b, oracle),
                    "verdict": rec["verdict"],
                    "executed": int(rec.get("executed", 0)),
                    "reason": rec.get("reason") or rec.get("error"),
                }
                results.append(record)
                _write_out()
                print(
                    f"  {combo_id:7d} [{record['engine']:10s}] "
                    f"{record['verdict']:12s} exec={record['executed']:2d} "
                    f"{record['cards'][0]} + {record['cards'][1]}  "
                    f"{record['reason']}"
                )
    finally:
        if store is not None:
            store.close()

    # Finalise the manifest, then stamp it onto the run rows this sweep created.
    manifest["finished_at"] = datetime.now(UTC).isoformat()
    manifest["wall_seconds"] = round(wall_seconds, 3) if wall_seconds is not None else None
    manifest["recall"] = _recall_block(results)
    if args.persist:
        note = {k: v for k, v in manifest.items() if k != "run_rows_annotated"}
        manifest["run_rows_annotated"] = _annotate_run_notes(
            args.db,
            [str(combo_id) for combo_id, _a, _b in batch],
            json.dumps(note, sort_keys=True),
            max_run_id_before,
        )
        manifest["max_run_id_after"] = _max_run_id(args.db)
    _write_out()

    counts = Counter(r["verdict"] for r in results)
    print(
        f"\nSUMMARY mode={args.mode} n={len(results)} verdicts={dict(counts)} "
        f"drove={sum(1 for r in results if r['executed'] > 0)} "
        f"wall={manifest['wall_seconds']}s"
    )
    for engine in ("activated", "triggered", "none"):
        subset = [r for r in results if r["engine"] == engine]
        if subset:
            sub_counts = Counter(r["verdict"] for r in subset)
            recall = sub_counts.get("loops", 0) / len(subset)
            print(f"  engine={engine:10s} n={len(subset):3d} {dict(sub_counts)} "
                  f"recall={recall:.0%}")
    print(
        f"manifest: mode={manifest['mode']} start={manifest['start']} "
        f"count={manifest['count']} total_available={manifest['total_available']} "
        f"workers={manifest['workers']} spawn={manifest['spawn']} "
        f"run_rows_annotated={manifest['run_rows_annotated']}"
    )
    print("wrote", out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
