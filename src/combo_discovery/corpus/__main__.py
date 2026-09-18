"""CLI entry point: ``combo-import-cards`` / ``python -m combo_discovery.corpus``.

Imports the Forge cardsfolder (and, optionally, cached Scryfall oracle data)
into the experiment SQLite store.  Network is opt-in: the default uses a cached
Scryfall bulk file if one exists and otherwise imports Forge scripts only;
``--download`` fetches the bulk file first.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..store import ExperimentStore
from .importer import ScryfallSource, import_corpus

DEFAULT_FORGE_ROOT = "/home/lza/Work/forge"
DEFAULT_DB = "./research.db"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="combo-import-cards",
        description="Import Forge card scripts (+ optional Scryfall oracle data) into SQLite.",
    )
    parser.add_argument(
        "--forge-root",
        default=DEFAULT_FORGE_ROOT,
        help=f"Forge checkout root or a cardsfolder (default: {DEFAULT_FORGE_ROOT})",
    )
    parser.add_argument("--db", default=DEFAULT_DB, help=f"SQLite path (default: {DEFAULT_DB})")
    parser.add_argument(
        "--scryfall",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="enrich from Scryfall (uses the cache if present; default: on)",
    )
    parser.add_argument(
        "--download",
        action="store_true",
        help="download the Scryfall oracle_cards bulk file before importing",
    )
    parser.add_argument(
        "--cache-dir",
        default=None,
        help="Scryfall cache directory (default: ~/.cache/combo-discovery/scryfall)",
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="import at most N scripts (testing)"
    )
    return parser


def _progress(downloaded: int, total: int) -> None:
    if total:
        pct = 100.0 * downloaded / total
        sys.stderr.write(f"\r[scryfall] {downloaded / 1e6:7.1f}/{total / 1e6:.1f} MB ({pct:5.1f}%)")
    else:
        sys.stderr.write(f"\r[scryfall] {downloaded / 1e6:7.1f} MB")
    if total and downloaded >= total:
        sys.stderr.write("\n")
    sys.stderr.flush()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    source: ScryfallSource | None = None
    if args.scryfall:
        source = ScryfallSource(cache_dir=args.cache_dir, auto_download=args.download)
        if not source.has_cache() and not args.download:
            print(
                "[scryfall] no cached bulk file; importing Forge scripts only "
                "(pass --download to fetch oracle_cards)",
                file=sys.stderr,
            )
            source = None

    store = ExperimentStore(Path(args.db))
    try:
        report = import_corpus(
            store,
            args.forge_root,
            scryfall=source,
            limit=args.limit,
        )
    finally:
        store.close()

    print(report.format())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
