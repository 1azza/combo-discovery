"""Local, read-only web console for watching witness runs.

The package is deliberately dependency-light: it serves HTML/JSON through the
standard library only (``http.server`` + a tiny router) and reads the research
database through a ``mode=ro`` SQLite connection. It never writes.

The visual language mirrors :mod:`combo_discovery.tui.theme` (the "omarchy"
palette) so the TUI and the web console read as one product.
"""

from __future__ import annotations

from .app import create_server, serve
from .db import ReadOnlyStore, open_read_only

__all__ = ["ReadOnlyStore", "create_server", "open_read_only", "serve"]
