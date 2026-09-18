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
import gzip
import importlib.util
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

from combo_discovery import witness as W
from combo_discovery.corpus.names import normalize_card_name, pair_hash
from combo_discovery.env import ForgeEnvClient
from combo_discovery.ontology.builder import _load_vintage
from combo_discovery.research_config import load_config
from combo_discovery.store import ExperimentStore

DEFAULT_DECKS = ["A=decks/goldfish_A.dck", "B=decks/goldfish_B.dck"]
DEFAULT_SCRYFALL = (
    Path.home() / ".cache" / "combo-discovery" / "scryfall" / "oracle_cards.jsonl.gz"
)


def _load_analogue_module():
    """Import the sibling ``analogue_transfer`` script.

    The ETB-untapper pattern and the known engine list live there; this script
    reuses them rather than duplicating the regexes.  Loading by path (instead of
    ``import analogue_transfer``) keeps working whether the file is executed
    directly or loaded by the tests under a synthetic module name.
    """
    path = Path(__file__).resolve().parent / "analogue_transfer.py"
    spec = importlib.util.spec_from_file_location("_combo_analogue_transfer", path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_ANALOGUE = _load_analogue_module()

#: The functional ETB-untap shape (reused from analogue_transfer).
UNTAP_PAT = _ANALOGUE.UNTAP_PAT

#: Established copy engines from analogue_transfer (Kiki-Jiki, Splinter Twin).
KNOWN_ENGINES = tuple(
    part.strip() for part in _ANALOGUE.DEFAULT_ENGINES.split(";") if part.strip()
)

#: An activated ability (a ``{...}`` cost followed by ``:`` on the same line)
#: whose effect creates a token that is a copy of a creature.  This is the
#: Kiki-Jiki / Splinter Twin shape the witness policy can drive.  Triggered
#: engines (attack / ETB / end-step) carry no ``{cost}:`` prefix and are
#: therefore excluded — the policy cannot drive them.
_ACTIVATED_COPY_ENGINE = re.compile(
    r"\{[^{}\n]*\}[^:\n]{0,60}:"  # one or more {cost} symbols, then ':'
    r"[^.\n]*?create (?:a |an )?(?:token|tokens)"  # creates a token
    r"[^.\n]*?cop(?:y|ies) "  # that is a copy
    r"(?:of )?"
    r"(?:a |an |the |that |this |another |target |nonlegendary )*"
    r"creature(?! card)",  # of a creature (not a creature *card*)
    re.IGNORECASE,
)

#: A ``{T}`` that is immediately followed by ``:`` — i.e. the tap is the whole
#: cost (the ability boundary check below rules out ``{2}{W}, {T}:`` shapes).
_TAP_ACTIVATED = re.compile(r"\{t\}\s*:", re.IGNORECASE)

#: Ability boundaries used to tell whether ``{T}`` is the first cost component.
_ABILITY_BOUNDARY = re.compile(r'[."\u2014\u2022]')

#: An activated ability this class excludes: per-turn / activation limits cannot
#: loop.  Applied to the ability text (cost + effect).
_TAP_LIMIT = re.compile(r"activate only|once each turn|only once", re.IGNORECASE)

#: A ``{T}:`` ability that creates a token copy (the copy class already covers
#: these, whatever the copied permanent's type).
_TAP_COPY_EFFECT = re.compile(
    r"create [^.\n]*?tokens?[^.\n]*?cop(?:y|ies)", re.IGNORECASE
)

#: A pure mana effect: starts with "add" and has no other action.
_MANA_EFFECT = re.compile(r"^\s*add\b", re.IGNORECASE)
_NON_MANA_VERB = re.compile(
    r"\b(create|draw|deal|destroy|exile|return|put|counter|sacrifice|discard|"
    r"mill|look|search|tap|untap|copy|gain|lose|damage|proliferate|scry|"
    r"surveil|transform|fight|regenerate|shuffle|investigate|attach|double|"
    r"remove|choose|reveal)\b",
    re.IGNORECASE,
)

#: Untappers of any shape: the analogue-transfer ETB pattern plus the other
#: untap wordings (activated untappers, attack-triggered untappers, ...).
UNTAP_ANY_PAT = re.compile(UNTAP_PAT.pattern + r"|untap each", re.IGNORECASE)


@dataclass(frozen=True)
class Card:
    """The card columns the selection logic needs."""

    normalized_name: str
    name: str
    type_line: str
    oracle_text: str  # lower-cased


def is_activated_copy_engine(oracle_text: str) -> bool:
    """True for an activated ability that copies a creature into a token."""
    return bool(oracle_text) and bool(_ACTIVATED_COPY_ENGINE.search(oracle_text))


def is_etb_untapper(type_line: str, oracle_text: str) -> bool:
    """True for the functional ETB-untapper partner shape.

    Mirrors ``analogue_transfer``'s partner query: a creature whose oracle text
    references an ``enters``/``enter the battlefield`` event and untaps a
    targeted permanent (or the gain-control-then-untap-it shape).
    """
    ot = oracle_text or ""
    if "untap" not in ot:
        return False
    if "creature" not in (type_line or "").lower():
        return False
    if "enters" not in ot and "enter the battlefield" not in ot:
        return False
    return bool(UNTAP_PAT.search(ot))


def _tap_only_effects(oracle_text: str) -> list[str]:
    """Effects of activated abilities whose *only* cost is ``{T}``.

    For each ``{T}:`` on a line, the text between the previous ability boundary
    (start of line, period, quote, em-dash, bullet) and the ``{T}`` must be
    empty — otherwise some other cost (mana, sacrifice, ...) precedes the tap,
    so the ability is not a free tap engine.
    """
    effects: list[str] = []
    for line in (oracle_text or "").split("\n"):
        low = line.lower()
        for match in _TAP_ACTIVATED.finditer(low):
            prefix = low[: match.start()]
            cut = 0
            for boundary in _ABILITY_BOUNDARY.finditer(prefix):
                cut = boundary.end()
            if prefix[cut:].strip():
                continue  # another cost (e.g. {2}{W}, ) sits before the tap
            effects.append(line[match.end():].strip())
    return effects


def is_pure_mana_ability(effect: str) -> bool:
    """True for a mana ability with no other effect (ramp, not a loop engine)."""
    return bool(_MANA_EFFECT.match(effect or "")) and not _NON_MANA_VERB.search(effect)


def is_tap_engine(oracle_text: str) -> bool:
    """True for a free ``{T}:`` activated engine that can loop with an untapper.

    Qualifies when the card has a ``{T}``-only-cost ability that is not pure
    mana, not a token-copy ability (the copy class covers those) and has no
    per-turn/activation limit.  Triggered abilities never match (no ``{T}:``).
    """
    for effect in _tap_only_effects(oracle_text):
        if not effect:
            continue
        if is_pure_mana_ability(effect):
            continue
        if _TAP_COPY_EFFECT.search(effect):
            continue
        if _TAP_LIMIT.search(effect):
            continue
        return True
    return False


def is_untapper(type_line: str, oracle_text: str) -> bool:
    """True for an untapper of any shape (activated / ETB / attack trigger).

    The analogue-transfer ETB-untapper pattern plus any card matching
    ``untap target|untap it|untap that|untap each``.
    """
    ot = oracle_text or ""
    return is_etb_untapper(type_line, ot) or bool(UNTAP_ANY_PAT.search(ot))


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
    predicate = (
        is_activated_copy_engine if engine_class == "copy" else is_tap_engine
    )
    out: list[Card] = []
    for card in cards:
        if not is_legal(card.name):
            continue
        if not predicate(card.oracle_text):
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

    Partner-outer iteration gives every engine coverage before the cap fills
    with one engine's partners.  ``max_pairs=None`` returns the whole product.
    """
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for partner in partners:
        pn = normalize_card_name(partner)
        for engine in engines:
            en = normalize_card_name(engine)
            if not en or not pn or en == pn:
                continue
            key = (en, pn)
            if key in seen:
                continue
            if pair_hash(en, pn) in known_hashes:
                continue
            seen.add(key)
            pairs.append((engine, partner))
            if max_pairs is not None and len(pairs) >= max_pairs:
                return pairs
    return pairs


def load_release_map(path: Path, corpus_names: set[str]) -> dict[str, str]:
    """Map normalized name -> ISO release date from the Scryfall JSONL cache.

    Only names present in the corpus are kept (the cache is much larger than the
    cards the DB carries).  When a name appears more than once the newest date
    wins.
    """
    releases: dict[str, str] = {}
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            name = entry.get("name")
            released = entry.get("released_at")
            if not name or not released:
                continue
            key = normalize_card_name(name)
            if key not in corpus_names:
                continue
            current = releases.get(key)
            if current is None or released > current:
                releases[key] = released
    return releases


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
        known = {row[0] for row in conn.execute("select pair_hash from known_combo_pairs")}
    finally:
        conn.close()

    legality = _load_vintage({})
    releases = load_release_map(DEFAULT_SCRYFALL, {card.normalized_name for card in cards})

    engines = select_recent_engines(
        cards, releases, since=args.since, is_legal=legality.is_legal,
        engine_class=args.engine_class,
    )
    known_for_class = KNOWN_ENGINES if args.engine_class == "copy" else ()
    partners = select_partners(
        cards, is_legal=legality.is_legal, known_engines=known_for_class,
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
    store = ExperimentStore(Path(args.db)) if args.persist else None
    results: list[dict] = []
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
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
    finally:
        if store is not None:
            store.close()

    loops = [r for r in results if r["verdict"] == "loops"]
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
