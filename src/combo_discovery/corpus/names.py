"""Card-name normalization and Commander Spellbook -> Forge matching.

Spellbook stores multi-face cards as ``"Front // Back"``; Forge's ``cards.name``
is the *front face only* and stores back faces in ``card_faces.name``.  Matching
therefore cannot be naive string splitting:

* ``Birgi, God of Storytelling // Harnfel, Horn of Bounty`` -> front face
  ``birgi god of storytelling``;
* ``Fable of the Mirror-Breaker // Reflection of Kiki-Jiki`` with ``usedFace=2``
  is still resolved through the front face in this round (oracle-id join is the
  precise path);
* ``SP//dr, Piloted by Peni`` is a *single-faced* card whose name contains a
  literal ``//`` and must not be split (detected via the ``faces`` metadata);
* ``A-*`` Alchemy rebalances are excluded.

Normalization is byte-compatible with :func:`combo_discovery.corpus.importer.normalize_name`
(asserted in the tests) so Spellbook pair hashes line up with the corpus.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable

_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")
DFC_SEP = " // "

#: Un-set / sticker reprint markers.  Sticker cards are never Vintage-legal, but
#: their names carry no ``A-`` prefix, so they need an explicit marker.  The
#: match is deliberately conservative (the exact sticker placeholder, not the
#: substring "sticker", which legitimate cards such as Nimble Birdsticker use).
_UNSET_NAME_RE = re.compile(r'^\s*"?name sticker"?', re.IGNORECASE)


def normalize_card_name(name: str | None) -> str:
    """NFKD-fold, lowercase, punctuation -> space, collapse whitespace."""
    if not name:
        return ""
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    cleaned = _NON_ALNUM.sub(" ", ascii_only.replace("//", " ").lower())
    return " ".join(cleaned.split())


def is_alchemy(name: str | None) -> bool:
    """True for Alchemy rebalances (``A-...``), which we exclude."""
    return bool(name) and name.startswith("A-")


def is_unset(name: str | None) -> bool:
    """True for Un-set / sticker placeholder names (never Vintage-legal)."""
    return bool(name) and bool(_UNSET_NAME_RE.match(name))


def is_non_vintage_printing(name: str | None) -> bool:
    """True for prints that exist only outside Vintage (Alchemy / Un-set).

    This is the name-level legality guard shared by the Spellbook importer and
    the ontology pool so an ``A-*`` rebalance can never leak into a proposal.
    """
    return is_alchemy(name) or is_unset(name)


def is_only_unset_printing(
    sets: Iterable[str], unset_set_codes: Iterable[str]
) -> bool:
    """True when *every* known printing of a card is an Un-set/novelty set.

    Set codes are the robust signal: Un-set cards such as ``Eager Beaver``
    (Unstable) or ``Blacker Lotus`` (Unglued) carry no name marker at all, so
    the name-level :func:`is_unset` cannot see them.  A card with at least one
    normal printing (``sets`` not a subset of ``unset_set_codes``) is rescued,
    which is why this takes the *whole* known printing set rather than a single
    code.  Case/whitespace tolerant.
    """
    known = {str(code).strip().upper() for code in sets if str(code).strip()}
    excluded = {
        str(code).strip().upper() for code in unset_set_codes if str(code).strip()
    }
    return bool(known) and known <= excluded


def split_dfc(name: str | None) -> list[str]:
    """Split a multi-face name on the canonical `` // `` separator."""
    if not name:
        return []
    if DFC_SEP in name:
        return [part.strip() for part in name.split(DFC_SEP) if part.strip()]
    return [name.strip()]


def front_face_name(name: str | None) -> str:
    parts = split_dfc(name)
    return parts[0] if parts else ""


def back_face_name(name: str | None) -> str | None:
    parts = split_dfc(name)
    return parts[1] if len(parts) > 1 else None


def pair_hash(name_a: str, name_b: str) -> str:
    """Canonical, order-independent hash of a normalized card-name pair."""
    key = "|".join(sorted((name_a, name_b)))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def combo_hash(names: Iterable[str]) -> str:
    """Canonical hash of a whole combo's normalized card set."""
    return hashlib.sha256("\x1f".join(sorted(names)).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ResolvedCard:
    """Outcome of resolving one Spellbook ``use`` against the local corpus."""

    raw_name: str
    normalized_name: str
    via: str  # oracle | name | unmatched | excluded
    oracle_id: str | None = None
    spellbook_card_id: int | None = None


def resolve_spellbook_use(
    card: dict[str, Any],
    *,
    used_face: int | None = None,
    oracle_names: dict[str, str] | None = None,
    local_names: set[str] | None = None,
) -> ResolvedCard:
    """Resolve a Spellbook ``use`` card to a normalized Forge-matching name.

    Priority: ``oracleId`` join against ``oracle_names`` (oracle id -> local
    normalized name), then a front-face name match.  ``via`` reports how the
    card resolved (or ``excluded`` for Alchemy, ``unmatched`` if the name is not
    in ``local_names``).  ``used_face`` is accepted for callers that later add
    back-face resolution; this round deliberately uses the front face.
    """
    raw = str(card.get("name") or "")
    oracle_id = card.get("oracleId") or card.get("oracle_id") or None
    spellbook_card_id = card.get("id")
    try:
        spellbook_card_id = int(spellbook_card_id) if spellbook_card_id is not None else None
    except (TypeError, ValueError):
        spellbook_card_id = None

    if is_alchemy(raw):
        return ResolvedCard(raw, "", "excluded", oracle_id, spellbook_card_id)

    if oracle_id and oracle_names and oracle_id in oracle_names:
        return ResolvedCard(
            raw, oracle_names[oracle_id], "oracle", oracle_id, spellbook_card_id
        )

    faces = card.get("faces")
    try:
        faces = int(faces) if faces is not None else 1
    except (TypeError, ValueError):
        faces = 1
    # Multi-face: use the front face (never blind string splitting on a card
    # whose metadata says it is single-faced, e.g. "SP//dr, Piloted by Peni").
    name = front_face_name(raw) if faces > 1 else raw
    normalized = normalize_card_name(name)
    if local_names is not None and normalized and normalized not in local_names:
        return ResolvedCard(raw, normalized, "unmatched", oracle_id, spellbook_card_id)
    return ResolvedCard(raw, normalized, "name", oracle_id, spellbook_card_id)


__all__ = [
    "DFC_SEP",
    "ResolvedCard",
    "back_face_name",
    "combo_hash",
    "front_face_name",
    "is_alchemy",
    "is_non_vintage_printing",
    "is_only_unset_printing",
    "is_unset",
    "normalize_card_name",
    "pair_hash",
    "resolve_spellbook_use",
    "split_dfc",
]
