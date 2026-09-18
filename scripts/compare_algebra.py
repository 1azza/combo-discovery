#!/usr/bin/env python
"""Legacy vs interaction-algebra evaluation comparison (read-only).

Both proposal paths live under the same corpus import (the legacy registry and
the algebra ``q:*`` vocabulary), so they are scored by restricting the
evaluation harness to each vocabulary with :func:`classify_pairs(patterns=...)`.

Run with::

    uv run python scripts/compare_algebra.py [--db research.db] [--top 15]
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable
from pathlib import Path

from combo_discovery.corpus.names import normalize_card_name
from combo_discovery.evaluation import PairVerdict, classify_pairs, metrics
from combo_discovery.ontology.builder import QUERY_PREFIX
from combo_discovery.ontology.patterns import PATTERNS
from combo_discovery.ontology.queries import QUERIES
from combo_discovery.store import ExperimentStore

KIKI = "Kiki-Jiki, Mirror Breaker"
LEGACY_NAMES = tuple(pattern.name for pattern in PATTERNS)
ALGEBRA_NAMES = tuple(f"{QUERY_PREFIX}{query.name}" for query in QUERIES)
KIKI_CHECK = (
    "Deceiver Exarch", "Pestermite", "Corridor Monitor", "Zealous Conscripts",
    "Fear of Missing Out", "Eager Beaver", "White Plume Adventurer",
)

HEADER = (
    f"  {'query/pattern':24} {'rows':>7} {'pairs':>7} {'known':>7} {'contained':>7} "
    f"{'unmatched':>8} {'missed':>6} {'P':>7} {'P@10':>6} {'R':>7} {'F1':>7}"
)


def _row(label: str, m) -> str:
    # ``pairs`` (unique) is the primary precision denominator; ``rows`` is the
    # raw interaction surface, which double-counts a pair proposed under several
    # queries.
    proposed = m.true_positives + m.partials + m.false_positives
    p10 = m.precision_at_k.get(10)
    return (
        f"  {label:24} {m.raw_proposals:>7} {proposed:>7} {m.true_positives:>7} "
        f"{m.partials:>7} {m.false_positives:>8} {m.missed:>6} {m.precision:>7.3f} "
        f"{(p10 if p10 is not None else 0.0):>6.3f} {m.recall:>7.3f} {m.f1:>7.3f}"
    )


def _print_table(title: str, report) -> None:
    print(title)
    print(HEADER)
    print(_row("AGGREGATE", report.aggregate))
    for name in sorted(report.by_pattern):
        print(_row(name, report.by_pattern[name]))
    print()


def _kiki_rows(verdicts: Iterable[PairVerdict], partner: str) -> list[PairVerdict]:
    normalized = normalize_card_name(partner)
    return [
        v for v in verdicts
        if v.verdict != "missed"
        and normalized in (v.source_name, v.target_name)
    ]


def _nonvintage_proposals(store: ExperimentStore) -> list[tuple[str, str, str]]:
    """Algebra interactions whose *raw* card name is Alchemy / Un-set.

    Verdicts carry normalized names (which drop the ``A-`` prefix), so the guard
    has to read the raw ``cards.name`` column.
    """
    rows = store._conn.execute(
        "SELECT DISTINCT s.name AS source_name, t.name AS target_name, "
        "p.name AS pattern FROM interactions i "
        "JOIN patterns p ON p.id = i.pattern_id "
        "JOIN cards s ON s.id = i.source_card_id "
        "JOIN cards t ON t.id = i.target_card_id "
        "WHERE p.name LIKE 'q:%' AND ("
        "s.name LIKE 'A-%' OR t.name LIKE 'A-%' "
        "OR s.name LIKE '%Name Sticker%' OR t.name LIKE '%Name Sticker%') "
        "ORDER BY s.name, t.name LIMIT 20"
    ).fetchall()
    return [(str(r["source_name"]), str(r["target_name"]), str(r["pattern"])) for r in rows]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="compare-algebra", description=__doc__)
    parser.add_argument("--db", default="research.db")
    parser.add_argument("--top", type=int, default=15)
    args = parser.parse_args(argv)

    db = Path(args.db)
    if not db.is_file():
        print(f"database not found: {db}")
        return 1

    store = ExperimentStore(db)
    try:
        legacy_verdicts = classify_pairs(store, patterns=LEGACY_NAMES)
        algebra_verdicts = classify_pairs(store, patterns=ALGEBRA_NAMES)
        kiki_legacy = classify_pairs(store, card=KIKI, patterns=LEGACY_NAMES)
        kiki_algebra = classify_pairs(store, card=KIKI, patterns=ALGEBRA_NAMES)
        legacy = metrics(legacy_verdicts)
        algebra = metrics(algebra_verdicts)
        nonvintage = _nonvintage_proposals(store)
    finally:
        store.close()

    print("LEGACY vs INTERACTION-ALGEBRA evaluation (same corpus import)")
    print("(precision / P@10 / recall are computed on UNIQUE pairs; 'rows' is the")
    print(" raw interaction surface, which double-counts a pair across queries)")
    print()
    _print_table("legacy: aggregate + per pattern", legacy)

    _print_table("algebra: aggregate + per query", algebra)

    print("Kiki-Jiki detail (verdict per known/algebra partner)")
    print(f"  {'partner':28} {'legacy':>12} {'algebra':>12}")
    for partner in KIKI_CHECK:
        lg = _kiki_rows(kiki_legacy, partner)
        al = _kiki_rows(kiki_algebra, partner)
        print(f"  {partner:28} {(lg[0].verdict if lg else 'absent'):>12} "
              f"{(al[0].verdict if al else 'absent'):>12}")
    missed_kiki = [v for v in kiki_legacy if v.verdict == "missed"]
    print(f"  Kiki known exact variants missed by both: {len(missed_kiki)}")

    print()
    print(f"Kiki top algebra proposals (top {args.top}):")
    ranked = sorted((v for v in kiki_algebra if v.verdict != "missed"),
                    key=lambda v: (-v.score, v.source_name, v.target_name))
    for v in ranked[: args.top]:
        print(f"  {v.score:.3f} {v.verdict:18} {v.pattern:22} "
              f"{v.source_name} + {v.target_name}")

    print()
    print(f"top unmatched algebra candidates per query (top {args.top}):")
    for query in ALGEBRA_NAMES:
        unmatched = sorted(
            (v for v in algebra_verdicts
             if v.pattern == query and v.verdict == "unmatched"),
            key=lambda v: (-v.score, v.source_name, v.target_name),
        )
        print(f"  [{query}] {len(unmatched)} unmatched")
        for v in unmatched[: args.top]:
            print(f"      {v.score:.3f} {v.source_name} + {v.target_name}")

    print()
    print("top unmatched algebra candidates overall (all queries):")
    top_unmatched = sorted(
        (v for v in algebra_verdicts if v.verdict == "unmatched"),
        key=lambda v: (-v.score, v.source_name, v.target_name),
    )
    for v in top_unmatched[: args.top]:
        print(f"      {v.score:.3f} {v.pattern:22} {v.source_name} + {v.target_name}")

    print()
    print("Alchemy / Un-set leak check (raw names):")
    if nonvintage:
        print(f"  FAIL: {len(nonvintage)} non-Vintage printing(s) still proposed")
        for source_name, target_name, pattern in nonvintage:
            print(f"      [{pattern}] {source_name} + {target_name}")
    else:
        print("  PASS: no A-* / Name Sticker card appears in the algebra proposals")

    print()
    print("comparison summary")
    la, aa = legacy.aggregate, algebra.aggregate
    for label, lv, av in (
        ("raw rows", la.raw_proposals, aa.raw_proposals),
        ("unique pairs", la.true_positives + la.partials + la.false_positives,
         aa.true_positives + aa.partials + aa.false_positives),
        ("known_pair", la.true_positives, aa.true_positives),
        ("contained_in_known", la.partials, aa.partials),
        ("unmatched", la.false_positives, aa.false_positives),
        ("missed", la.missed, aa.missed),
        ("precision", la.precision, aa.precision),
        ("precision@10", la.precision_at_k.get(10, 0.0), aa.precision_at_k.get(10, 0.0)),
        ("recall", la.recall, aa.recall),
        ("f1", la.f1, aa.f1),
    ):
        if isinstance(lv, int):
            print(f"  {label:18} legacy={lv:>8}  algebra={av:>8}")
        else:
            print(f"  {label:18} legacy={lv:>8.4f}  algebra={av:>8.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
