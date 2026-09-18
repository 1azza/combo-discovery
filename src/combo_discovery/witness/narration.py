"""Card-level narration of a witness run, as it happens.

Maps engine ``GameEvent`` streams (and, where no event exists, policy decisions)
to Magic-readable sentences a live UI can render.  Everything here is pure and
deterministic: the driver owns emission order and the store owns ``seq``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

#: The fixed narration vocabulary (mirrors the ``witness_events.kind`` contract).
NARRATION_KINDS = frozenset(
    {
        "play_land",
        "cast",
        "activate",
        "trigger",
        "copy",
        "untap",
        "token",
        "iteration",
        "verdict",
        "other",
    }
)

#: "copy of <name>" followed by "(id)", a comma, a full stop or end of text.
_COPY_RE = re.compile(r"copy of (?P<name>.+?)(?:\s*\(\d+\)|,|\.|$)", re.IGNORECASE)
#: "targeting <name>" / "targeting [<name> (id)]" / "(Targeting: <name> (id))".
#: Card names may contain commas, so the match ends only at an engine id, a
#: closing bracket/paren, a field separator or the end of the text.
_TARGET_RE = re.compile(
    r"targeting[:\s]*\[?(?P<name>.+?)(?:\s*\(\d+\)|;|\]|\)|$)", re.IGNORECASE
)


@dataclass
class Narration:
    """One narration row (``seq`` is assigned by the store on append)."""

    kind: str
    text: str
    turn: int | None = None
    phase: str = ""
    actor: str = ""
    target: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _target_from(text: str) -> str:
    match = _TARGET_RE.search(text or "")
    return _clean(match.group("name")) if match else ""


def _copy_target(text: str) -> str:
    match = _COPY_RE.search(text or "")
    return _clean(match.group("name")) if match else ""


def _with_target(action: str, target: str) -> str:
    return f"{action}, targeting {target}" if target else action


def narrate_game_event(event: Any) -> Narration | None:
    """Map one drained game event to a narration row (``None`` = not narrated).

    Events that carry no card-level story (draws, phases, mana, shuffles, the
    scenario injection itself) are deliberately silent; the driver still uses
    them for counters and boundary detection.
    """
    etype = _clean(getattr(event, "type", ""))
    actor = _clean(getattr(event, "card_name", ""))
    detail = _clean(getattr(event, "detail_raw", ""))
    extra = _clean(getattr(event, "extra", ""))
    turn = int(getattr(event, "turn", 0) or 0)
    phase = _clean(getattr(event, "phase", ""))
    combined = f"{detail} {extra}"
    low_detail = detail.lower()

    if etype == "LandPlayed":
        text = (
            f"{actor} enters the battlefield"
            if actor
            else "A land enters the battlefield"
        )
        return Narration(
            kind="play_land", text=text, turn=turn, phase=phase, actor=actor,
            detail={"to": "battlefield"},
        )

    if etype == "SpellCast":
        if " triggered " in low_detail:
            target = _target_from(combined)
            return Narration(
                kind="trigger",
                text=_with_target(f"{actor} triggers", target),
                turn=turn, phase=phase, actor=actor, target=target,
                detail={"raw": detail},
            )
        if " activated " in low_detail:
            target = _target_from(combined)
            return Narration(
                kind="activate",
                text=_with_target(f"{actor} activates", target),
                turn=turn, phase=phase, actor=actor, target=target,
                detail={"raw": detail},
            )
        return Narration(
            kind="cast", text=f"{actor} is cast", turn=turn, phase=phase,
            actor=actor, detail={"raw": detail},
        )

    if etype == "SpellResolved":
        target = _copy_target(combined)
        if target:
            return Narration(
                kind="copy", text=f"{actor} copies {target}", turn=turn,
                phase=phase, actor=actor, target=target, detail={"raw": detail},
            )
        return None

    if etype == "PermanentEntered":
        low_extra = extra.lower()
        is_token = "from=null" in low_extra or "from=none" in low_extra
        text = (
            f"{actor} enters the battlefield"
            if actor
            else "A permanent enters the battlefield"
        )
        return Narration(
            kind="token" if is_token else "other", text=text, turn=turn,
            phase=phase, actor=actor, detail={"from": extra},
        )

    if etype == "PermanentLeftBattlefield":
        text = (
            f"{actor} leaves the battlefield"
            if actor
            else "A permanent leaves the battlefield"
        )
        return Narration(
            kind="other", text=text, turn=turn, phase=phase, actor=actor,
            detail={"raw": detail},
        )

    if etype == "CardTapped":
        untapped = "tapped=false" in extra.lower() or "untapped=true" in extra.lower()
        if untapped:
            text = f"{actor} untaps" if actor else "A permanent untaps"
            return Narration(
                kind="untap", text=text, turn=turn, phase=phase, actor=actor,
                detail={"tapped": False},
            )
        text = f"{actor} becomes tapped" if actor else "A permanent becomes tapped"
        return Narration(
            kind="other", text=text, turn=turn, phase=phase, actor=actor,
            detail={"tapped": True},
        )

    if etype == "CardCounters":
        text = f"{actor} gets counters" if actor else "A permanent gets counters"
        return Narration(
            kind="other", text=text, turn=turn, phase=phase, actor=actor,
            detail={
                "old": int(getattr(event, "old_value", 0) or 0),
                "new": int(getattr(event, "new_value", 0) or 0),
                "raw": extra,
            },
        )

    if etype == "AttachmentMoved":
        return Narration(
            kind="other", text=f"{actor} moves", turn=turn, phase=phase,
            actor=actor, detail={"raw": extra},
        )

    return None


def narrate_option(
    card_name: str,
    *,
    option_kind: str = "",
    turn: int | None = None,
    phase: str = "",
    detail: dict[str, Any] | None = None,
) -> Narration | None:
    """Decision-stream fallback: narrate a policy action when no event exists."""
    name = _clean(card_name)
    if not name:
        return None
    kind_word = option_kind.lower()
    kind = "cast" if ("cast" in kind_word or "spell" in kind_word) else "activate"
    text = f"{name} is cast" if kind == "cast" else f"{name} is activated"
    return Narration(
        kind=kind, text=text, turn=turn, phase=phase, actor=name,
        detail=dict(detail or {}),
    )


__all__ = ["NARRATION_KINDS", "Narration", "narrate_game_event", "narrate_option"]
