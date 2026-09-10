"""Card-corpus importer: Forge scripts (+ optional Scryfall oracle data) -> SQLite.

The importer walks ``cardsfolder/<letter>/*.txt``, hashes and parses every
script, optionally enriches it from a Scryfall ``oracle_cards`` bulk download,
and writes schema v2 (see :func:`combo_discovery.store._migration_2`) through the
append-only :class:`~combo_discovery.store.ExperimentStore`.

Design constraints:

* **Network is optional and isolated.**  :class:`ScryfallSource` owns every
  socket; the importer only calls :meth:`ScryfallSource.load`, which reads the
  cache or — only when the caller asked for it — downloads once.  If Scryfall is
  absent or unreachable the import still completes with
  ``scryfall_source='forge_script'`` and no oracle enrichment.
* **Append-only per import.**  Each call inserts one ``import_runs`` row and all
  child rows reference its ``import_id``; a re-import appends a complete new
  snapshot.  Nothing is mutated or removed.
* **Single transaction.**  All writes happen inside one transaction with
  ``executemany`` batches, so a 33k-file import is one commit.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import unicodedata
import urllib.request
import uuid
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .. import store as store_module
from ..store import ExperimentStore
from . import parser as corpus_parser
from .parser import ParsedCard

# ---------------------------------------------------------------------------
# Name normalization
# ---------------------------------------------------------------------------

_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")


def normalize_name(name: str) -> str:
    """Lowercase, accent-fold and strip punctuation for name matching.

    ``"Kiki-Jiki, Mirror Breaker"`` and ``"Kiki Jiki Mirror Breaker"`` collapse
    to the same key, as do ``"Bind // Liberate"`` and its normalized parts.
    """
    if not name:
        return ""
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    cleaned = _NON_ALNUM.sub(" ", ascii_only.replace("//", " ").lower())
    return " ".join(cleaned.split())


# ---------------------------------------------------------------------------
# Scryfall bulk source (the only network-touching code in this module)
# ---------------------------------------------------------------------------


class ScryfallSource:
    """Lazy access to a cached Scryfall ``oracle_cards`` bulk file.

    ``download()`` streams the bulk document from
    ``https://api.scryfall.com/bulk-data`` (resolving ``oracle_cards`` via the
    API JSON) into ``~/.cache/combo-discovery/scryfall/`` and records the
    SHA-256 of the streamed bytes.  ``load()`` turns the cache into a
    name-keyed index; it never downloads unless ``auto_download=True``.
    """

    BULK_API = "https://api.scryfall.com/bulk-data"
    DEFAULT_CACHE_DIR = Path.home() / ".cache" / "combo-discovery" / "scryfall"
    USER_AGENT = "combo-discovery/0.1 (card corpus importer)"

    def __init__(
        self,
        cache_dir: str | Path | None = None,
        *,
        auto_download: bool = False,
        timeout: float = 120.0,
        progress: Callable[[int, int], None] | None = None,
    ) -> None:
        self.cache_dir = Path(cache_dir) if cache_dir else self.DEFAULT_CACHE_DIR
        self.auto_download = auto_download
        self.timeout = timeout
        self.progress = progress
        self.download_uri: str | None = None
        self.error: str | None = None
        self.available = False
        self._index: dict[str, dict[str, Any]] | None = None
        self._sha256: str | None = None

    # -- cache paths --------------------------------------------------------

    def cache_path(self) -> Path:
        return self.cache_dir / "oracle_cards.json"

    def sha_path(self) -> Path:
        return self.cache_dir / "oracle_cards.sha256"

    def has_cache(self) -> bool:
        return self.cache_path().is_file()

    @property
    def sha256(self) -> str | None:
        if self._sha256 is None and self.sha_path().is_file():
            self._sha256 = self.sha_path().read_text(encoding="utf-8").strip() or None
        return self._sha256

    def metadata(self) -> dict[str, Any]:
        return {
            "cache_path": str(self.cache_path()),
            "download_uri": self.download_uri,
            "sha256": self.sha256,
            "available": self.available,
            "auto_download": self.auto_download,
        }

    # -- network ------------------------------------------------------------

    def download(self, *, force: bool = False) -> Path:
        """Download ``oracle_cards`` into the cache and return its path."""
        target = self.cache_path()
        if target.is_file() and not force:
            return target
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        meta = self._get_json(self.BULK_API)
        entry = next(
            (e for e in meta.get("data", []) if e.get("type") == "oracle_cards"),
            None,
        )
        if entry is None or not entry.get("download_uri"):
            raise RuntimeError("Scryfall bulk-data has no oracle_cards download_uri")
        self.download_uri = str(entry["download_uri"])

        tmp = target.with_name(target.name + ".tmp")
        digest = hashlib.sha256()
        downloaded = 0
        request = urllib.request.Request(
            self.download_uri, headers={"User-Agent": self.USER_AGENT}
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            total = int(response.headers.get("Content-Length") or 0)
            with open(tmp, "wb") as fh:
                while True:
                    chunk = response.read(1 << 20)
                    if not chunk:
                        break
                    digest.update(chunk)
                    fh.write(chunk)
                    downloaded += len(chunk)
                    if self.progress is not None:
                        self.progress(downloaded, total)
        tmp.replace(target)
        self._sha256 = digest.hexdigest()
        self.sha_path().write_text(self._sha256, encoding="utf-8")
        return target

    def _get_json(self, url: str) -> dict[str, Any]:
        request = urllib.request.Request(
            url, headers={"User-Agent": self.USER_AGENT, "Accept": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    # -- loading ------------------------------------------------------------

    def load(self) -> bool:
        """Load the cache into a name index.  Returns availability.

        Downloads only when there is no cache *and* ``auto_download`` is set.
        Any failure is recorded on :attr:`error` and returns ``False``.
        """
        if self._index is not None:
            return True
        try:
            path = self.cache_path()
            if not path.is_file():
                if not self.auto_download:
                    return False
                path = self.download()
            with open(path, encoding="utf-8") as fh:
                records = json.load(fh)
        except Exception as exc:  # noqa: BLE001 - offline/no-cache must not fail an import
            self.error = f"{type(exc).__name__}: {exc}"
            return False

        index: dict[str, dict[str, Any]] = {}
        for record in records:
            kept = _keep_record(record)
            for name in _record_names(record):
                index.setdefault(normalize_name(name), kept)
        self._index = index
        self.available = True
        return True

    def lookup(self, name: str) -> dict[str, Any] | None:
        if self._index is None:
            return None
        return self._index.get(normalize_name(name))

    def __len__(self) -> int:
        return len(self._index or {})


def _keep_record(record: dict[str, Any]) -> dict[str, Any]:
    """Only the Scryfall fields the corpus schema persists."""
    faces = []
    for face in record.get("card_faces") or []:
        faces.append(
            {
                "name": face.get("name") or "",
                "oracle_text": face.get("oracle_text") or "",
                "type_line": face.get("type_line") or "",
                "mana_cost": face.get("mana_cost") or "",
                "colors": list(face.get("colors") or []),
            }
        )
    return {
        "name": record.get("name") or "",
        "oracle_text": record.get("oracle_text") or "",
        "type_line": record.get("type_line") or "",
        "mana_cost": record.get("mana_cost") or "",
        "colors": list(record.get("colors") or []),
        "legalities": record.get("legalities") or {},
        "set": record.get("set") or "",
        "collector_number": record.get("collector_number") or "",
        "rarity": record.get("rarity") or "",
        "layout": record.get("layout") or "",
        "oracle_id": record.get("oracle_id") or "",
        "card_faces": faces,
    }


def _record_names(record: dict[str, Any]) -> Iterator[str]:
    name = record.get("name")
    if name:
        yield name
    for face in record.get("card_faces") or []:
        face_name = face.get("name")
        if face_name:
            yield face_name


@runtime_checkable
class OracleSource(Protocol):
    """Structural type for an oracle-text provider (Scryfall, or a test fake)."""

    @property
    def sha256(self) -> str | None: ...

    @property
    def download_uri(self) -> str | None: ...

    def load(self) -> bool: ...

    def lookup(self, name: str) -> dict[str, Any] | None: ...


# ---------------------------------------------------------------------------
# Cardsfolder discovery
# ---------------------------------------------------------------------------

# Tried in order against the supplied root; the Forge checkout layout first,
# then the bare cardsfolder, then the root itself (for synthetic test folders).
_CARDSFOLDER_CANDIDATES = (
    Path("forge-gui/res/cardsfolder"),
    Path("res/cardsfolder"),
    Path("cardsfolder"),
)


def resolve_cardsfolder(forge_root: str | Path) -> Path:
    root = Path(forge_root)
    for relative in _CARDSFOLDER_CANDIDATES:
        candidate = root / relative
        if candidate.is_dir():
            return candidate
    return root


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass
class ImportReport:
    """Outcome of one :func:`import_corpus` call."""

    import_id: str
    forge_root: str
    cardsfolder: str
    scryfall_source: str
    scryfall_sha256: str | None
    cards_total: int = 0
    cards_imported: int = 0
    parse_ok: int = 0
    parse_error_cards: int = 0
    parse_error_lines: int = 0
    faces: int = 0
    effects: int = 0
    abilities: int = 0
    triggers: int = 0
    statics: int = 0
    replacements: int = 0
    svars: int = 0
    aliases: int = 0
    scryfall_matched: int = 0
    scryfall_unmatched: int = 0
    missing_oracle: int = 0
    duration_s: float = 0.0
    coverage: dict[str, int] = field(default_factory=dict)
    errors_sample: list[dict[str, Any]] = field(default_factory=list)

    def format(self) -> str:
        lines = [
            f"import id        : {self.import_id}",
            f"forge root       : {self.forge_root}",
            f"cardsfolder      : {self.cardsfolder}",
            f"scryfall source  : {self.scryfall_source}"
            + (f" (sha256 {self.scryfall_sha256[:12]}…)" if self.scryfall_sha256 else ""),
            "",
            f"cards total      : {self.cards_total}",
            f"cards imported   : {self.cards_imported}",
            f"parse ok         : {self.parse_ok}",
            f"parse error cards: {self.parse_error_cards} ({self.parse_error_lines} lines)",
            f"faces            : {self.faces}",
            f"abilities (K)    : {self.abilities}",
            f"effects          : {self.effects}"
            f"  [trigger {self.triggers} / static {self.statics} / replacement {self.replacements}]",
            f"svars            : {self.svars}",
            f"aliases          : {self.aliases}",
            f"scryfall matched : {self.scryfall_matched}",
            f"scryfall missing : {self.scryfall_unmatched}",
            f"missing oracle   : {self.missing_oracle}",
            f"duration         : {self.duration_s:.2f}s",
        ]
        if self.coverage:
            lines.append("")
            lines.append("coverage:")
            for metric, value in sorted(self.coverage.items()):
                lines.append(f"  {metric:<24} {value}")
        if self.errors_sample:
            lines.append("")
            lines.append("first parse errors:")
            for sample in self.errors_sample[:10]:
                lines.append(
                    f"  {sample['source']}:{sample['line']}  {sample['reason']}"
                )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Importer
# ---------------------------------------------------------------------------

# How many child rows to buffer before an executemany flush.
_BATCH = 2000
# Number of verb/mode coverage rows kept per metric.
_TOP_N = 100
# Verbose (raw script) is stored in card_scripts.raw; keep bytes bounded.
_MAX_RAW_BYTES = 512 * 1024


def _http_error_sample(path: Path, exc: BaseException) -> dict[str, Any]:
    return {"source": str(path), "line": 0, "reason": f"{type(exc).__name__}: {exc}"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _match_names(parsed: ParsedCard) -> list[str]:
    names: list[str] = []
    if parsed.faces:
        front = parsed.faces[0].name
        if front:
            names.append(front)
        if len(parsed.faces) > 1:
            combined = " // ".join(face.name for face in parsed.faces if face.name)
            if combined:
                names.append(combined)
        names.extend(face.name for face in parsed.faces if face.name)
    if parsed.flavor_name:
        names.append(parsed.flavor_name)
    seen: set[str] = set()
    ordered: list[str] = []
    for name in names:
        key = normalize_name(name)
        if key and key not in seen:
            seen.add(key)
            ordered.append(name)
    return ordered


def _card_aliases(parsed: ParsedCard, oracle: dict[str, Any] | None) -> list[tuple[str, str]]:
    aliases: list[tuple[str, str]] = []
    for face in parsed.faces:
        if face.name:
            aliases.append((face.name, "face"))
    if len(parsed.faces) > 1:
        combined = " // ".join(face.name for face in parsed.faces if face.name)
        if combined:
            aliases.append((combined, "combined"))
    if parsed.flavor_name:
        aliases.append((parsed.flavor_name, "flavor"))
    if oracle and oracle.get("name"):
        aliases.append((str(oracle["name"]), "scryfall"))

    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for alias, kind in aliases:
        key = normalize_name(alias)
        if key and key not in seen:
            seen.add(key)
            unique.append((alias, kind))
    return unique


def _effect_rows(parsed: ParsedCard) -> Iterator[tuple[Any, bool, str | None]]:
    """Yield ``(effect, is_svar, svar_name)`` for direct and SVar effects."""
    for effect in parsed.effects:
        yield effect, False, None
    for svar in parsed.svars.values():
        if not svar.is_effect:
            continue
        effect = corpus_parser.parse_effect(
            "A", svar.expression, svar.line, svar.face, svar_name=svar.name
        )
        if effect is not None:
            yield effect, True, svar.name


def import_corpus(
    store: ExperimentStore,
    forge_root: str | Path,
    scryfall: OracleSource | None = None,
    card_filter: Callable[[Path], bool] | None = None,
    *,
    limit: int | None = None,
    forge_commit: str | None = None,
) -> ImportReport:
    """Import every script under ``forge_root``'s cardsfolder into ``store``.

    ``card_filter`` (when given) receives each script ``Path`` and returns
    ``True`` to keep it; ``limit`` then truncates the ordered list (handy for
    smoke tests).  A fresh ``import_id`` is created on every call.
    """
    started = time.monotonic()
    import_id = str(uuid.uuid4())
    cardsfolder = resolve_cardsfolder(forge_root)
    files = sorted(path for path in cardsfolder.rglob("*.txt") if path.is_file())
    if card_filter is not None:
        files = [path for path in files if card_filter(path)]
    if limit is not None:
        files = files[: max(0, int(limit))]

    report = ImportReport(
        import_id=import_id,
        forge_root=str(forge_root),
        cardsfolder=str(cardsfolder),
        scryfall_source="forge_script",
        scryfall_sha256=None,
    )

    oracle_source: OracleSource | None = None
    if scryfall is not None:
        try:
            if scryfall.load():
                oracle_source = scryfall
                report.scryfall_source = "scryfall"
                report.scryfall_sha256 = scryfall.sha256
        except Exception:  # noqa: BLE001 - optional enrichment only
            oracle_source = None

    counts: Counter[str] = Counter()
    verb_counts: Counter[str] = Counter()
    trigger_modes: Counter[str] = Counter()
    static_modes: Counter[str] = Counter()
    replacement_modes: Counter[str] = Counter()

    conn = store._conn
    sql = _Sql
    with store_module._DB_LOCK:
        with conn:
            conn.execute(
                "INSERT INTO import_runs (import_id, started_at, forge_root, "
                "forge_commit, scryfall_source, scryfall_sha256, scryfall_download_uri) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    import_id,
                    _utc_now(),
                    str(forge_root),
                    forge_commit,
                    report.scryfall_source,
                    report.scryfall_sha256,
                    getattr(scryfall, "download_uri", None) if oracle_source else None,
                ),
            )

            face_rows: list[tuple[Any, ...]] = []
            effect_rows: list[tuple[Any, ...]] = []
            alias_rows: list[tuple[Any, ...]] = []
            pending = 0

            for path in files:
                try:
                    data = path.read_bytes()
                except OSError as exc:
                    counts["parse_error_cards"] += 1
                    report.errors_sample.append(_http_error_sample(path, exc))
                    continue

                sha = hashlib.sha256(data).hexdigest()
                text = data.decode("utf-8", errors="replace")
                parsed = corpus_parser.parse_script(text, source=str(path))
                relative = _relative(path, cardsfolder)

                oracle = None
                if oracle_source is not None:
                    for name in _match_names(parsed):
                        oracle = oracle_source.lookup(name)
                        if oracle is not None:
                            break
                    if oracle is not None:
                        counts["scryfall_matched"] += 1
                    else:
                        counts["scryfall_unmatched"] += 1

                front = parsed.faces[0] if parsed.faces else corpus_parser.Face()
                set_code = oracle.get("set") if oracle else None
                rarity = oracle.get("rarity") if oracle else None
                collector = oracle.get("collector_number") if oracle else None
                oracle_id = oracle.get("oracle_id") if oracle else None
                layout = oracle.get("layout") if oracle else None
                type_line = (oracle or {}).get("type_line") or front.types
                oracle_text = (oracle or {}).get("oracle_text") or front.oracle
                mana_cost = (oracle or {}).get("mana_cost") or front.mana_cost
                colors = _colors_text(oracle, front.colors)

                cur = conn.execute(sql.insert_card, (
                    import_id, relative, sha, front.name, normalize_name(front.name),
                    mana_cost, type_line, oracle_text, colors,
                    front.pt, front.loyalty, front.defense,
                    set_code, rarity, collector, oracle_id, layout,
                    parsed.alternate_mode, parsed.meld_pair, parsed.copy_face_from,
                    len(parsed.effects), len(parsed.faces),
                    1 if parsed.is_multiface else 0, 1 if parsed.parse_ok else 0,
                ))
                card_id = int(cur.lastrowid or 0)

                script_cur = conn.execute(sql.insert_script, (
                    import_id, card_id, relative, sha,
                    text.count("\n") + 1, len(parsed.faces), len(parsed.effects),
                    len(parsed.svars), len(parsed.abilities), parsed.alternate_mode,
                    1 if parsed.parse_ok else 0, len(parsed.parse_errors),
                    json.dumps([e.as_dict() for e in parsed.parse_errors], sort_keys=True),
                    text[:_MAX_RAW_BYTES],
                ))
                script_id = int(script_cur.lastrowid or 0)

                _append_faces(face_rows, import_id, card_id, parsed, oracle)
                _append_effects(
                    effect_rows, import_id, card_id, script_id, parsed,
                    counts, verb_counts, trigger_modes, static_modes, replacement_modes,
                )
                aliases = _card_aliases(parsed, oracle)
                counts["aliases"] += len(aliases)
                for alias, kind in aliases:
                    alias_rows.append(
                        (import_id, card_id, alias, normalize_name(alias), kind)
                    )

                counts["cards_total"] += 1
                report.cards_imported += 1
                if parsed.parse_ok:
                    counts["parse_ok"] += 1
                else:
                    counts["parse_error_cards"] += 1
                    counts["parse_error_lines"] += len(parsed.parse_errors)
                    if len(report.errors_sample) < 25:
                        report.errors_sample.extend(
                            {
                                "source": relative,
                                "line": err.line,
                                "reason": err.reason,
                            }
                            for err in parsed.parse_errors[:3]
                        )
                counts["faces"] += len(parsed.faces)
                counts["abilities"] += len(parsed.abilities)
                counts["svars"] += len(parsed.svars)
                if not oracle_text.strip():
                    counts["missing_oracle"] += 1

                pending += 1
                if pending >= _BATCH:
                    _flush(conn, face_rows, effect_rows, alias_rows)
                    pending = 0

            _flush(conn, face_rows, effect_rows, alias_rows)

            coverage = _coverage(
                counts, trigger_modes, static_modes, replacement_modes,
                import_id, int((time.monotonic() - started) * 1000),
            )
            conn.executemany(
                "INSERT INTO corpus_coverage (import_id, metric, key, value, detail_json) "
                "VALUES (?, ?, ?, ?, ?)",
                coverage,
            )
            conn.executemany(
                "INSERT INTO corpus_coverage (import_id, metric, key, value, detail_json) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (import_id, "effects_by_verb", verb, value, None)
                    for verb, value in verb_counts.most_common(_TOP_N)
                ]
                + [
                    (import_id, "triggers_by_mode", mode, value, None)
                    for mode, value in trigger_modes.most_common(_TOP_N)
                ]
                + [
                    (import_id, "statics_by_mode", mode, value, None)
                    for mode, value in static_modes.most_common(_TOP_N)
                ]
                + [
                    (import_id, "replacements_by_mode", mode, value, None)
                    for mode, value in replacement_modes.most_common(_TOP_N)
                ],
            )

    report.duration_s = time.monotonic() - started
    report.cards_total = counts["cards_total"]
    report.cards_imported = counts["cards_total"]
    report.parse_ok = counts["parse_ok"]
    report.parse_error_cards = counts["parse_error_cards"]
    report.parse_error_lines = counts["parse_error_lines"]
    report.faces = counts["faces"]
    report.effects = counts["effects"]
    report.abilities = counts["abilities"]
    report.triggers = counts["triggers"]
    report.statics = counts["statics"]
    report.replacements = counts["replacements"]
    report.svars = counts["svars"]
    report.aliases = counts["aliases"]
    report.scryfall_matched = counts["scryfall_matched"]
    report.scryfall_unmatched = counts["scryfall_unmatched"]
    report.missing_oracle = counts["missing_oracle"]
    report.coverage = {
        metric: value
        for _import_id, metric, key, value, _detail in coverage
        if key is None
    }
    return report


def _relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _colors_text(oracle: dict[str, Any] | None, fallback: str) -> str:
    if oracle and oracle.get("colors"):
        return "".join(str(c) for c in oracle["colors"])
    return fallback


def _append_faces(
    face_rows: list[tuple[Any, ...]],
    import_id: str,
    card_id: int,
    parsed: ParsedCard,
    oracle: dict[str, Any] | None,
) -> None:
    oracle_faces = (oracle or {}).get("card_faces") or []
    for index, face in enumerate(parsed.faces):
        sf_face = oracle_faces[index] if index < len(oracle_faces) else None
        face_rows.append((
            import_id, card_id, index,
            face.name or (parsed.faces[0].name if parsed.faces else ""),
            (sf_face or {}).get("mana_cost") or face.mana_cost,
            (sf_face or {}).get("type_line") or face.types,
            (sf_face or {}).get("oracle_text") or face.oracle,
            _colors_text(sf_face, face.colors),
            face.pt, face.loyalty, face.defense, face.text, face.marker,
            1 if sf_face else 0,
        ))


def _append_effects(
    effect_rows: list[tuple[Any, ...]],
    import_id: str,
    card_id: int,
    script_id: int,
    parsed: ParsedCard,
    counts: Counter[str],
    verb_counts: Counter[str],
    trigger_modes: Counter[str],
    static_modes: Counter[str],
    replacement_modes: Counter[str],
) -> None:
    for effect, is_svar, svar_name in _effect_rows(parsed):
        effect_rows.append((
            import_id, card_id, script_id, effect.face,
            effect.kind, effect.verb, effect.ability_type, effect.zone,
            1 if effect.is_optional else 0, 1 if is_svar else 0, svar_name,
            effect.description, effect.line,
            json.dumps(effect.params, sort_keys=True),
        ))
        counts["effects"] += 1
        counts[f"{effect.kind}s"] += 1
        verb_counts[effect.verb] += 1
        if effect.kind == "trigger":
            trigger_modes[effect.verb] += 1
        elif effect.kind == "static":
            static_modes[effect.verb] += 1
        elif effect.kind == "replacement":
            replacement_modes[effect.verb] += 1


def _flush(
    conn: sqlite3.Connection,
    face_rows: list[tuple[Any, ...]],
    effect_rows: list[tuple[Any, ...]],
    alias_rows: list[tuple[Any, ...]],
) -> None:
    if face_rows:
        conn.executemany(_Sql.insert_face, face_rows)
        face_rows.clear()
    if effect_rows:
        conn.executemany(_Sql.insert_effect, effect_rows)
        effect_rows.clear()
    if alias_rows:
        conn.executemany(_Sql.insert_alias, alias_rows)
        alias_rows.clear()


def _coverage(
    counts: Counter[str],
    trigger_modes: Counter[str],
    static_modes: Counter[str],
    replacement_modes: Counter[str],
    import_id: str,
    duration_ms: int,
) -> list[tuple[str, str, str | None, int, str | None]]:
    metrics: list[tuple[str, int]] = [
        ("total_cards", counts["cards_total"]),
        ("parse_ok", counts["parse_ok"]),
        ("parse_error_cards", counts["parse_error_cards"]),
        ("parse_error_lines", counts["parse_error_lines"]),
        ("faces", counts["faces"]),
        ("effects", counts["effects"]),
        ("abilities", counts["abilities"]),
        ("triggers", counts["triggers"]),
        ("statics", counts["statics"]),
        ("replacements", counts["replacements"]),
        ("svars", counts["svars"]),
        ("aliases", counts["aliases"]),
        ("scryfall_matched", counts["scryfall_matched"]),
        ("scryfall_unmatched", counts["scryfall_unmatched"]),
        ("missing_oracle", counts["missing_oracle"]),
        ("trigger_modes", len(trigger_modes)),
        ("static_modes", len(static_modes)),
        ("replacement_modes", len(replacement_modes)),
        ("duration_ms", duration_ms),
    ]
    return [(import_id, metric, None, value, None) for metric, value in metrics]


class _Sql:
    """Pre-built INSERT statements (kept together for readability)."""

    insert_card = (
        "INSERT INTO cards (import_id, script_path, file_sha256, name, "
        "normalized_name, mana_cost, type_line, oracle_text, colors, pt, loyalty, "
        "defense, set_code, rarity, collector_number, scryfall_oracle_id, layout, "
        "alternate_mode, meld_pair, copy_face_from, effect_count, face_count, "
        "is_multiface, parse_ok) VALUES (" + ", ".join(["?"] * 24) + ")"
    )
    insert_script = (
        "INSERT INTO card_scripts (import_id, card_id, script_path, file_sha256, "
        "line_count, face_count, effect_count, svar_count, ability_count, "
        "alternate_mode, parse_ok, parse_error_count, error_json, raw) VALUES ("
        + ", ".join(["?"] * 14) + ")"
    )
    insert_face = (
        "INSERT INTO card_faces (import_id, card_id, face_index, name, mana_cost, "
        "type_line, oracle_text, colors, pt, loyalty, defense, text, marker, "
        "has_scryfall) VALUES (" + ", ".join(["?"] * 14) + ")"
    )
    insert_alias = (
        "INSERT INTO card_aliases (import_id, card_id, alias, normalized_alias, "
        "alias_kind) VALUES (?, ?, ?, ?, ?)"
    )
    insert_effect = (
        "INSERT INTO card_effects (import_id, card_id, script_id, face_index, "
        "effect_kind, verb_or_mode, ability_type, zone, is_optional, is_svar, "
        "svar_name, description, line_no, params_json) VALUES ("
        + ", ".join(["?"] * 14) + ")"
    )


# Public aliases used by the tests/report consumers.
TOP_N = _TOP_N

__all__ = [
    "ImportReport",
    "ScryfallSource",
    "import_corpus",
    "normalize_name",
    "resolve_cardsfolder",
]
