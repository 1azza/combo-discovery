"""CLI entry point: ``combo-build-ontology`` / ``python -m combo_discovery.ontology``.

By default builds the **interaction algebra** (cycle queries registered as
``q:<name>`` patterns, cycles -> hypotheses, pairwise links -> interactions).
Pass ``--legacy`` to build the original pattern-registry edges instead.  Both
paths append; neither deletes the other, so the legacy baseline stays
measurable.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..store import ExperimentStore
from .budget import DEFAULT_MAX_SECONDS, DEFAULT_MAX_STEPS
from .builder import build_ontology, probe_interactions

DEFAULT_DB = "./research.db"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="combo-build-ontology",
        description="Build the ontology + interaction graph from a corpus import.",
    )
    parser.add_argument("--db", default=DEFAULT_DB, help=f"SQLite path (default: {DEFAULT_DB})")
    parser.add_argument(
        "--import-id",
        default=None,
        help="corpus import_id to build (default: the latest import_runs row)",
    )
    parser.add_argument("--legacy", action="store_true",
                        help="build the original pattern-registry edges (baseline) "
                             "instead of the interaction algebra")
    parser.add_argument("--caps", action="store_true",
                        help="legacy path: apply per-pattern fan-out caps")
    parser.add_argument("--broad", action="store_true",
                        help="algebra path: search the full Vintage pool "
                             "(requires --allow-over-budget above the safe limit)")
    parser.add_argument("--allow-over-budget", action="store_true",
                        help="algebra path: permit configs above the safe thresholds")
    parser.add_argument("--max-len", type=int, default=3, help="max cards per cycle")
    parser.add_argument("--max-seconds", type=float, default=DEFAULT_MAX_SECONDS,
                        help="internal wall-clock budget for the algebra search")
    parser.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS,
                        help="internal step budget for the algebra search")
    parser.add_argument("--force", action="store_true",
                        help="rebuild even if rows already exist for the import")
    parser.add_argument("--top", type=int, default=20, help="how many hypotheses to print")
    parser.add_argument(
        "--probe",
        action="append",
        default=[],
        metavar="CARD",
        help="after building, print the highest-scoring interactions for CARD "
             "(repeatable; recall probe)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = ExperimentStore(Path(args.db))
    try:
        report = build_ontology(
            store,
            args.import_id,
            force=args.force,
            apply_caps=args.caps,
            mode="legacy" if args.legacy else "algebra",
            broad_pool=args.broad,
            max_len=args.max_len,
            max_seconds=args.max_seconds,
            max_steps=args.max_steps,
            allow_over_budget=args.allow_over_budget,
        )
        probes = {
            name: probe_interactions(store, name, args.import_id, limit=args.top)
            for name in args.probe
        }
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        store.close()

    print(report.format(top_n=args.top))
    for name, rows in probes.items():
        print("")
        print(f"probe: {name}")
        if not rows:
            print("  (no interactions)")
        for row in rows:
            print(
                f"  [{row['pattern']}] score={row['score']:.3f} "
                f"{row['source_name']} + {row['target_name']} ({row['direction']})"
            )
            print(f"      {row['mechanism']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
