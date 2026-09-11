#!/usr/bin/env python
"""Read-only demonstration of the interaction algebra on the real corpus.

Builds ability signatures for the Vintage pool, derives the interaction graph,
runs the cycle queries and prints a transparent report.  The database is opened
``mode=ro`` and never written; enrichment weights are read from the latest
persisted run.  Run with::

    uv run python scripts/demo_algebra.py [--db research.db] [--max-len 2]

Pruning used for the full corpus (documented in ``ontology.links.LinkOptions``):

* ``scope="retrigger"`` — only explore ``enables``/``satisfies`` links around
  the (rare) ``re_trigger`` engines, not the full pairwise graph;
* ``closure_depth=2`` — a listener that cannot feed the engine directly cannot
  form a 2-card cycle, so only those links are materialised (set 3 for 3-card
  cycles at a large runtime cost);
* a Vintage-legal, loop-machinery pool (untap / copy / extra phase /
  reanimation / sacrifice) instead of all 33k cards;
* ``max_retrigger`` bounds input-less engines, ``max_enables`` bounds each
  consumer's producer fan-out.
"""

from __future__ import annotations

import argparse
import sqlite3
import time
from collections import Counter
from pathlib import Path

from combo_discovery.corpus.spellbook import DEFAULT_VINTAGE_FORMAT, VintageLegality
from combo_discovery.ontology.cycles import (
    build_graph,
    find_combos,
    load_motif_weights,
    vintage_pool,
)
from combo_discovery.ontology.extractor import load_contexts
from combo_discovery.ontology.links import LinkOptions
from combo_discovery.ontology.ports import build_signatures

KIKI = "Kiki-Jiki, Mirror Breaker"
KIKI_PARTNERS = (
    "Deceiver Exarch", "Pestermite", "Corridor Monitor",
    "Zealous Conscripts", "Fear of Missing Out",
)
LAND_UNTAPPERS = (
    "Bumi, Unleashed", "Zacama, Primal Calamity", "Cloud of Faeries",
    "Palinchron", "Peregrine Drake", "Great Whale",
    "Nissa, Vastwood Seer", "Nissa, Who Shakes the World",
    "Woodcaller Automaton",
)


def _loop_machinery(sig) -> bool:
    """Cards that carry a loop-relevant typed port (the demo's scoped pool)."""
    if any(p.kind in ("copy_permanent", "extra_phase", "untap") for p in sig.produces):
        return True
    if any(p.kind == "zone_move" and str(p.params.get("to") or "").lower() == "battlefield"
           for p in sig.produces):
        return True
    if any(p.kind == "sacrifice" for p in sig.consumes):
        return True
    if any(p.kind == "zone_move" and "graveyard" in str(p.params.get("from") or "").lower()
           for p in sig.produces):
        return True
    return False


def _latest_corpus_import(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT r.import_id FROM import_runs r "
        "WHERE EXISTS (SELECT 1 FROM cards c WHERE c.import_id = r.import_id) "
        "ORDER BY r.rowid DESC LIMIT 1"
    ).fetchone()
    return row["import_id"] if row is not None else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="demo-algebra", description=__doc__)
    parser.add_argument("--db", default="research.db")
    parser.add_argument("--max-len", type=int, default=2,
                        help="maximum cards per cycle (2 keeps the full-corpus demo fast)")
    parser.add_argument("--max-retrigger", type=int, default=256)
    parser.add_argument("--max-enables", type=int, default=8)
    args = parser.parse_args(argv)

    started = time.monotonic()
    db_path = Path(args.db)
    if not db_path.is_file():
        print(f"database not found: {db_path}")
        return 1

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        import_id = _latest_corpus_import(conn)
        if import_id is None:
            print("no corpus import found")
            return 1
        contexts, effects = load_contexts(conn, import_id)
        t_sig = time.monotonic()
        sigs = build_signatures(contexts, effects)
        t_sig = time.monotonic() - t_sig

        names = {cid: ctx.name for cid, ctx in contexts.items()}
        if DEFAULT_VINTAGE_FORMAT.is_file():
            legality = VintageLegality.from_forge_format(DEFAULT_VINTAGE_FORMAT)
        else:
            legality = VintageLegality.permissive()
        vintage = set(vintage_pool([c.card_id for c in contexts.values()], names, legality))
        pool = sorted({s.card_id for s in sigs if s.card_id in vintage and _loop_machinery(s)})

        weights = load_motif_weights(conn)
        options = LinkOptions(
            scope="retrigger",
            max_retrigger=args.max_retrigger,
            max_enables=args.max_enables,
            closure_depth=2 if args.max_len <= 2 else 3,
        )
        t_graph = time.monotonic()
        graph = build_graph(sigs, pool=pool, options=options)
        t_graph = time.monotonic() - t_graph
        t_search = time.monotonic()
        combos = find_combos(
            sigs, weights=weights, max_len=args.max_len, pool=pool,
            options=options, graph=graph, max_cycles=5_000_000,
        )
        t_search = time.monotonic() - t_search
        total = time.monotonic() - started
    finally:
        conn.close()

    # -- report ------------------------------------------------------------
    lines = [
        "Interaction-algebra demo (READ-ONLY)",
        f"  database            : {db_path}",
        f"  corpus import       : {import_id}",
        f"  signatures          : {len(sigs)}  ({t_sig:.1f}s)",
        f"  Vintage pool        : {len(vintage)} cards",
        f"  loop-machinery scope: {len(pool)} cards",
        f"  motif weights       : {len(weights)} (latest enrichment run)",
        "",
        "  pruning:",
        f"    scope=retrigger  closure_depth={options.closure_depth}  "
        f"max_len={args.max_len}",
        f"    max_retrigger={args.max_retrigger}  max_enables={args.max_enables}",
        f"    graph: nodes={len(graph.by_card)} links={len(graph.links)} "
        f"re_triggers={len(graph.re_triggers)}  ({t_graph:.1f}s)",
        "",
        f"(a) cycles found: {len(combos)}  ({t_search:.1f}s search)",
    ]
    by_pattern = Counter(p for combo in combos for p in combo.patterns)
    for pattern, count in by_pattern.most_common():
        lines.append(f"      {pattern:<18} {count}")
    lines.append("    top cycles:")
    for combo in combos[:8]:
        pre = f"  pre={list(combo.preconditions)}" if combo.preconditions else ""
        lines.append(f"      {combo.score:.3f} [{','.join(combo.patterns)}] "
                     f"{' + '.join(combo.cards)}{pre}")

    lines.append("")
    lines.append("(b) Kiki-Jiki's known partners as Kiki cycles:")
    for partner in KIKI_PARTNERS:
        matches = [c for c in combos if set(c.cards) == {KIKI, partner}]
        if matches:
            combo = matches[0]
            pre = f" pre={list(combo.preconditions)}" if combo.preconditions else ""
            lines.append(f"      FOUND  {partner:<22} {combo.patterns}{pre}")
        else:
            lines.append(f"      absent {partner}")

    lines.append("")
    lines.append("(c) Bumi / land-untappers:")
    land_with_kiki = [
        c.cards for c in combos
        if KIKI in c.cards and any(name in c.cards for name in LAND_UNTAPPERS)
    ]
    lines.append(f"      absent as Kiki partners: {not land_with_kiki}")
    lines.append(f"      Bumi cycles (any partner): "
                 f"{[c.cards for c in combos if 'Bumi, Unleashed' in c.cards][:5]}")
    lines.append(f"      land-untapper cycles elsewhere (legitimate, non-Kiki): "
                 f"{[c.cards for c in combos if any(n in c.cards for n in LAND_UNTAPPERS)][:5]}")

    lines.append("")
    lines.append(f"(d) total runtime: {total:.1f}s")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
