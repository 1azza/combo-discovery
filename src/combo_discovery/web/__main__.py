"""``combo-web`` entry point: ``python -m combo_discovery.web``."""

from __future__ import annotations

import argparse
import sys

from .app import DEFAULT_HOST, DEFAULT_PORT, serve


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="combo-web",
        description=(
            "Local, read-only web console for watching witness runs. Binds "
            "127.0.0.1 by default and never writes to the database."
        ),
    )
    parser.add_argument(
        "--db", default="research.db", help="SQLite research DB (default: research.db)"
    )
    parser.add_argument(
        "--host", default=DEFAULT_HOST, help=f"bind host (default: {DEFAULT_HOST})"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"bind port (default: {DEFAULT_PORT})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return serve(args.db, host=args.host, port=args.port)


if __name__ == "__main__":  # pragma: no cover - module entry
    sys.exit(main())
