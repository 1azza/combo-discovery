"""Scryfall oracle-id backfill for the local Forge corpus.

``cards.scryfall_oracle_id`` is blank for every corpus row (the import ran in
Forge-script-only mode), and the store is append-only, so the mapping lives in
the schema-v4 ``card_oracle_ids`` table instead of updating ``cards``.  This
reuses :class:`combo_discovery.corpus.importer.ScryfallSource` for the cached
``oracle_cards`` bulk download and name index.
"""

from __future__ import annotations

import json
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from ..cards import utc_now as _utc_now
from .. import store as store_module
from ..store import ExperimentStore


@dataclass
class OracleBackfillReport:
    import_id: str
    source: str = "scryfall"
    scryfall_sha256: str | None = None
    cards_seen: int = 0
    matched: int = 0
    unmatched: int = 0
    top_unmatched: list[tuple[str, int]] = field(default_factory=list)
    duration_s: float = 0.0

    def format(self) -> str:
        lines = [
            f"import id        : {self.import_id}",
            f"source           : {self.source}",
            f"scryfall sha256  : {self.scryfall_sha256}",
            f"cards seen       : {self.cards_seen}",
            f"matched          : {self.matched}",
            f"unmatched        : {self.unmatched}",
            f"duration         : {self.duration_s:.2f}s",
        ]
        if self.top_unmatched:
            lines.append("top unmatched:")
            for name, count in self.top_unmatched[:20]:
                lines.append(f"  {count:>6}  {name}")
        return "\n".join(lines)


def backfill_oracle_ids(
    store: ExperimentStore,
    scryfall,
    *,
    source: str = "scryfall",
    limit: int | None = None,
    import_id: str | None = None,
) -> OracleBackfillReport:
    """Populate ``card_oracle_ids`` from a loaded Scryfall source.

    ``scryfall`` must already be loaded (``scryfall.load()``); a fresh
    ``import_runs`` row records the backfill.  Matching is by the corpus
    ``cards.name`` (front face for multi-face cards), which the Scryfall index
    resolves via the combined name or either face name.
    """
    started = time.monotonic()
    conn = store._conn
    if not scryfall.load():
        raise RuntimeError(
            "Scryfall source unavailable; download/point at oracle_cards first"
        )
    import_id = import_id or str(uuid.uuid4())
    report = OracleBackfillReport(
        import_id=import_id, source=source, scryfall_sha256=scryfall.sha256
    )
    unmatched: Counter[str] = Counter()
    rows: list[tuple[Any, ...]] = []
    now = _utc_now()
    with store_module._DB_LOCK:
        conn.execute(
            "INSERT INTO import_runs (import_id, started_at, forge_root, forge_commit, "
            "scryfall_source, scryfall_sha256, scryfall_download_uri, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (import_id, now, None, None, "scryfall_oracle_backfill",
             scryfall.sha256, getattr(scryfall, "download_uri", None),
             json.dumps({"backfill": "oracle_ids", "source": source}, sort_keys=True)),
        )
        with conn:
            for row in conn.execute(
                "SELECT id, name, normalized_name FROM cards ORDER BY id"
            ):
                if limit is not None and report.cards_seen >= limit:
                    break
                report.cards_seen += 1
                card_id = int(row["id"])
                name = row["name"] or ""
                record = scryfall.lookup(name)
                oracle_id = (record or {}).get("oracle_id")
                if oracle_id:
                    rows.append((import_id, card_id, oracle_id, source, now))
                    report.matched += 1
                else:
                    report.unmatched += 1
                    if name:
                        unmatched[name] += 1
                if len(rows) >= 2000:
                    conn.executemany(
                        "INSERT INTO card_oracle_ids (import_id, card_id, oracle_id, "
                        "source, imported_at) VALUES (?, ?, ?, ?, ?)",
                        rows,
                    )
                    rows.clear()
            if rows:
                conn.executemany(
                    "INSERT INTO card_oracle_ids (import_id, card_id, oracle_id, "
                    "source, imported_at) VALUES (?, ?, ?, ?, ?)",
                    rows,
                )
    report.top_unmatched = unmatched.most_common(50)
    report.duration_s = time.monotonic() - started
    return report


__all__ = ["OracleBackfillReport", "backfill_oracle_ids"]
