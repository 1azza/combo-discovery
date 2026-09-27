#!/usr/bin/env python
"""Static cycle gate: report cycle evidence for pairings, and validate the signal.

The gate is an **additive** signal over the interaction graph (see
:mod:`combo_discovery.ontology.cycle_gate`).  It never removes or reorders the
existing candidates; it annotates a pairing (or a candidate set) with whether
the graph contains a closed, resource-closed cycle with no once-per-turn cap.

Modes
-----

* ``--pair "Kiki-Jiki, Mirror Breaker + Deceiver Exarch"`` (repeatable) or
  ``--pairs-file PATH`` (one ``A + B`` per line): print the cycle path for a
  pairing.
* ``--validate``: run the signal over persisted witness verdicts and emit the
  precision/recall table against the existing ``combo_hypotheses.score``.  This
  is the retroactive check; it needs no harness, only ``research.db``.
* ``--hypotheses``: apply the gate to a bounded slice of the generated
  candidate set (2- and 3-card hypotheses) and report how much of it carries
  proven cycle structure.

Examples
--------
    uv run python scripts/cycle_gate.py --pair "Kiki-Jiki, Mirror Breaker + Deceiver Exarch"
    uv run python scripts/cycle_gate.py --validate --json /tmp/opencode/cycle_gate.json
    uv run python scripts/cycle_gate.py --hypotheses --pattern mana_engine --limit 100
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from combo_discovery.ontology.builder import latest_import_id
from combo_discovery.ontology.cycle_gate import (
    DEFAULT_RUN_RANGES,
    CycleEvidence,
    check_card_ids,
    load_verdicts,
    resolve_card_id,
    validate,
)
from combo_discovery.ontology.extractor import load_contexts


def _open_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _parse_runs(value: str) -> tuple[tuple[int, int], ...]:
    spans = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        low, _, high = part.partition("-")
        spans.append((int(low), int(high or low)))
    return tuple(spans) or DEFAULT_RUN_RANGES


def _parse_pair(text: str) -> tuple[str, str]:
    parts = [p.strip() for p in text.split(" + ")]
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"expected 'A + B', got {text!r}")
    return parts[0], parts[1]


def _resolve_pair(contexts, pair) -> tuple[int, int] | None:
    left = resolve_card_id(contexts, pair[0])
    right = resolve_card_id(contexts, pair[1])
    if left is None or right is None:
        return None
    return left, right


def _print_evidence(evidence: CycleEvidence) -> None:
    print(evidence.format())


def _cmd_pairs(args, contexts, effects) -> int:
    pairs: list[tuple[str, str]] = []
    for text in args.pair or ():
        pairs.append(_parse_pair(text))
    if args.pairs_file:
        for line in Path(args.pairs_file).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                pairs.append(_parse_pair(line))
    if not pairs:
        print("no pairs given (use --pair or --pairs-file)", file=sys.stderr)
        return 2

    results = []
    for pair in pairs:
        card_ids = _resolve_pair(contexts, pair)
        if card_ids is None:
            print(f"{' + '.join(pair)}: UNRESOLVED (not both in corpus)")
            results.append({"cards": list(pair), "unresolved": True})
            continue
        evidence = check_card_ids(
            contexts, effects, card_ids,
            max_len=args.max_len,
            include_refresh=not args.no_refresh,
            include_triggered=args.include_triggered,
        )
        _print_evidence(evidence)
        results.append(evidence.as_dict())
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=1))
        print(f"wrote {args.json}")
    return 0


def _format_block(name: str, block: dict) -> list[str]:
    if not block:
        return []
    lines = []
    for key, label in (
        ("static_unbounded", "static unbounded"),
        ("static_closed", "static closed   "),
        ("existing_score_gt0", "existing score>0"),
    ):
        conf = block.get(key)
        if conf:
            lines.append(
                f"  {label}: n={conf['n']:>4} loops={conf['loops']:>3} "
                f"tp={conf['tp']:>3} fp={conf['fp']:>3} fn={conf['fn']:>3} "
                f"tn={conf['tn']:>3}  precision={conf['precision']:.3f} "
                f"recall={conf['recall']:.3f}"
            )
    lines.append(
        f"  separation: AUC static={block.get('static_auc')} "
        f"existing={block.get('existing_auc')}"
    )
    return lines


def _cmd_validate(args, conn, contexts, effects) -> int:
    runs = _parse_runs(args.runs)
    rows, unresolved = load_verdicts(
        conn, contexts, effects,
        run_ranges=runs,
        include_refresh=not args.no_refresh,
        include_triggered=args.include_triggered,
        max_len=args.max_len,
    )
    if args.engine_class:
        wanted = {c.strip() for c in args.engine_class.split(",") if c.strip()}
        rows = [row for row in rows if row.engine_class in wanted]
    result = validate(rows, threshold=args.threshold)
    result["run_ranges"] = [list(span) for span in runs]
    result["unresolved"] = unresolved
    result["include_refresh"] = not args.no_refresh
    result["include_triggered"] = args.include_triggered

    print(f"witness verdict rows: {len(rows)} (unresolved pairs skipped: {unresolved})")
    print(f"verdicts: {result['verdicts']}")
    print(f"inconclusive excluded from predictor evaluation: {result['inconclusive_excluded']}")
    print(f"scored rows: {result['scored']}")
    print()
    print("OVERALL")
    for line in _format_block("overall", result["overall"]):
        print(line)
    for engine, block in result["by_engine"].items():
        print(f"\nENGINE CLASS: {engine}")
        for line in _format_block(engine, block):
            print(line)

    if args.check_reproduction:
        result["reproduction"] = _check_reproduction(conn, contexts, effects, rows)
        print()
        print(f"score reproduction: {result['reproduction']}")

    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=1))
        print(f"\nwrote {args.json}")
    return 0


def _check_reproduction(conn, contexts, effects, rows) -> dict:
    """How faithfully does the legacy-pattern reproduction match persisted scores?

    ``exact_matches`` counts pairs whose reproduced legacy score equals the
    persisted maximum; ``algebra_q_only`` counts pairs whose only persisted score
    comes from the algebra ``q:`` path (which the legacy registry cannot
    reproduce, so the baseline takes the persisted value instead).
    """
    from combo_discovery.ontology.cycle_gate import load_persisted_scores

    persisted = load_persisted_scores(conn)
    compared = matched = q_only = 0
    for row in rows:
        key = tuple(sorted(row.card_ids))
        if key not in persisted:
            continue
        compared += 1
        if abs(persisted[key] - row.score) < 1e-6:
            matched += 1
        elif row.score == 0.0 and persisted[key] > 0.0:
            q_only += 1
    return {
        "pairs_in_candidate_set": compared,
        "exact_matches": matched,
        "algebra_q_only": q_only,
    }


def _cmd_hypotheses(args, conn, contexts, effects) -> int:
    pattern_clause = ""
    params: list = []
    if args.pattern:
        pattern_clause = "AND p.name = ?"
        params.append(args.pattern)
    query = (
        "SELECT h.id, h.card_ids_json, h.score, p.name AS pattern "
        "FROM combo_hypotheses h JOIN patterns p ON p.id = h.pattern_id "
        "WHERE 1=1 " + pattern_clause + " ORDER BY h.id LIMIT ?"
    )
    params.append(args.limit)
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM combo_hypotheses h JOIN patterns p ON p.id = h.pattern_id "
        "WHERE 1=1 " + pattern_clause,
        params[:-1],
    ).fetchone()["n"]

    from collections import Counter

    by_pattern: dict[str, Counter] = {}
    counts: Counter = Counter()
    examined = 0
    examples: list[dict] = []
    for record in conn.execute(query, params):
        ids = [int(i) for i in json.loads(record["card_ids_json"])]
        if len(ids) not in (2, 3):
            continue
        evidence = check_card_ids(
            contexts, effects, ids,
            max_len=max(args.max_len, len(ids)),
            include_refresh=not args.no_refresh,
            include_triggered=args.include_triggered,
        )
        examined += 1
        label = ("unbounded" if evidence.unbounded
                 else "capped" if evidence.closed else "none")
        counts[label] += 1
        by_pattern.setdefault(record["pattern"], Counter())[label] += 1
        if evidence.unbounded and len(examples) < args.examples:
            examples.append(evidence.as_dict())

    print(f"candidates in slice: {examined} of {total} matching the filter "
          f"(limit={args.limit})")
    print(f"static signal: {dict(counts)}")
    print("by pattern:")
    for pattern, tally in sorted(by_pattern.items()):
        print(f"  {pattern:<24} {dict(tally)}")
    if examples:
        print("\nexamples flagged unbounded:")
        for item in examples:
            print(f"  {' + '.join(item['cards'])}")
            for step in item["path"]:
                print(f"    {step['src']} -[{step['kind']}/{step['subkind']}]- {step['dst']}")
    if args.json:
        Path(args.json).write_text(json.dumps(
            {"examined": examined, "total": total, "counts": dict(counts),
             "by_pattern": {k: dict(v) for k, v in by_pattern.items()},
             "examples": examples},
            indent=1,
        ))
        print(f"\nwrote {args.json}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cycle-gate",
        description="Static cycle gate over the interaction graph.",
    )
    parser.add_argument("--db", default="research.db")
    parser.add_argument("--import-id", default=None,
                        help="corpus import id (default: latest with cards)")
    parser.add_argument("--pair", action="append", default=None,
                        help="'A + B' (repeatable)")
    parser.add_argument("--pairs-file", default=None,
                        help="file with one 'A + B' per line")
    parser.add_argument("--validate", action="store_true",
                        help="emit the precision/recall table over witness verdicts")
    parser.add_argument("--hypotheses", action="store_true",
                        help="apply the gate to a bounded slice of candidates")
    parser.add_argument("--runs", default="1586-2008,169-301",
                        help="witness run ranges, e.g. '1586-2008,169-301'")
    parser.add_argument("--engine-class", default=None,
                        help="filter validation rows: activated,triggered,none")
    parser.add_argument("--pattern", default=None,
                        help="filter the --hypotheses slice by pattern name")
    parser.add_argument("--limit", type=int, default=200,
                        help="max candidates to examine in --hypotheses mode")
    parser.add_argument("--examples", type=int, default=5)
    parser.add_argument("--threshold", type=float, default=0.5)
    parser.add_argument("--max-len", type=int, default=3,
                        help="max cards in a cycle")
    parser.add_argument("--no-refresh", action="store_true",
                        help="disable the additive fresh-token closure rule")
    parser.add_argument("--include-triggered", action="store_true",
                        help="include triggered copy engines (default: off)")
    parser.add_argument("--check-reproduction", action="store_true",
                        help="compare reproduced legacy scores to persisted ones")
    parser.add_argument("--json", default=None, help="write JSON output to this path")
    args = parser.parse_args(argv)

    if not (args.pair or args.pairs_file or args.validate or args.hypotheses):
        parser.error("choose one of --pair/--pairs-file, --validate, --hypotheses")

    conn = _open_db(args.db)
    try:
        import_id = args.import_id or latest_import_id(conn)
        if import_id is None:
            print("no corpus import found in database", file=sys.stderr)
            return 1
        print(f"import: {import_id}", file=sys.stderr)
        contexts, effects = load_contexts(conn, import_id)
        if args.pair or args.pairs_file:
            return _cmd_pairs(args, contexts, effects)
        if args.validate:
            return _cmd_validate(args, conn, contexts, effects)
        return _cmd_hypotheses(args, conn, contexts, effects)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
