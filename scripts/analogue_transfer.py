"""Lever 1: analogue transfer.

For known activated copy engines, enumerate cards with the same *functional*
ETB-untap shape as the known combo partners, drop any pair already in Commander
Spellbook, and run the real witness verifier. Survivors are candidate
uncatalogued combos (they still need the independent second source before anyone
calls them novel).

This is how `Reptilian Recruiter` (analogue of `Coercive Recruiter`, absent from
Spellbook) and `Splinter Twin + Giant-Sized Flying Ant` surfaced. It concentrates
on the shapes the witness can actually drive (activated copy engines) and on the
places Spellbook lags (recent sets).

Usage:
    uv run python scripts/analogue_transfer.py
    uv run python scripts/analogue_transfer.py --engines "Kiki-Jiki, Mirror Breaker;Splinter Twin"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

from combo_discovery import witness as W
from combo_discovery.corpus.names import normalize_card_name, pair_hash
from combo_discovery.env import ForgeEnvClient
from combo_discovery.ontology.builder import _load_vintage
from combo_discovery.research_config import load_config
from combo_discovery.store import ExperimentStore

DEFAULT_ENGINES = "Kiki-Jiki, Mirror Breaker;Splinter Twin"
DEFAULT_DECKS = ["A=decks/goldfish_A.dck", "B=decks/goldfish_B.dck"]

#: The functional ETB-untap shapes that can untap the copy engine: untap a
#: targeted permanent/creature, or the gain-control-then-untap-it shape.
UNTAP_PAT = re.compile(r"untap target|untap it|untap that")


def _is_creature(tl: str) -> bool:
    return "creature" in (tl or "").lower()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="analogue-transfer",
        description="Witness-verify functional analogues of known combo partners.",
    )
    parser.add_argument("--db", default="research.db")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=50051)
    parser.add_argument("--engines", default=DEFAULT_ENGINES)
    parser.add_argument("--decks", default=",".join(DEFAULT_DECKS))
    parser.add_argument("--out", default="/tmp/opencode/analogue_results.json")
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
    legality = _load_vintage({})
    known = {r[0] for r in conn.execute("select pair_hash from known_combo_pairs")}
    type_lines = {r[0]: (r[1] or "") for r in conn.execute(
        "select name, type_line from cards")}

    partners = []
    for _id, _norm, name, tl, ot in conn.execute(
        "select id, normalized_name, name, type_line, lower(oracle_text) from cards"
    ):
        if not ot or "untap" not in ot:
            continue
        if not _is_creature(tl):
            continue
        if "enters" not in ot and "enter the battlefield" not in ot:
            continue
        if not UNTAP_PAT.search(ot):
            continue
        if not legality.is_legal(name):
            continue
        partners.append(name)
    partners = sorted(set(partners))

    engines = [e.strip() for e in args.engines.split(";") if e.strip()]
    pairs, seen = [], set()
    for engine in engines:
        en = normalize_card_name(engine)
        for partner in partners:
            if pair_hash(en, normalize_card_name(partner)) in known:
                continue
            if (engine, partner) in seen:
                continue
            seen.add((engine, partner))
            pairs.append((engine, partner))
    print(f"engines={len(engines)} partners={len(partners)} analogue pairs={len(pairs)}")

    # Only touch the database when asked: the default path stays write-free.
    store = ExperimentStore(Path(args.db)) if args.persist else None
    results: list[dict] = []
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with ForgeEnvClient(host=args.host, port=args.port) as client:
            client.connect()
            for engine, partner in pairs:
                combo = W.Candidate(
                    cards=(engine, partner),
                    type_lines=(type_lines.get(engine, ""),
                                type_lines.get(partner, "")),
                    kind="pair", key=f"analogue:{engine}+{partner}",
                )
                scenario = W.build_scenario(combo)
                policy = W.WitnessPolicy(combo)
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
                    res = W.run_witness(
                        client, scenario, policy, seeds=[1],
                        max_iterations=6, max_decisions=256, decks=decks,
                        candidate_kind=combo.kind, candidate_key=combo.key,
                        card_names=combo.cards, infinite=combo.infinite,
                        recorder=recorder,
                    )
                    persisted_res = res
                    diag = policy.diagnostics()
                    record = {
                        "engine": engine, "partner": partner, "verdict": res.verdict,
                        "executed": int(diag.get("executed_actions", 0)),
                        "reason": (res.evidence or {}).get("kind")
                        or (res.evidence or {}).get("reason"),
                    }
                except Exception as exc:  # noqa: BLE001
                    record = {"engine": engine, "partner": partner, "verdict": "error",
                              "executed": 0, "reason": str(exc)}
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
                out_path.write_text(json.dumps(results, indent=1))
                print(f"  {record['verdict']:12s} exec={record['executed']:2d} "
                      f"{engine} + {partner}")
    finally:
        if store is not None:
            store.close()

    loops = [r for r in results if r["verdict"] == "loops"]
    print(f"\nSUMMARY n={len(results)} {dict(Counter(r['verdict'] for r in results))}")
    print(f"candidate loops not in Spellbook: {len(loops)}")
    for r in loops:
        print(f"  LOOPS {r['engine']} + {r['partner']}  ({r['reason']})")
    print("wrote", out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
