"""Resolve card names to Scryfall image URLs.

Card art is a core part of the rebuilt web console, and the maintainer has
explicitly approved loading it from Scryfall's image CDN.  This module is the
whole surface for that: it turns a card name into one or more
``https://cards.scryfall.io/...`` URLs **without ever making a network call**.
The browser fetches the art; the Python side only constructs strings, so the web
server stays a read-only consumer.

Where the ids come from
-----------------------
Scryfall's CDN keys images by the *printing* (card) id, not the oracle id::

    https://cards.scryfall.io/<size>/<front|back>/<id[0]>/<id[1]>/<id>.jpg

The research database does not store that id.  It stores Scryfall *oracle* ids
(``card_oracle_ids.oracle_id``; ``cards.scryfall_oracle_id`` is blank in every
corpus row), and an oracle id 404s on the CDN.  The corpus does cache Scryfall's
``oracle_cards`` bulk at
``~/.cache/combo-discovery/scryfall/oracle_cards.jsonl.gz`` (the same file the
instruments read for release dates), and that bulk carries the printing ``id``
alongside the ``oracle_id`` and ``name``.  Resolution therefore is:

1. name -> oracle id from the database (read-only, the corpus authority);
2. oracle id -> printing id from the cached bulk (guarded by a front-name match);
3. name -> printing id from the cached bulk (fallback).

The resolved printing id is memoised per normalised name, so rendering a page of
40 cards costs at most one bulk index build and 40 dict lookups.
"""

from __future__ import annotations

import gzip
import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from ..research_config import load_config
from .names import normalize_card_name

#: Scryfall image CDN root.
SCRYFALL_IMAGE_BASE = "https://cards.scryfall.io"

#: The image variants Scryfall serves for every printing.
SIZES = ("small", "normal", "large", "art_crop", "border_crop")

#: Default location of the cached Scryfall ``oracle_cards`` bulk.
DEFAULT_BULK_PATH = (
    Path.home() / ".cache" / "combo-discovery" / "scryfall" / "oracle_cards.jsonl.gz"
)

#: Non-playable "layouts" skipped when indexing art (art-series prints reuse a
#: real card's name and would otherwise shadow the canonical printing).
_SKIP_LAYOUTS = frozenset({"art_series"})


@dataclass(frozen=True)
class _Art:
    """One Scryfall printing's image identity."""

    scryfall_id: str
    face_count: int


class _Resolver:
    """Lazy, in-process name -> printing-id index over the DB and the bulk."""

    def __init__(self, db_path: Path, bulk_path: Path) -> None:
        self._db_path = Path(db_path)
        self._bulk_path = Path(bulk_path)
        self._lock = threading.RLock()
        self._resolved: dict[str, _Art | None] = {}
        self._by_name: dict[str, _Art] = {}
        self._by_oracle: dict[str, tuple[str, _Art]] = {}
        self._db_oracle: dict[str, str] = {}
        self._bulk_loaded = False
        self._db_loaded = False

    # -- lazy loads ------------------------------------------------------

    def _load_bulk(self) -> None:
        if self._bulk_loaded:
            return
        self._bulk_loaded = True
        try:
            handle = gzip.open(self._bulk_path, "rt", encoding="utf-8")
        except OSError:
            return
        with handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if entry.get("layout") in _SKIP_LAYOUTS:
                    continue
                card_id = entry.get("id")
                if not card_id:
                    continue
                faces = entry.get("card_faces") or []
                face_images = sum(1 for face in faces if face.get("image_uris"))
                art = _Art(str(card_id).lower(), face_images or 1)
                front = normalize_card_name(str(entry.get("name") or "").split("//")[0])
                if front:
                    self._by_name.setdefault(front, art)
                oracle_id = entry.get("oracle_id")
                if oracle_id:
                    self._by_oracle.setdefault(str(oracle_id), (front, art))

    def _load_db(self) -> None:
        if self._db_loaded:
            return
        self._db_loaded = True
        path = self._db_path
        try:
            if not path.exists():
                return
            uri = f"file:{quote(str(path.expanduser().resolve()))}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        except sqlite3.Error:
            return
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout = 3000")
            # Belt-and-braces, mirroring ``combo_discovery.web.db``: this
            # connection can never write even if a statement is malformed.
            conn.execute("PRAGMA query_only = ON")
            self._db_oracle = self._read_oracle_ids(conn)
        finally:
            conn.close()

    @staticmethod
    def _read_oracle_ids(conn: sqlite3.Connection) -> dict[str, str]:
        mapping: dict[str, str] = {}
        try:
            rows = conn.execute(
                "SELECT c.normalized_name AS name, "
                "COALESCE(NULLIF(coi.oracle_id, ''), "
                "NULLIF(c.scryfall_oracle_id, '')) AS oracle_id "
                "FROM cards c "
                "LEFT JOIN card_oracle_ids coi ON coi.card_id = c.id "
                "ORDER BY c.id"
            )
            for row in rows:
                name = str(row["name"] or "")
                oracle_id = str(row["oracle_id"] or "")
                if name and oracle_id and name not in mapping:
                    mapping[name] = oracle_id
        except sqlite3.Error:
            # Older/smaller databases may lack ``card_oracle_ids``.
            try:
                rows = conn.execute(
                    "SELECT normalized_name AS name, scryfall_oracle_id AS oracle_id "
                    "FROM cards ORDER BY id"
                )
            except sqlite3.Error:
                return mapping
            for row in rows:
                name = str(row["name"] or "")
                oracle_id = str(row["oracle_id"] or "")
                if name and oracle_id and name not in mapping:
                    mapping[name] = oracle_id
        return mapping

    # -- lookup ----------------------------------------------------------

    def resolve(self, card_name: str) -> _Art | None:
        key = normalize_card_name(card_name)
        if not key:
            return None
        with self._lock:
            if key in self._resolved:
                return self._resolved[key]
            self._load_bulk()
            self._load_db()
            art: _Art | None = None
            oracle_id = self._db_oracle.get(key)
            if oracle_id:
                candidate = self._by_oracle.get(oracle_id)
                # Guard against a misaligned oracle id (rare, ~0.05%): only
                # trust it when the printing's front face is the same card.
                if candidate is not None and candidate[0] == key:
                    art = candidate[1]
            if art is None:
                art = self._by_name.get(key)
            self._resolved[key] = art
            return art


_RESOLVER: _Resolver | None = None
_RESOLVER_LOCK = threading.Lock()


def _database_path() -> Path:
    """The research database path, resolved the way the other tools do."""
    return Path(load_config().db_path)


def _bulk_path() -> Path:
    """The cached Scryfall ``oracle_cards`` bulk path."""
    return DEFAULT_BULK_PATH


def _resolver() -> _Resolver:
    global _RESOLVER
    if _RESOLVER is None:
        with _RESOLVER_LOCK:
            if _RESOLVER is None:
                _RESOLVER = _Resolver(_database_path(), _bulk_path())
    return _RESOLVER


def _reset_cache() -> None:
    """Drop the in-process resolver (tests / database hot-swaps)."""
    global _RESOLVER
    with _RESOLVER_LOCK:
        _RESOLVER = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def image_url(card_name: str, face: int = 0, size: str = "normal") -> str | None:
    """A Scryfall image URL for ``card_name``, or ``None`` if unavailable.

    ``face`` 0 is the front face and 1 the back face of a double-faced card.
    ``size`` must be one of :data:`SIZES` (anything else raises
    :class:`ValueError`).  Unknown cards, cards with no Scryfall id, and face
    indices the card does not have all return ``None``; this never raises for a
    missing card and never touches the network.
    """
    if size not in SIZES:
        raise ValueError(
            f"unknown image size {size!r}; expected one of {', '.join(SIZES)}"
        )
    try:
        face_index = int(face)
    except (TypeError, ValueError):
        return None
    art = _resolver().resolve(card_name)
    if art is None:
        return None
    if face_index < 0 or face_index >= art.face_count:
        return None
    segment = "front" if face_index == 0 else "back"
    card_id = art.scryfall_id
    return (
        f"{SCRYFALL_IMAGE_BASE}/{size}/{segment}/"
        f"{card_id[0]}/{card_id[1]}/{card_id}.jpg"
    )


def image_urls(card_name: str) -> list[str]:
    """Every face image for ``card_name`` (0, 1 or 2 ``normal`` URLs)."""
    art = _resolver().resolve(card_name)
    if art is None:
        return []
    urls: list[str] = []
    for index in range(art.face_count):
        url = image_url(card_name, face=index)
        if url is not None:
            urls.append(url)
    return urls


__all__ = [
    "DEFAULT_BULK_PATH",
    "SCRYFALL_IMAGE_BASE",
    "SIZES",
    "image_url",
    "image_urls",
]
