"""CLI entry point: ``combo-build-ontology`` / ``python -m combo_discovery.ontology``.

Builds predicates, interactions and combo hypotheses for a corpus import and
prints a report (predicate counts, interactions by pattern, top hypotheses).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..store import ExperimentStore
from .builder import build_ontology, probe_interactions

DEFAULT_DB = "./research.db"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="combo-build-ontology",
        description="Build the predicate ontology + interaction graph from a corpus import.",
    )
    parser.add_argument("--db", default=DEFAULT_DB, help=f"SQLite path (default: {DEFAULT_DB})")
    parser.add_argument(
        "--import-id",
        default=None,
        help="corpus import_id to build (default: the latest import_runs row)",
    )
    parser.add_argument("--caps", action="store_true",
                        help="apply per-pattern fan-out caps (default: keep every pair)")
    parser.add_argument("--force", action="store_true",
                        help="rebuild even if predicates already exist for the import")
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
