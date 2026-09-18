"""Lever 2: recent engines x established partner shapes.

Commander Spellbook reliably lags on brand-new sets.  This instrument enumerates
recent-set *engines* and crosses them with the established partner shapes the
witness policy already drives, drops anything Spellbook catalogues, and runs the
real witness verifier.

Two engine classes are supported (``--engine-class``):

``copy`` (default)
    An activated ability that creates a token copy of a creature — the
    Kiki-Jiki / Splinter Twin shape.  Partners are the functional ETB-untappers
    from :mod:`analogue_transfer` plus the known engines.
``tap``
    A free ``{T}:`` activated engine (the tap is the whole cost), closed by any
    card that untaps a permanent — the Kiki/Pestermite shape generalised.
    Partners are untappers of any shape.

Survivors are candidate uncatalogued combos; they still need the independent
second source before anyone calls them novel.

Usage:
    uv run python scripts/recent_engines.py --since 2024 --max-pairs 25 --persist
    uv run python scripts/recent_engines.py --engine-class tap --since 2024
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

from combo_discovery import batch as B
from combo_discovery import witness as W
from combo_discovery.cards import (
    UNTAP_ANY_PAT,
    is_activated_copy_engine,
    is_etb_untapper,
    is_legal,
    is_tap_engine,
    known_pair_hashes,
    load_release_map,
)
from combo_discovery.corpus.names import normalize_card_name, pair_hash
from combo_discovery.env import ForgeEnvClient
from combo_discovery.research_config import load_config
from combo_discovery.store import ExperimentStore

DEFAULT_DECKS = ["A=decks/goldfish_A.dck", "B=decks/goldfish_B.dck"]
DEFAULT_SCRYFALL = (
    Path.home() / ".cache" / "combo-discovery" / "scryfall" / "oracle_cards.jsonl.gz"
)

#: Established copy engines for the copy class (Kiki-Jiki, Splinter Twin).  The
#: tap class has none.  Kept here, not in the shared module: the known-engine
#: list is a script choice, not a card heuristic.
KNOWN_ENGINES = ("Kiki-Jiki, Mirror Breaker", "Splinter Twin")


@dataclass(frozen=True)
class Card:
    """The card columns the selection logic needs."""

    normalized_name: str
    name: str
    type_line: str
    oracle_text: str  # lower-cased


def _release_year(released_at: str | None) -> int | None:
    if not released_at or len(released_at) < 4:
        return None
    try:
        return int(released_at[:4])
    except ValueError:
        return None


def select_recent_engines(
    cards: Iterable[Card],
    releases: Mapping[str, str],
    *,
    since: int,
    is_legal: Callable[[str], bool],
    engine_class: str = "copy",
) -> list[Card]:
    """Legal engines of ``engine_class`` released in ``since`` or later.

    Newest first, then by name, so the cap in :func:`build_pairs` favours the
    freshest sets.
    """
    out: list[Card] = []
    for card in cards:
        if not is_legal(card.name):
            continue
        if engine_class == "copy":
            qualifies = is_activated_copy_engine(card.oracle_text)
        else:
            qualifies = is_tap_engine(card.oracle_text, card.type_line)
        if not qualifies:
            continue
        year = _release_year(releases.get(card.normalized_name))
        if year is None or year < since:
            continue
        out.append(card)
    out.sort(key=lambda c: (-(_release_year(releases.get(c.normalized_name)) or 0), c.name))
    return out


def select_partners(
    cards: Iterable[Card],
    *,
    is_legal: Callable[[str], bool],
    known_engines: Sequence[str] = (),
    engine_class: str = "copy",
) -> list[str]:
    """Established partner shapes for ``engine_class``.

    ``copy``: functional ETB-untappers, then the known engines (untappers first
    so the primary direction is never crowded out of the cap).
    ``tap``: untappers of any shape (activated / ETB / attack-triggered).  The
    ETB-untapper pattern is listed first so the cap favours the established
    partner shape over the broad ``untap it/that`` spells.
    """
    card_list = list(cards)
    if engine_class == "tap":
        established = sorted(
            {
                card.name
                for card in card_list
                if is_legal(card.name) and is_etb_untapper(card.type_line, card.oracle_text)
            }
        )
        established_set = set(established)
        other = sorted(
            {
                card.name
                for card in card_list
                if is_legal(card.name)
                and card.name not in established_set
                and UNTAP_ANY_PAT.search(card.oracle_text or "")
            }
        )
        untappers = established + other
    else:
        untappers = sorted(
            {
                card.name
                for card in card_list
                if is_legal(card.name) and is_etb_untapper(card.type_line, card.oracle_text)
            }
        )
    extra = sorted(engine for engine in known_engines if engine not in set(untappers))
    return untappers + extra


def build_pairs(
    engines: Sequence[str],
    partners: Sequence[str],
    known_hashes: set[str],
    *,
    max_pairs: int | None = None,
) -> list[tuple[str, str]]:
    """Uncatalogued engine x partner pairs in a deterministic, balanced order.

    Engine-major round-robin: the engines cycle in order while the partner
    advances one step each time, so a capped sweep samples *both* sides instead
    of exhausting one engine (or one partner) first.  With ``E`` engines,
    ``P`` partners and round ``d`` the pairs are ``(engine[j], partner[(j+d)%P])``
    for ``j`` in ``0..E-1``; over ``d`` in ``0..P-1`` every engine meets every
    partner exactly once.  ``pair_hash`` known pairs are skipped, duplicates are
    dropped, and ``max_pairs=None`` returns the whole (deduped) product.
    """
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    n_engines = len(engines)
    n_partners = len(partners)
    if n_engines == 0 or n_partners == 0:
        return pairs
    limit = n_engines * n_partners if max_pairs is None else max(0, max_pairs)
    round_no = 0
    while len(pairs) < limit and round_no < n_partners:
        for j in range(n_engines):
            engine = engines[j]
            partner = partners[(j + round_no) % n_partners]
            en = normalize_card_name(engine)
            pn = normalize_card_name(partner)
            if not en or not pn or en == pn:
                continue
            key = (en, pn)
            if key in seen:
                continue
            if pair_hash(en, pn) in known_hashes:
                continue
            seen.add(key)
            pairs.append((engine, partner))
            if len(pairs) >= limit:
                break
        round_no += 1
    return pairs


def _read_cards(conn: sqlite3.Connection) -> list[Card]:
    """One Card per normalized name (first printing wins)."""
    cards: dict[str, Card] = {}
    for norm, name, type_line, oracle in conn.execute(
        "select normalized_name, name, type_line, lower(oracle_text) from cards"
    ):
        key = norm or normalize_card_name(name)
        if not key or key in cards:
            continue
        cards[key] = Card(
            normalized_name=key,
            name=name,
            type_line=type_line or "",
            oracle_text=oracle or "",
        )
    return list(cards.values())


def _year_label(releases: Mapping[str, str], name: str) -> str:
    year = _release_year(releases.get(normalize_card_name(name)))
    return str(year) if year is not None else "?"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="recent-engines",
        description="Witness-verify recent engines against established partners.",
    )
    parser.add_argument("--db", default="research.db")
    parser.add_argument(
        "--engine-class", choices=("copy", "tap"), default="copy",
        help="engine shape: 'copy' token-copy engines (default) or 'tap' free "
             "{T}: engines",
    )
    parser.add_argument(
        "--since", type=int, default=2024,
        help="earliest release year for an engine (default: 2024)",
    )
    parser.add_argument(
        "--max-pairs", type=int, default=60,
        help="cap on pairs to run (default: 60)",
    )
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=50051)
    parser.add_argument("--decks", default=",".join(DEFAULT_DECKS))
    parser.add_argument("--out", default="/tmp/opencode/recent_engines.json")
    parser.add_argument(
        "--persist", action="store_true",
        help="stream observations + verdicts into the witness tables (off by default)",
    )
    parser.add_argument(
        "--workers", type=int, default=1,
        help="harness workers (1 = today's serial path; N uses ports port..port+N-1)",
    )
    parser.add_argument(
        "--spawn", action="store_true",
        help="spawn the worker harness servers before the sweep",
    )
    args = parser.parse_args(argv)
    # Engine metadata for persisted runs (research.toml falls back to defaults).
    cfg = load_config()

    decks = []
    for part in args.decks.split(","):
        name, _, path = part.partition("=")
        decks.append((name or path, os.path.abspath(path or name)))

    conn = sqlite3.connect(args.db)
    try:
        cards = _read_cards(conn)
        known = known_pair_hashes(conn)
    finally:
        conn.close()

    releases = load_release_map(DEFAULT_SCRYFALL, {card.normalized_name for card in cards})

    engines = select_recent_engines(
        cards, releases, since=args.since, is_legal=is_legal,
        engine_class=args.engine_class,
    )
    known_for_class = KNOWN_ENGINES if args.engine_class == "copy" else ()
    partners = select_partners(
        cards, is_legal=is_legal, known_engines=known_for_class,
        engine_class=args.engine_class,
    )
    all_pairs = build_pairs(
        [card.name for card in engines], partners, known, max_pairs=None
    )
    pairs = all_pairs[: max(0, args.max_pairs)]
    print(
        f"engine_class={args.engine_class} recent_engines={len(engines)} "
        f"partners={len(partners)} uncatalogued_pairs={len(all_pairs)} "
        f"running={len(pairs)} (since={args.since})"
    )
    for card in engines:
        print(f"  engine {card.name} [{_year_label(releases, card.name)}]")

    # Only touch the database when asked: the default path stays write-free.
    # ``--workers 1`` (the default) keeps the original serial code path exactly.
    store = (
        ExperimentStore(Path(args.db))
        if (args.persist and args.workers <= 1)
        else None
    )
    results: list[dict] = []
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if args.workers <= 1:
            with ForgeEnvClient(host=args.host, port=args.port) as client:
                client.connect()
                for engine, partner in pairs:
                    engine_card = next(c for c in engines if c.name == engine)
                    partner_card = next(
                        (c for c in cards if c.name == partner), None
                    )
                    combo = W.Candidate(
                        cards=(engine, partner),
                        type_lines=(
                            engine_card.type_line,
                            partner_card.type_line if partner_card else "",
                        ),
                        oracle_texts=(
                            engine_card.oracle_text,
                            partner_card.oracle_text if partner_card else "",
                        ),
                        kind="pair",
                        key=f"recent_engines:{engine}+{partner}",
                        pattern="recent_engine",
                    )
                    scenario = W.build_scenario(combo)
                    policy = W.WitnessPolicy(combo)
                    run_id = None
                    recorder = None
                    if store is not None:
                        run_id, recorder = W.start_witness_recording(
                            store, scenario=scenario, seeds=[1],
                            params={"max_iterations": 6, "max_decisions": 256},
                            engine_commit=cfg.engine_commit,
                            proto_version=cfg.proto_version,
                        )
                    persisted_res: W.WitnessResult | None = None
                    try:
                        res = W.run_witness(
                            client, scenario, policy, seeds=[1],
                            max_iterations=6, max_decisions=256, decks=decks,
                            candidate_kind=combo.kind, candidate_key=combo.key,
                            card_names=combo.cards, infinite=combo.infinite,
                            recorder=recorder,
                        )
                        persisted_res = res
                        diag = policy.diagnostics()
                        evidence = res.evidence or {}
                        record = {
                            "engine_class": args.engine_class,
                            "engine": engine,
                            "partner": partner,
                            "engine_year": _year_label(releases, engine),
                            "partner_year": _year_label(releases, partner),
                            "verdict": res.verdict,
                            "executed": int(diag.get("executed_actions", 0)),
                            "reason": evidence.get("kind") or evidence.get("reason"),
                        }
                    except Exception as exc:  # noqa: BLE001
                        record = {
                            "engine_class": args.engine_class,
                            "engine": engine,
                            "partner": partner,
                            "engine_year": _year_label(releases, engine),
                            "partner_year": _year_label(releases, partner),
                            "verdict": "error",
                            "executed": 0,
                            "reason": str(exc),
                        }
                        if store is not None and run_id is not None:
                            # Never leave the run row without a result.
                            W.persist_error_result(
                                store, run_id, scenario=scenario, error=exc,
                                candidate_kind=combo.kind, candidate_key=combo.key,
                                card_names=combo.cards,
                            )
                            run_id = None
                    if store is not None and run_id is not None and persisted_res is not None:
                        W.persist_witness(store, persisted_res, run_id=run_id)
                    results.append(record)
                    out_path.write_text(json.dumps(results, indent=1))
                    print(
                        f"  {record['verdict']:12s} exec={record['executed']:2d} "
                        f"{engine} [{record['engine_year']}] + {partner} "
                        f"[{record['partner_year']}]  {record['reason'] or ''}"
                    )
        else:
            # Mirror the serial Candidate exactly (same key/pattern and the
            # already-lower-cased oracle texts from this run's corpus read).
            engine_by_name = {card.name: card for card in engines}
            card_by_name = {card.name: card for card in cards}

            def _recent_candidate(engine: str, partner: str) -> W.Candidate:
                engine_card = engine_by_name.get(engine)
                partner_card = card_by_name.get(partner)
                return W.Candidate(
                    cards=(engine, partner),
                    type_lines=(
                        engine_card.type_line if engine_card else "",
                        partner_card.type_line if partner_card else "",
                    ),
                    oracle_texts=(
                        engine_card.oracle_text if engine_card else "",
                        partner_card.oracle_text if partner_card else "",
                    ),
                    kind="pair",
                    key=f"recent_engines:{engine}+{partner}",
                    pattern="recent_engine",
                )

            records = B.run_batch(
                pairs,
                workers=args.workers,
                host=args.host,
                base_port=args.port,
                spawn=args.spawn,
                db=args.db,
                persist=args.persist,
                decks=decks,
                seeds=[1],
                max_iterations=6,
                max_decisions=256,
                candidate_builder=_recent_candidate,
            )
            for (engine, partner), rec in zip(pairs, records):
                record = {
                    "engine_class": args.engine_class,
                    "engine": engine,
                    "partner": partner,
                    "engine_year": _year_label(releases, engine),
                    "partner_year": _year_label(releases, partner),
                    "verdict": rec["verdict"],
                    "executed": int(rec.get("executed", 0)),
                    "reason": rec.get("reason"),
                }
                results.append(record)
                out_path.write_text(json.dumps(results, indent=1))
                print(
                    f"  {record['verdict']:12s} exec={record['executed']:2d} "
                    f"{engine} [{record['engine_year']}] + {partner} "
                    f"[{record['partner_year']}]  {record['reason'] or ''}"
                )
    finally:
        if store is not None:
            store.close()

    loops = [r for r in results if r["verdict"] == "loops"]
    tested_engines = sorted({r["engine"] for r in results})
    tested_partners = sorted({r["partner"] for r in results})
    print(
        f"\nSAMPLE engine_class={args.engine_class} "
        f"engines={len(tested_engines)}/{len(engines)} "
        f"partners={len(tested_partners)}/{len(partners)}"
    )
    print(f"  tested engines ({len(tested_engines)}): {', '.join(tested_engines)}")
    print(f"  tested partners ({len(tested_partners)}): {', '.join(tested_partners)}")
    print(
        f"\nSUMMARY engine_class={args.engine_class} n={len(results)} "
        f"{dict(Counter(r['verdict'] for r in results))}"
    )
    print(
        f"engine class: {args.engine_class}  engines: {len(engines)}  "
        f"uncatalogued pairs: {len(all_pairs)}  run: {len(results)}"
    )
    print(f"candidate loops not in Spellbook: {len(loops)}")
    for r in loops:
        print(
            f"  LOOPS {r['engine']} [{r['engine_year']}] + {r['partner']} "
            f"[{r['partner_year']}]  ({r['reason']})"
        )
    print("wrote", out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
