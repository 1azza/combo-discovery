#!/usr/bin/env python
"""Coverage-gap diagnostic: why known combos are absent from the candidate set.

Read-only.  Maps Commander Spellbook known combos to corpus card ids with the
repo's own normalizer, then classifies every known card pair into:

* ``net_present``         -- the pair is a generated 2-card hypothesis;
* ``no_graph_edge``       -- no ``interactions`` row exists at all;
* ``edge_exists_filtered``-- an edge exists but the pair is never a 2-card
  hypothesis (it only closed inside a larger cycle).

It also reports the missing-capability profile of the no-edge pairs, the inverse
precision view (how much of the candidate space is a known pair), and the
loop-machinery pool gap.

Usage:
    uv run python scripts/coverage_gap.py
    uv run python scripts/coverage_gap.py --denominator strict2 --no-pool
    uv run python scripts/coverage_gap.py --json /tmp/opencode/coverage_gap.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from combo_discovery.corpus.names import normalize_card_name
from combo_discovery.ontology.builder import latest_import_id
from combo_discovery.ontology.coverage import (
    analyze_denominator,
    combo_pairs,
    load_candidate_pairs,
    load_card_meta,
    load_known_combos,
    loop_pool_cards,
    oracle_index,
    ordered_combo_pairs,
    pool_gap_view,
    precision_view,
)


def _open_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _parse_runs(value: str) -> tuple[tuple[int, int], ...]:
    spans = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        low, _, high = part.partition("-")
        spans.append((int(low), int(high or low)))
    return tuple(spans)


def _witness_view(conn, by_norm, runs) -> dict:
    """Reconcile with the earlier witness-verdict measurement."""
    where = " OR ".join("run_id BETWEEN ? AND ?" for _ in runs)
    params = [value for span in runs for value in span]
    pairs: set[frozenset[int]] = set()
    row_pairs: list[frozenset[int]] = []
    for row in conn.execute(
        f"SELECT card_names_json FROM witness_results WHERE {where}", params
    ):
        names = json.loads(row["card_names_json"])
        ids = [by_norm.get(normalize_card_name(name)) for name in names]
        ids = [cid for cid in ids if cid is not None]
        if len(ids) == 2:
            pair = frozenset(ids)
            pairs.add(pair)
            row_pairs.append(pair)
    return {"distinct_pairs": len(pairs), "row_pairs": row_pairs, "_pairs": pairs}


def _print_bucket_report(report) -> None:
    print(f"\nBUCKETS: {report.denominator} (n={report.n_pairs})")
    for bucket, count in report.buckets.items():
        pct = 100.0 * count / report.n_pairs if report.n_pairs else 0.0
        print(f"  {bucket:22s} {count:>7}  ({pct:5.1f}%)")
    print("  top missing capabilities (no-edge pairs):")
    for capability, count in list(report.capabilities.items())[:8]:
        print(f"    {capability:22s} {count:>7}")
        for a, b in report.capability_examples.get(capability, [])[:3]:
            print(f"        - {a} + {b}")
    if report.filtered_examples:
        print("  filtered examples (edge, not a 2-card proposal):")
        for a, b in report.filtered_examples[:5]:
            print(f"        - {a} + {b}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="coverage-gap",
        description="Classify why known combos are absent from the candidate set.",
    )
    parser.add_argument("--db", default="research.db")
    parser.add_argument("--import-id", default=None)
    parser.add_argument(
        "--denominator", choices=("strict2", "pairs", "both"), default="both",
        help="known-pair set to bucket: exact 2-card combos, all known pairs, or both",
    )
    parser.add_argument("--witness-runs", default="1586-2008,169-301")
    parser.add_argument("--no-pool", action="store_true",
                        help="skip the loop-machinery pool analysis")
    parser.add_argument("--json", default=None)
    args = parser.parse_args(argv)

    conn = _open_db(args.db)
    try:
        import_id = args.import_id or latest_import_id(conn)
        metas = load_card_meta(conn)
        by_norm = {m.normalized_name: cid for cid, m in metas.items()}
        by_oracle = oracle_index(conn)
        combos = load_known_combos(conn, metas, by_oracle=by_oracle)
        candidates = load_candidate_pairs(conn)

        unmapped_rows = sum(c.n_unmapped for c in combos)
        unmapped_combos = sum(1 for c in combos if c.n_unmapped)
        print(f"import: {import_id}")
        print(f"corpus cards: {len(metas)}  oracle ids: {len(by_oracle)}")
        print("\nMAPPING")
        print(f"  known combos: {len(combos)}")
        print(f"  use rows: {sum(len(c.card_ids) for c in combos)}")
        print(f"  use rows unmapped: {unmapped_rows}")
        print(f"  combos with >=1 unmapped use card: {unmapped_combos}")

        strict2 = combo_pairs(combos, sizes=(2,))
        ordered2 = ordered_combo_pairs(combos, sizes=(2,))
        size3 = combo_pairs(combos, sizes=(2, 3))
        all_pairs = combo_pairs(combos)

        present2 = candidates["pairs2"]
        present_any = candidates["pairs"]
        edges = candidates["edges"]

        print("\nDENOMINATOR LADDER (mapped known pairs)")
        ladder = [
            ("exact 2-card combos", strict2),
            ("ordered exact 2-card", None),
            ("all-mapped combos <=3 cards", size3),
            ("all known pairs (any combo)", all_pairs),
        ]
        for label, pairs in ladder:
            if pairs is None:
                n = len(ordered2)
                hit2 = sum(1 for a, b in ordered2 if frozenset((a, b)) in present2)
                hit_any = sum(1 for a, b in ordered2 if frozenset((a, b)) in present_any)
            else:
                n = len(pairs)
                hit2 = len(pairs & present2)
                hit_any = len(pairs & present_any)
            print(f"  {label:30s} pairs={n:>7}  present2={hit2:>5}  "
                  f"present_any={hit_any:>5}")

        witness = _witness_view(
            conn, by_norm, _parse_runs(args.witness_runs)
        )
        witness["present2"] = len(witness["_pairs"] & present2)
        witness["present_any"] = len(witness["_pairs"] & present_any)
        witness["row_hits2"] = sum(
            1 for pair in witness["row_pairs"] if pair in present2
        )
        witness["row_hits_any"] = sum(
            1 for pair in witness["row_pairs"] if pair in present_any
        )
        print(f"  {'witness verdict pairs':30s} pairs={witness['distinct_pairs']:>7}  "
              f"present2={witness['present2']:>5}  "
              f"present_any={witness['present_any']:>5}  "
              f"(row hits: {witness['row_hits2']}/{len(witness['row_pairs'])})")

        reports = []
        if args.denominator in ("strict2", "both"):
            reports.append(analyze_denominator(
                "exact 2-card combos", strict2,
                present_pairs=present2, edge_pairs=edges, metas=metas,
            ))
        if args.denominator in ("pairs", "both"):
            reports.append(analyze_denominator(
                "all known pairs", all_pairs,
                present_pairs=present2, edge_pairs=edges, metas=metas,
            ))
        for report in reports:
            _print_bucket_report(report)

        print("\nINVERSE PRECISION VIEW")
        for label, candidate_set in (
            ("2-card hypotheses", present2),
            ("hypothesis pairs (any)", present_any),
        ):
            view = precision_view(candidate_set, all_pairs)
            print(f"  {label:24s} candidates={view['candidate_pairs']:>7}  "
                  f"known={view['known_pairs']:>5}  "
                  f"fraction={view['fraction']:.4%}")

        payload = {
            "import_id": import_id,
            "mapping": {
                "combos": len(combos),
                "unmapped_rows": unmapped_rows,
                "unmapped_combos": unmapped_combos,
            },
            "ladder": {
                "exact2": len(strict2),
                "ordered2": len(ordered2),
                "size_le_3": len(size3),
                "all_pairs": len(all_pairs),
                "witness_pairs": witness["distinct_pairs"],
            },
            "candidates": {
                "pairs2": len(present2),
                "pairs_any": len(present_any),
                "edges": len(edges),
            },
            "reports": [report.as_dict() for report in reports],
            "precision": {
                "pairs2": precision_view(present2, all_pairs),
                "pairs_any": precision_view(present_any, all_pairs),
            },
            "witness": {
                "pairs": witness["distinct_pairs"],
                "present2": witness["present2"],
                "present_any": witness["present_any"],
            },
        }

        if not args.no_pool and import_id is not None:
            print("\nLOOP-MACHINERY POOL GAP")
            try:
                pool = loop_pool_cards(conn, import_id)
            except Exception as exc:  # noqa: BLE001 - diagnostic best-effort
                print(f"  pool unavailable: {exc}")
                pool = None
            if pool is not None:
                print(f"  pool cards: {len(pool)}")
                no_edge = all_pairs - edges
                print(f"  no-edge known pairs: {len(no_edge)}  "
                      f"{pool_gap_view(no_edge, pool)}")
                no_edge_strict = strict2 - edges
                print(f"  no-edge exact-2-card pairs: {len(no_edge_strict)}  "
                      f"{pool_gap_view(no_edge_strict, pool)}")
                payload["pool"] = {
                    "cards": len(pool),
                    "no_edge_all": pool_gap_view(no_edge, pool),
                    "no_edge_exact2": pool_gap_view(no_edge_strict, pool),
                }

        if args.json:
            Path(args.json).write_text(json.dumps(payload, indent=1))
            print(f"\nwrote {args.json}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
