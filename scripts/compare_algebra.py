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
from pathlib import Path
from typing import Iterable

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
    f"  {'query/pattern':24} {'proposed':>8} {'known':>7} {'contained':>7} "
    f"{'unmatched':>8} {'missed':>6} {'P':>7} {'P@10':>6} {'R':>7} {'F1':>7}"
)


def _row(label: str, m) -> str:
    proposed = m.true_positives + m.partials + m.false_positives
    p10 = m.precision_at_k.get(10)
    return (
        f"  {label:24} {proposed:>8} {m.true_positives:>7} {m.partials:>7} "
        f"{m.false_positives:>8} {m.missed:>6} {m.precision:>7.3f} "
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
    finally:
        store.close()

    print("LEGACY vs INTERACTION-ALGEBRA evaluation (same corpus import)")
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
    print("comparison summary")
    la, aa = legacy.aggregate, algebra.aggregate
    for label, lv, av in (
        ("proposed", la.true_positives + la.partials + la.false_positives,
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
