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
from collections import Counter, defaultdict
from pathlib import Path

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
        if args.mode == "home" and not (
            (_is_copy(oracle[a]) and _is_untap(oracle[b]))
            or (_is_copy(oracle[b]) and _is_untap(oracle[a]))
        ):
            continue
        selected.append((combo_id, a, b))
    selected.sort()
    batch = selected[args.start:args.start + args.count]
    print(
        f"mode={args.mode} total={len(selected)} "
        f"running {args.start}..{args.start + len(batch)}"
    )

    out_path = Path(args.out) if args.out else Path(
        f"/tmp/opencode/known_recall_{args.mode}_{args.start}.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Only touch the database when asked: the default path stays write-free.
    store = ExperimentStore(Path(args.db)) if args.persist else None
    results: list[dict] = []
    try:
        with ForgeEnvClient(host=args.host, port=args.port) as client:
            client.connect()
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
                        "verdict": res.verdict,
                        "executed": int(diag.get("executed_actions", 0)),
                        "reason": evidence.get("kind") or evidence.get("reason"),
                    }
                except Exception as exc:  # noqa: BLE001
                    record = {
                        "combo_id": combo_id,
                        "cards": [display.get(a, a), display.get(b, b)],
                        "engine": engine,
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
                out_path.write_text(json.dumps(results, indent=1))
                print(
                    f"  {combo_id:7d} [{engine:10s}] {record['verdict']:12s} "
                    f"exec={record['executed']:2d} {record['cards'][0]} + "
                    f"{record['cards'][1]}  {record['reason']}"
                )
    finally:
        if store is not None:
            store.close()

    counts = Counter(r["verdict"] for r in results)
    print(
        f"\nSUMMARY mode={args.mode} n={len(results)} verdicts={dict(counts)} "
        f"drove={sum(1 for r in results if r['executed'] > 0)}"
    )
    for engine in ("activated", "triggered", "none"):
        subset = [r for r in results if r["engine"] == engine]
        if subset:
            sub_counts = Counter(r["verdict"] for r in subset)
            recall = sub_counts.get("loops", 0) / len(subset)
            print(f"  engine={engine:10s} n={len(subset):3d} {dict(sub_counts)} "
                  f"recall={recall:.0%}")
    print("wrote", out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
