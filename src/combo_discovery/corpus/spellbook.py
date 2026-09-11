"""Commander Spellbook ingestion: Tier-A known-combo ground truth.

Streams the public bulk file (``variants.json.gz``, ~646 MB decompressed) with
``gzip`` + ``ijson`` -- never ``json.load`` -- and writes the ``known_*`` schema
v4 tables through the append-only store.  Network use is isolated in
:func:`download_variants`; the importer itself only reads a local path.

Vintage filtering (when enabled) keeps a variant only when:

* its own ``legalities.vintage`` is true;
* every concrete ``use`` is Vintage-legal (Scryfall ``legalities.vintage`` when
  available, else the Forge Vintage format's banned list -- restricted cards
  stay legal); and
* no use/require has ``mustBeCommander``.

Attribution: data from Commander Spellbook, https://commanderspellbook.com.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
import time
import urllib.request
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import ijson

from .. import store as store_module
from ..store import ExperimentStore
from .names import (
    ResolvedCard,
    combo_hash,
    front_face_name,
    is_non_vintage_printing,
    normalize_card_name,
    pair_hash,
    resolve_spellbook_use,
)

SPELLBOOK_BULK_URL = "https://json.commanderspellbook.com/variants.json.gz"
SOURCE_NAME = "commander_spellbook"
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "combo-discovery" / "spellbook"
DEFAULT_VINTAGE_FORMAT = Path(
    "/home/lza/Work/forge/forge-gui/res/formats/Sanctioned/Vintage.txt"
)
_KEEP_STATUS = frozenset({"OK", "EXAMPLE"})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Vintage legality
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VintageLegality:
    """Per-card Vintage legality.

    Built from the Forge Vintage format file's ``Banned:``/``Restricted:`` names
    (restricted cards are legal).  The format's ``Sets:`` list is parsed but not
    enforced: local ``cards.set_code`` is blank, and Vintage's set list is
    comprehensive, so the practical filter is the banned list.  When a Scryfall
    record's ``legalities`` is supplied it is authoritative.
    """

    banned: frozenset[str] = frozenset()
    restricted: frozenset[str] = frozenset()
    sets: frozenset[str] = frozenset()
    source: str = "permissive"

    @classmethod
    def from_forge_format(cls, path: str | Path) -> "VintageLegality":
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        banned: set[str] = set()
        restricted: set[str] = set()
        sets: set[str] = set()
        for line in text.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            key = key.strip().lower()
            if key == "banned":
                banned.update(
                    normalize_card_name(name) for name in value.split(";") if name.strip()
                )
            elif key == "restricted":
                restricted.update(
                    normalize_card_name(name) for name in value.split(";") if name.strip()
                )
            elif key == "sets":
                sets.update(code.strip() for code in value.split(",") if code.strip())
        return cls(frozenset(banned), frozenset(restricted), frozenset(sets), "forge_format")

    @classmethod
    def permissive(cls) -> "VintageLegality":
        return cls()

    def is_legal(
        self,
        name: str | None,
        *,
        oracle_id: str | None = None,
        scryfall_legalities: dict[str, Any] | None = None,
    ) -> bool:
        if scryfall_legalities is not None:
            return bool(scryfall_legalities.get("vintage"))
        # Alchemy rebalances (``A-*``) and Un-set/sticker placeholder names are
        # not Vintage-legal regardless of the banned list; the banned list holds
        # only real card names and cannot catch them.
        if is_non_vintage_printing(name):
            return False
        normalized = normalize_card_name(name)
        if not normalized:
            return True
        return normalized not in self.banned


# ---------------------------------------------------------------------------
# Download (the only network code in this module)
# ---------------------------------------------------------------------------


def download_variants(
    cache_dir: str | Path | None = None,
    *,
    url: str = SPELLBOOK_BULK_URL,
    force: bool = False,
    timeout: float = 300.0,
    progress=None,
) -> Path:
    """Download the Spellbook bulk gz into the cache; return its path."""
    cache = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / "variants.json.gz"
    if target.is_file() and not force:
        return target
    tmp = target.with_name(target.name + ".tmp")
    digest = hashlib.sha256()
    request = urllib.request.Request(
        url, headers={"User-Agent": "combo-discovery/0.1 (ground truth importer)"}
    )
    downloaded = 0
    with urllib.request.urlopen(request, timeout=timeout) as response:
        total = int(response.headers.get("Content-Length") or 0)
        with open(tmp, "wb") as fh:
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                digest.update(chunk)
                fh.write(chunk)
                downloaded += len(chunk)
                if progress is not None:
                    progress(downloaded, total)
    tmp.replace(target)
    (cache / "variants.json.gz.sha256").write_text(digest.hexdigest(), encoding="utf-8")
    return target


def _open_binary(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rb")
    return open(path, "rb")


def _sha256_and_size(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _read_metadata(path: Path) -> dict[str, Any]:
    """Read the top-level scalars without parsing the arrays."""
    meta: dict[str, Any] = {}
    with _open_binary(path) as fh:
        for prefix, event, value in ijson.parse(fh):
            if prefix in ("timestamp", "version") and event == "string":
                meta[prefix] = value
            elif prefix == "variants" and event == "start_array":
                break
    return meta


def _iter_items(path: Path, prefix: str) -> Iterator[dict[str, Any]]:
    with _open_binary(path) as fh:
        yield from ijson.items(fh, prefix)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass
class SpellbookReport:
    import_id: str
    source: str = SOURCE_NAME
    source_path: str = ""
    source_url: str | None = None
    sha256: str | None = None
    version: str | None = None
    timestamp: str | None = None
    size_bytes: int = 0
    variants_seen: int = 0
    variants_kept: int = 0
    skipped_status: int = 0
    skipped_vintage: int = 0
    skipped_commander: int = 0
    skipped_alchemy: int = 0
    combos_written: int = 0
    templates_written: int = 0
    cards_oracle_matched: int = 0
    cards_name_matched: int = 0
    cards_unmatched: int = 0
    cards_excluded: int = 0
    pairs_written: int = 0
    full_variant_pairs: int = 0
    aliases_written: int = 0
    top_unmatched: list[tuple[str, int]] = field(default_factory=list)
    duration_s: float = 0.0

    def format(self) -> str:
        lines = [
            f"import id        : {self.import_id}",
            f"source           : {self.source} ({self.source_url or self.source_path})",
            f"version          : {self.version}  ({self.timestamp})",
            f"sha256           : {self.sha256}",
            f"size             : {self.size_bytes / 1e6:.1f} MB",
            "",
            f"variants seen    : {self.variants_seen}",
            f"variants kept    : {self.variants_kept}",
            f"  skipped status : {self.skipped_status}",
            f"  skipped vintage: {self.skipped_vintage}",
            f"  skipped cmdr   : {self.skipped_commander}",
            f"  skipped alchemy: {self.skipped_alchemy}",
            f"combos written   : {self.combos_written}",
            f"cards oracle-id  : {self.cards_oracle_matched}",
            f"cards by name    : {self.cards_name_matched}",
            f"cards unmatched  : {self.cards_unmatched}",
            f"cards excluded   : {self.cards_excluded}",
            f"templates        : {self.templates_written}",
            f"pairs written    : {self.pairs_written} (full-variant {self.full_variant_pairs})",
            f"aliases written  : {self.aliases_written}",
            f"duration         : {self.duration_s:.2f}s",
        ]
        if self.top_unmatched:
            lines.append("")
            lines.append("top unmatched names:")
            for name, count in self.top_unmatched[:20]:
                lines.append(f"  {count:>6}  {name}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Importer
# ---------------------------------------------------------------------------


def _zone_locations(use: dict[str, Any]) -> str:
    return json.dumps(use.get("zoneLocations") or [], sort_keys=True)


def _card_state(use: dict[str, Any]) -> str:
    states = {
        "battlefield": use.get("battlefieldCardState") or "",
        "exile": use.get("exileCardState") or "",
        "library": use.get("libraryCardState") or "",
        "graveyard": use.get("graveyardCardState") or "",
    }
    return json.dumps(states, sort_keys=True)


def _load_oracle_names(conn: sqlite3.Connection) -> dict[str, str]:
    """oracle_id -> local ``cards.normalized_name`` (last write wins)."""
    mapping: dict[str, str] = {}
    for row in conn.execute(
        "SELECT coi.oracle_id AS oracle_id, c.normalized_name AS normalized_name "
        "FROM card_oracle_ids coi JOIN cards c ON c.id = coi.card_id ORDER BY coi.id"
    ):
        mapping[str(row["oracle_id"])] = str(row["normalized_name"])
    return mapping


def _load_local_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row["normalized_name"])
        for row in conn.execute("SELECT DISTINCT normalized_name FROM cards")
        if row["normalized_name"]
    }


def _existing_source_ids(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row["source_id"])
        for row in conn.execute(
            "SELECT source_id FROM known_combos WHERE source = ?", (SOURCE_NAME,)
        )
    }


def _scryfall_legalities(scryfall, name: str | None) -> dict[str, Any] | None:
    if scryfall is None or not name:
        return None
    record = scryfall.lookup(front_face_name(name))
    if record is None:
        return None
    return record.get("legalities") or None


def import_spellbook(
    store: ExperimentStore,
    source_path_or_url: str | Path | None = None,
    *,
    download: bool = False,
    vintage_only: bool = True,
    vintage: VintageLegality | None = None,
    scryfall=None,
    format_path: str | Path | None = None,
    cache_dir: str | Path | None = None,
    limit: int | None = None,
    progress=None,
) -> SpellbookReport:
    """Stream Commander Spellbook variants into the ``known_*`` tables.

    ``source_path_or_url`` may be a local ``.json``/``.json.gz`` path or the bulk
    URL (with ``download=True``).  ``vintage`` overrides the legality checker;
    otherwise it is built from ``format_path`` (default Forge Vintage) or is
    permissive when that file is absent.  ``scryfall`` (a loaded
    :class:`~combo_discovery.corpus.importer.ScryfallSource`) supplies per-card
    ``legalities.vintage`` when present.
    """
    started = time.monotonic()
    report = SpellbookReport(import_id="", source=SOURCE_NAME)

    # 1. Resolve the source file.
    source_url: str | None = None
    if download or (isinstance(source_path_or_url, str) and source_path_or_url.startswith("http")):
        source_url = str(source_path_or_url or SPELLBOOK_BULK_URL)
        path = download_variants(cache_dir, url=source_url, progress=progress)
    elif source_path_or_url is not None:
        path = Path(source_path_or_url)
    else:
        path = download_variants(cache_dir, progress=progress)
        source_url = SPELLBOOK_BULK_URL

    import_id = str(uuid.uuid4())
    report.import_id = import_id
    report.source_path = str(path)
    report.source_url = source_url
    report.sha256, report.size_bytes = _sha256_and_size(path)
    meta = _read_metadata(path)
    report.version = meta.get("version")
    report.timestamp = meta.get("timestamp")

    if vintage is None:
        fmt = Path(format_path) if format_path else DEFAULT_VINTAGE_FORMAT
        vintage = VintageLegality.from_forge_format(fmt) if fmt.is_file() else VintageLegality.permissive()

    conn = store._conn
    counts: Counter[str] = Counter()
    unmatched = Counter()
    fetched_at = _utc_now()

    with store_module._DB_LOCK:
        conn.execute(
            "INSERT INTO import_runs (import_id, started_at, forge_root, forge_commit, "
            "scryfall_source, scryfall_sha256, scryfall_download_uri, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                import_id, fetched_at, None, None, "spellbook", report.sha256,
                source_url,
                json.dumps({
                    "version": report.version,
                    "timestamp": report.timestamp,
                    "size_bytes": report.size_bytes,
                    "source_path": str(path),
                }, sort_keys=True),
            ),
        )
        oracle_names = _load_oracle_names(conn)
        local_names = _load_local_names(conn)
        existing = _existing_source_ids(conn)

        card_rows: list[tuple[Any, ...]] = []
        pair_rows: list[tuple[Any, ...]] = []

        def flush() -> None:
            if card_rows:
                conn.executemany(_INSERT_COMBO_CARD, card_rows)
                card_rows.clear()
            if pair_rows:
                conn.executemany(_INSERT_COMBO_PAIR, pair_rows)
                pair_rows.clear()

        with conn:
            for variant in _iter_items(path, "variants.item"):
                counts["variants_seen"] += 1
                if limit is not None and counts["variants_seen"] > limit:
                    break
                status = str(variant.get("status") or "")
                if status not in _KEEP_STATUS:
                    counts["skipped_status"] += 1
                    continue
                uses = variant.get("uses") or []
                requires = variant.get("requires") or []

                # Resolve every concrete use first (also detects Alchemy).
                resolved: list[tuple[dict[str, Any], ResolvedCard]] = []
                excluded = False
                for use in uses:
                    card = use.get("card") or {}
                    res = resolve_spellbook_use(
                        card, used_face=use.get("usedFace"),
                        oracle_names=oracle_names, local_names=local_names,
                    )
                    if res.via == "excluded":
                        excluded = True
                        break
                    resolved.append((use, res))
                if excluded:
                    counts["skipped_alchemy"] += 1
                    continue

                if vintage_only:
                    legalities = variant.get("legalities") or {}
                    if not legalities.get("vintage"):
                        counts["skipped_vintage"] += 1
                        continue
                    if any(u.get("mustBeCommander") for u in uses) or any(
                        r.get("mustBeCommander") for r in requires
                    ):
                        counts["skipped_commander"] += 1
                        continue
                    illegal = False
                    for use, res in resolved:
                        sf_leg = _scryfall_legalities(scryfall, res.raw_name)
                        if not vintage.is_legal(
                            res.raw_name, oracle_id=res.oracle_id,
                            scryfall_legalities=sf_leg,
                        ):
                            illegal = True
                            break
                    if illegal:
                        counts["skipped_vintage"] += 1
                        continue

                source_id = str(variant.get("id") or "")
                if not source_id or source_id in existing:
                    continue

                resolved_names = sorted({r.normalized_name for _, r in resolved if r.normalized_name})
                combo_row = (
                    "commander_spellbook", source_id, report.version, report.timestamp,
                    fetched_at, status, variant.get("bracketTag"), variant.get("identity"),
                    variant.get("popularity"), variant.get("variantCount"),
                    variant.get("manaNeeded"), variant.get("manaValueNeeded"),
                    variant.get("easyPrerequisites"), variant.get("notablePrerequisites"),
                    variant.get("description"), variant.get("notes"),
                    1 if variant.get("spoiler") else 0,
                    json.dumps(variant.get("legalities") or {}, sort_keys=True),
                    json.dumps(variant.get("produces") or [], sort_keys=True),
                    json.dumps(variant.get("requires") or [], sort_keys=True),
                    len(uses), len(requires), len(variant.get("produces") or []),
                    combo_hash(resolved_names), import_id,
                )
                cur = conn.execute(_INSERT_COMBO, combo_row)
                combo_id = int(cur.lastrowid or 0)
                counts["combos_written"] += 1
                existing.add(source_id)

                for use, res in resolved:
                    card_rows.append((
                        combo_id, "use", res.raw_name, res.normalized_name, res.oracle_id,
                        res.spellbook_card_id, None, use.get("quantity"), use.get("usedFace"),
                        _zone_locations(use), 1 if use.get("mustBeCommander") else 0,
                        _card_state(use), import_id,
                    ))
                    if res.via == "oracle":
                        counts["cards_oracle_matched"] += 1
                    elif res.via == "name":
                        counts["cards_name_matched"] += 1
                    else:
                        counts["cards_unmatched"] += 1
                        if res.raw_name:
                            unmatched[res.raw_name] += 1
                for require in requires:
                    template = require.get("template") or {}
                    card_rows.append((
                        combo_id, "require", None, None, None, None,
                        template.get("id"), require.get("quantity"), None,
                        _zone_locations(require), 1 if require.get("mustBeCommander") else 0,
                        _card_state(require), import_id,
                    ))
                    counts["templates_written"] += 1

                if len(resolved_names) >= 2:
                    full = 1 if (len(uses) == 2 and not requires) else 0
                    for i, name_a in enumerate(resolved_names):
                        for name_b in resolved_names[i + 1:]:
                            pair_rows.append((
                                combo_id, pair_hash(name_a, name_b), full,
                                "commander_spellbook", import_id,
                            ))
                            counts["pairs_written"] += 1
                            if full:
                                counts["full_variant_pairs"] += 1

                if counts["combos_written"] % 2000 == 0:
                    flush()
            flush()

            # Aliases (top-level array, after variants).
            alias_rows = []
            for alias in _iter_items(path, "aliases.item"):
                alias_rows.append((
                    str(alias.get("id") or ""), alias.get("variant"), import_id,
                ))
                if len(alias_rows) >= 5000:
                    conn.executemany(_INSERT_ALIAS, alias_rows)
                    counts["aliases_written"] += len(alias_rows)
                    alias_rows.clear()
            if alias_rows:
                conn.executemany(_INSERT_ALIAS, alias_rows)
                counts["aliases_written"] += len(alias_rows)

    report.variants_seen = counts["variants_seen"]
    report.variants_kept = counts["combos_written"]
    report.skipped_status = counts["skipped_status"]
    report.skipped_vintage = counts["skipped_vintage"]
    report.skipped_commander = counts["skipped_commander"]
    report.skipped_alchemy = counts["skipped_alchemy"]
    report.combos_written = counts["combos_written"]
    report.templates_written = counts["templates_written"]
    report.cards_oracle_matched = counts["cards_oracle_matched"]
    report.cards_name_matched = counts["cards_name_matched"]
    report.cards_unmatched = counts["cards_unmatched"]
    report.cards_excluded = counts["skipped_alchemy"]
    report.pairs_written = counts["pairs_written"]
    report.full_variant_pairs = counts["full_variant_pairs"]
    report.aliases_written = counts["aliases_written"]
    report.top_unmatched = unmatched.most_common(50)
    report.duration_s = time.monotonic() - started
    return report


_INSERT_COMBO = (
    "INSERT INTO known_combos (source, source_id, source_version, source_timestamp, "
    "fetched_at, status, bracket_tag, identity, popularity, variant_count, mana_needed, "
    "mana_value_needed, easy_prereqs, notable_prereqs, description, notes, spoiler, "
    "legalities_json, produces_json, requires_json, n_uses, n_requires, n_produces, "
    "canonical_hash, import_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
    "?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

_INSERT_COMBO_CARD = (
    "INSERT INTO known_combo_cards (combo_id, role, raw_name, normalized_name, oracle_id, "
    "spellbook_card_id, template_id, quantity, used_face, zone_locations, "
    "must_be_commander, card_state, import_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

_INSERT_COMBO_PAIR = (
    "INSERT INTO known_combo_pairs (combo_id, pair_hash, is_full_variant, source, "
    "import_id) VALUES (?, ?, ?, ?, ?)"
)

_INSERT_ALIAS = (
    "INSERT INTO known_aliases (alias_id, canonical_id, import_id) VALUES (?, ?, ?)"
)


__all__ = [
    "DEFAULT_VINTAGE_FORMAT",
    "SPELLBOOK_BULK_URL",
    "SOURCE_NAME",
    "SpellbookReport",
    "VintageLegality",
    "download_variants",
    "import_spellbook",
]
