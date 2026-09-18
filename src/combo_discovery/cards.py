"""Shared, script-independent card heuristics.

The batch instruments (``analogue_transfer``, ``recent_engines``,
``known_recall``) and the corpus/ontology layers all need the same small set of
card-text predicates.  This module is their single home: the untap patterns, the
activated-copy-engine shape, the free ``{T}:`` engine test, the cached Scryfall
release map, the Spellbook known-pair hash set, and the two small time/verdict
helpers.

Everything here is pure and import-light: the only top-level imports are the
standard library, so this module can be imported from anywhere in the package
without risking an import cycle.  Package imports (``corpus.names``,
``ontology.builder``) happen lazily inside the functions that need them.
"""

from __future__ import annotations

import gzip
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

# ---------------------------------------------------------------------------
# Untap shapes
# ---------------------------------------------------------------------------

#: The functional ETB-untap shape: untap a targeted permanent/creature, or the
#: gain-control-then-untap-it shape.
UNTAP_PAT = re.compile(r"untap target|untap it|untap that")

#: Untappers of any shape: the ETB pattern plus the other untap wordings
#: (activated untappers, attack-triggered untappers, ``untap each``, ...).
UNTAP_ANY_PAT = re.compile(UNTAP_PAT.pattern + r"|untap each", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Activated copy engines (the Kiki-Jiki / Splinter Twin shape)
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Free tap ({T}:) engines
# ---------------------------------------------------------------------------

#: A ``{T}`` that is immediately followed by ``:`` — i.e. the tap is the whole
#: cost (the ability boundary check below rules out ``{2}{W}, {T}:`` shapes).
_TAP_ACTIVATED = re.compile(r"\{t\}\s*:", re.IGNORECASE)

#: Ability boundaries used to tell whether ``{T}`` is the first cost component.
_ABILITY_BOUNDARY = re.compile(r'[."\u2014\u2022]')

#: The start of the next activated ability on a line (any ``{...}`` cost then
#: ``:``).  Used to slice an ability's *own* effect text: a mana land such as
#: ``"{T}: Add {C}. {T}: Add {R} or {W}."`` must judge each ``{T}`` ability on
#: its own text rather than letting one ability's verbs leak into another's.
_NEXT_ABILITY = re.compile(r"\{[^{}\n]*\}[^:\n]{0,60}:")

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
            rest = low[match.end():]
            # Stop at the next activated ability so each ``{T}`` ability is
            # judged on its own effect text, not the rest of the line.
            nxt = _NEXT_ABILITY.search(rest)
            effects.append((rest[: nxt.start()] if nxt else rest).strip())
    return effects


def is_pure_mana_ability(effect: str) -> bool:
    """True for a mana ability with no other effect (ramp, not a loop engine)."""
    return bool(_MANA_EFFECT.match(effect or "")) and not _NON_MANA_VERB.search(effect)


def _is_mana_ability_text(effect: str) -> bool:
    """True when an ability's text adds mana (a mana ability, whatever else it says)."""
    return bool(_MANA_EFFECT.match(effect or ""))


def is_tap_engine(oracle_text: str, type_line: str = "") -> bool:
    """True for a free ``{T}:`` activated engine that can loop with an untapper.

    Qualifies when the card has a ``{T}``-only-cost ability whose *own* text
    carries a non-mana effect verb (``_NON_MANA_VERB``), is not a token-copy
    ability (the copy class covers those) and has no per-turn/activation limit.
    Triggered abilities never match (no ``{T}:``).

    Belt and braces: a card whose ``{T}`` abilities are *all* mana abilities is
    ramp, not an engine — this covers mana lands and pure-mana artifacts whose
    mana clause carries a rider such as ``deals 1 damage to you`` (the Talismans),
    which would otherwise trip the non-mana verb heuristic.
    """
    effects = _tap_only_effects(oracle_text)
    if effects and all(_is_mana_ability_text(effect) for effect in effects):
        return False
    for effect in effects:
        if not effect:
            continue
        if _TAP_LIMIT.search(effect):
            continue
        if not _NON_MANA_VERB.search(effect):
            continue
        if _TAP_COPY_EFFECT.search(effect):
            continue
        return True
    return False


def is_untapper(type_line: str, oracle_text: str) -> bool:
    """True for an untapper of any shape (activated / ETB / attack trigger).

    The ETB-untapper pattern plus any card matching
    ``untap target|untap it|untap that|untap each``.
    """
    ot = oracle_text or ""
    return is_etb_untapper(type_line, ot) or bool(UNTAP_ANY_PAT.search(ot))


# ---------------------------------------------------------------------------
# Legality
# ---------------------------------------------------------------------------

#: Cached vintage legality checker (built once from the Forge format).
_LEGALITY = None


def _load_legality():
    """Build (and memoise) the configured vintage legality checker."""
    global _LEGALITY
    if _LEGALITY is None:
        from .ontology.builder import _load_vintage

        _LEGALITY = _load_vintage({})
    return _LEGALITY


def is_legal(name: str) -> bool:
    """True when ``name`` is legal in the configured (vintage) format."""
    return _load_legality().is_legal(name)


# ---------------------------------------------------------------------------
# Corpus helpers
# ---------------------------------------------------------------------------


def load_release_map(path: Path, corpus_names: set[str]) -> dict[str, str]:
    """Map normalized name -> ISO release date from the Scryfall JSONL cache.

    Only names present in the corpus are kept (the cache is much larger than the
    cards the DB carries).  When a name appears more than once the newest date
    wins.
    """
    from .corpus.names import normalize_card_name

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


def known_pair_hashes(conn: sqlite3.Connection) -> set[str]:
    """The normalized ``pair_hash`` values Commander Spellbook already catalogues."""
    return {row[0] for row in conn.execute("select pair_hash from known_combo_pairs")}


# ---------------------------------------------------------------------------
# Small shared helpers (previously duplicated per module)
# ---------------------------------------------------------------------------


def utc_now() -> str:
    """The current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


#: Verdict strength ordering: lower is stronger.
_VERDICT_PREFERENCE = ("loops", "inconclusive", "no_loop", "refuted", "error")


def verdict_rank(verdict: str) -> int:
    """Lower is stronger; unknown verdicts rank weakest."""
    try:
        return _VERDICT_PREFERENCE.index(str(verdict))
    except ValueError:
        return len(_VERDICT_PREFERENCE)


__all__ = [
    "UNTAP_ANY_PAT",
    "UNTAP_PAT",
    "is_activated_copy_engine",
    "is_etb_untapper",
    "is_legal",
    "is_pure_mana_ability",
    "is_tap_engine",
    "is_untapper",
    "known_pair_hashes",
    "load_release_map",
    "utc_now",
    "verdict_rank",
]
