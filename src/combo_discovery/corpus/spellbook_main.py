"""CLI entry point: ``combo-import-spellbook``.

Imports Commander Spellbook (Tier-A ground truth) into the ``known_*`` tables.
Network is opt-in: ``--download`` (or no ``--variants-file``) fetches the bulk
gz; otherwise a local ``.json``/``.json.gz`` is streamed.  ``--backfill-oracle``
first populates ``card_oracle_ids`` from the cached Scryfall bulk.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..store import ExperimentStore
from .oracle import backfill_oracle_ids
from .spellbook import SPELLBOOK_BULK_URL, import_spellbook


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="combo-import-spellbook",
        description="Import Commander Spellbook variants (Tier-A ground truth).",
    )
    parser.add_argument("--db", default="./research.db")
    parser.add_argument("--variants-file", default=None,
                        help="local variants.json or variants.json.gz")
    parser.add_argument("--download", action="store_true",
                        help="download the bulk gz into the cache")
    parser.add_argument("--no-vintage-filter", action="store_true",
                        help="keep non-Vintage variants as well")
    parser.add_argument("--limit", type=int, default=None,
                        help="process at most N variants (testing)")
    parser.add_argument("--cache-dir", default=None, help="Spellbook cache directory")
    parser.add_argument("--format-path", default=None,
                        help="Forge Vintage format file (default: Forge checkout)")
    parser.add_argument("--scryfall", action="store_true",
                        help="use the cached Scryfall bulk for per-card Vintage legality")
    parser.add_argument("--backfill-oracle", action="store_true",
                        help="backfill card_oracle_ids from the cached Scryfall bulk first")
    parser.add_argument("--url", default=SPELLBOOK_BULK_URL)
    return parser


def _progress(downloaded: int, total: int) -> None:
    if total:
        sys.stderr.write(f"\r[spellbook] {downloaded / 1e6:7.1f}/{total / 1e6:.1f} MB")
    else:
        sys.stderr.write(f"\r[spellbook] {downloaded / 1e6:7.1f} MB")
    if total and downloaded >= total:
        sys.stderr.write("\n")
    sys.stderr.flush()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = ExperimentStore(Path(args.db))
    try:
        scryfall = None
        if args.scryfall or args.backfill_oracle:
            from .importer import ScryfallSource

            scryfall = ScryfallSource()
            if not args.backfill_oracle:
                scryfall.load()
        if args.backfill_oracle:
            report = backfill_oracle_ids(store, scryfall)
            print(report.format())
            print()
        download = args.download or args.variants_file is None
        report = import_spellbook(
            store,
            args.variants_file,
            download=download,
            vintage_only=not args.no_vintage_filter,
            scryfall=scryfall if args.scryfall else None,
            format_path=args.format_path,
            cache_dir=args.cache_dir,
            limit=args.limit,
            progress=_progress if download else None,
        )
    except Exception as exc:  # noqa: BLE001 - surface a clean CLI error
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    finally:
        store.close()

    print(report.format())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
