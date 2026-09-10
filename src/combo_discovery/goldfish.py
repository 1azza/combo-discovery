"""Real goldfish policy for REMOTE-driven games (protocol v6).

A *goldfish* game is solitaire: the opponent is ignored. This policy is a
deterministic, opponent-blind driver for a REMOTE player, so a deck can be
exercised end-to-end without a Forge AI in the other seat.

Behavior per decision type:

  PRIORITY
    1. Play a land if any offered option is a land play (matched on the
       option description: anchored "Land" / "Play land" — so the card name
       "Island" and oracle text like "Destroy target land" do NOT match).
    2. Otherwise cast the cheapest non-land *spell* (``kind == "play_card"``);
       the converted mana cost is parsed from the option description
       (Forge-style ``{1}{G}``, with a legacy ``Cost: 1`` fallback). The
       harness pre-filters PRIORITY options to abilities the player can pay
       for, so every offered spell is affordable.
    3. Otherwise fall back to ``default_policy`` (highest option id — the
       harness offers no explicit "pass" option; its PRIORITY options are the
       playable abilities only, so this is the historical fallback).
  MULLIGAN_KEEP
    Mulligan while the offered hand exceeds ``KEEP_HAND_SIZE`` and keep at it,
    driving the London ladder 7 -> 6 -> 5 and never below. The number of
    mulligans taken this game is tracked in ``mulligans`` and reset by
    ``new_game()`` (run_game calls that hook at the start of each game).
  DECLARE_ATTACKERS
    Attack with every legal candidate against the first defender. The harness
    only offers legally-attacking (untapped, not summoning-sick) cards and the
    default decks' creatures are 2/2 bears, so "everything legal" is the whole
    candidate list; goldfish ignores the opponent, so there is no suicidal
    check to make.
  ASSIGN_COMBAT_DAMAGE and every other type
    ``default_policy`` behavior (first blocker when blocked, else the
    defending player; etc.).

Deterministic by construction: no randomness, and every tie breaks on option
id / request order.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .generated import forge_env_pb2 as pb

if TYPE_CHECKING:  # pragma: no cover - typing only (avoids an import cycle)
    from .runner import Answer, DecisionContext

# The policy mulligans while the offered hand is larger than this and keeps at
# it, so the kept hand always has at least this many cards (7 -> 6 -> 5).
KEEP_HAND_SIZE = 5

# Cost assigned to a cast whose description carries no parseable mana cost:
# most expensive, so any parseable (cheaper) option wins; ties then break by id.
_UNKNOWN_COST = 99

# A land play description ("Land" / "Play land" / "Play land (…)"): anchored so
# oracle text such as "Destroy target land" is not mistaken for a land play.
_LAND_RE = re.compile(r"^\s*(?:play\s+)?land\b", re.IGNORECASE)
_BRACE_SYMBOL_RE = re.compile(r"\{([^}]*)\}")
_LEGACY_COST_RE = re.compile(r"cost[:\s$]*([0-9]+)", re.IGNORECASE)


def _mana_cost(description: str) -> int:
    """Best-effort converted mana cost parsed from an option description.

    Handles Forge-style brace symbols (e.g. ``Grizzly Bears {1}{G}``) and the
    legacy ``Cost: 2 ...`` spelling. ``{X}``/``{Y}``/``{Z}`` count 0;
    coloured/hybrid/phyrexian symbols count 1. Returns ``_UNKNOWN_COST`` when
    nothing parses.
    """
    symbols = _BRACE_SYMBOL_RE.findall(description)
    if symbols:
        total = 0
        for symbol in symbols:
            token = symbol.strip().lower()
            if token.isdigit():
                total += int(token)
            elif token and token not in ("x", "y", "z"):
                total += 1
        return total
    match = _LEGACY_COST_RE.search(description)
    return int(match.group(1)) if match else _UNKNOWN_COST


def _is_land_option(option: pb.Option) -> bool:
    """True when the option description marks a land play."""
    return bool(_LAND_RE.search(option.description))


def _fallback(ctx: "DecisionContext") -> "Answer":
    """Deferred import: runner imports this module, so importing it at module
    load would create a cycle. At call time runner is fully initialized."""
    from .runner import default_policy

    return default_policy(ctx)


class GoldfishPolicy:
    """Deterministic, opponent-blind policy. Callable: ``ctx -> Answer``.

    The runner accepts any callable; passing an instance makes the per-game
    ``new_game()`` reset hook available (run_game invokes it when present).
    """

    def __init__(self) -> None:
        self.mulligans = 0

    def new_game(self) -> None:
        """Reset per-game state. run_game calls this at each game start."""
        self.mulligans = 0

    def __call__(self, ctx: "DecisionContext") -> "Answer":
        decision = ctx.decision_type
        if decision == pb.DECISION_TYPE_PRIORITY:
            return self._priority(ctx)
        if decision == pb.DECISION_TYPE_MULLIGAN_KEEP:
            return self._mulligan_keep(ctx)
        if decision == pb.DECISION_TYPE_DECLARE_ATTACKERS:
            # Goldfish: attack with everything legal against the first
            # defender. Candidates are the harness's legal attackers (untapped
            # 2-power-or-more bears in the default decks); no opponent-facing
            # risk assessment is possible or wanted.
            defender = ctx.defender_players[0] if ctx.defender_players else 0
            return ("attackers", [(card_id, defender) for card_id in ctx.candidate_ids])
        # ASSIGN_COMBAT_DAMAGE (first blocker / player) and every other type
        # keep the always-legal default behavior.
        return _fallback(ctx)

    # -- PRIORITY -----------------------------------------------------------

    def _priority(self, ctx: "DecisionContext") -> "Answer":
        options = ctx.options
        if not options:
            raise ValueError("PRIORITY decision with no options")
        lands = [option for option in options if _is_land_option(option)]
        if lands:
            return ("option_id", min(lands, key=lambda option: option.id).id)
        spells = [option for option in options if option.kind == "play_card"]
        if spells:
            cheapest = min(
                spells, key=lambda option: (_mana_cost(option.description), option.id)
            )
            return ("option_id", cheapest.id)
        # No land and no castable spell: the historical fallback (highest id).
        return _fallback(ctx)

    # -- MULLIGAN_KEEP ------------------------------------------------------

    def _mulligan_keep(self, ctx: "DecisionContext") -> "Answer":
        hand_size = len(ctx.candidates)
        if hand_size > KEEP_HAND_SIZE:
            self.mulligans += 1
            return ("boolean_answer", False)
        return ("boolean_answer", True)
