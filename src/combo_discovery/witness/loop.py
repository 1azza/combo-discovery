"""Observations and loop detection.

Split out of ``combo_discovery.witness``; behaviour is unchanged.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ..generated import forge_env_pb2 as pb

#: Resource counters tracked for growth.  These are deliberately excluded from
#: the witness signature so a growing loop can still recur structurally.
#: ``graveyard``/``library``/``hand`` are included because discard/draw/mill
#: move cards between them monotonically every iteration; they are reported in
#: the evidence but are *not* durable enough to qualify a loop on their own (see
#: :data:`GAME_STATE_GROWTH_KEYS`).
GROWTH_KEYS = (
    "mana",
    "tokens",
    "life",
    "damage",
    "casts",
    "spells_resolved",
    "extra_phases",
    "permanents",
    "graveyard",
    "library",
    "hand",
)

#: Growth in these counters is driven by the *policy's own* repeated actions
#: (the witness activating/casting on each pass), not by an unbounded game
#: resource.  On their own they must not qualify a recurrence: a static board
#: plus the policy spinning produces `casts`/`spells_resolved` growth with no
#: loop.  Only these keys count as a real, game-state resource.
GAME_STATE_GROWTH_KEYS = ("mana", "tokens", "life", "damage", "permanents")

#: Consecutive observations with an unchanged structural signature and no
#: game-state resource growth before the driver forces a phase-advancing pass.
#: A ``Kiki-Jiki + Pestermite`` loop also has a constant signature but grows
#: ``tokens`` every iteration, so it never reaches this threshold; a pilot
#: spinning on a no-op (e.g. repeatedly equipping inside turn 1) does and is
#: nudged to the next phase, where its real trigger can fire.
SPIN_THRESHOLD = 2

#: Hard cap on forced passes per witness run: a line that cannot make progress
#: still terminates instead of passing forever (after the cap the policy's own
#: answers are used again, exactly as before this fix).
MAX_FORCED_PASSES = 8


# ---------------------------------------------------------------------------
# Observations / loop detection
# ---------------------------------------------------------------------------


@dataclass
class Observation:
    """One captured witness step (usually one completed loop iteration)."""

    iteration: int
    signature: str
    resources: dict[str, int] = field(default_factory=dict)
    signature_fields: dict[str, Any] = field(default_factory=dict)
    state_hash: str = ""
    event_seq: int = 0
    turn: int = 0
    phase: str = ""


def _zone_count(zone: Any) -> int:
    total = 0
    for card in getattr(zone, "cards", ()):  # CardRef(name, count)
        count = int(getattr(card, "count", 0) or 0)
        total += count if count > 0 else 1
    return total


def _battlefield_entries(state: pb.FullState) -> list[tuple[Any, ...]]:
    """Non-token battlefield permanents as the structural signature.

    Token permanents are deliberately excluded: a token-growing loop (e.g.
    Kiki-Jiki copying a creature every iteration) adds a new token each pass, so
    including tokens would make the structural signature change on every
    iteration and the recurrence could never be detected.  Token counts are
    tracked as a *growing resource* instead (:func:`resource_totals`).
    """
    permanents = list(getattr(state, "battlefield_cards", ()) or ())
    entries: list[tuple[Any, ...]] = []
    for zone in state.battlefield:
        for pid in zone.permanents:
            if 0 <= pid < len(permanents):
                perm = permanents[pid]
                if bool(perm.is_token):
                    continue
                counters = tuple(
                    sorted((str(c.type), int(c.count)) for c in perm.typed_counters)
                )
                entries.append(
                    (
                        str(perm.card_name),
                        bool(perm.tapped),
                        counters,
                    )
                )
            else:  # harness without a flat battlefield listing: fall back to names
                for card in zone.cards:
                    entries.append((str(card.name), False, ()))
    entries.sort()
    return entries


def _non_token_battlefield_count(state: pb.FullState, zone: Any) -> int:
    """Count one player's battlefield permanents that are not tokens."""
    permanents = list(getattr(state, "battlefield_cards", ()) or ())
    total = 0
    for pid in zone.permanents:
        if 0 <= pid < len(permanents):
            if not bool(permanents[pid].is_token):
                total += 1
        else:
            total += len(list(zone.cards))
    return total


def witness_signature(state: pb.FullState) -> dict[str, Any]:
    """The structural projection that must recur for a loop.

    Captures *non-token* battlefield names/tapped/counters, phase, active
    player, the non-token battlefield count, ``exile``/``command`` counts and
    the stack shape.  Monotonic *scalars* (mana pool, **life totals**, total
    damage, cast counts, **token count**) are deliberately excluded so they can
    accumulate across iterations without changing the signature; they are
    tracked as growing resources by :func:`resource_totals`.  A token-growing
    loop therefore shows a recurring structure + a strictly growing ``tokens``
    resource.

    The *volume* zone counts ``graveyard``/``library``/``hand`` are excluded for
    the same reason: discard/draw/mill move cards between them monotonically
    every iteration, so including them stops a genuine loop from recurring
    (Fear of Missing Out's ETB "discard a card, then draw a card" grows the
    graveyard each pass).  They are reported as resources by
    :func:`resource_totals`.  The battlefield count and ``exile``/``command``
    stay structural: the non-token board must recur, and exile/command changes
    are loop-relevant board shifts rather than per-iteration churn.

    Life is excluded for a concrete reason: a combat loop damages the opponent
    every iteration, so including life would stop the structure from recurring
    and reject a genuine loop (Combat Celebrant + Kiki-Jiki).  ``turn`` is also
    excluded (a turn-cycling loop should recur).  Library order is not exposed
    by FullState v2.
    """
    return {
        "phase": state.phase,
        "active_player": state.active_player,
        "battlefield": _battlefield_entries(state),
        "zone_counts": {
            "battlefield": [
                _non_token_battlefield_count(state, z) for z in state.battlefield
            ],
            "exile": [_zone_count(z) for z in state.exile],
            "command": [_zone_count(z) for z in state.command],
        },
        "stack": [
            (str(e.card_name), str(e.sa_description), int(e.controller))
            for e in state.stack
        ],
    }


def _signature_hash(fields: dict[str, Any]) -> str:
    blob = json.dumps(fields, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def resource_totals(
    state: pb.FullState, *, cast_count: int = 0, spells_resolved: int = 0,
    extra_phases: int = 0,
) -> dict[str, int]:
    """Monotonic resource counters used to qualify a structural recurrence."""
    mana = 0
    typed = list(getattr(state, "typed_mana_pools", ()) or ())
    if typed:
        for pool in typed:
            mana += (
                int(pool.white)
                + int(pool.blue)
                + int(pool.black)
                + int(pool.red)
                + int(pool.green)
                + int(pool.colorless)
            )
    else:  # pragma: no cover - legacy harness fallback
        for value in (getattr(state, "mana_pools", None) or {}).values():
            mana += int(value or 0)

    tokens = 0
    damage = 0
    permanents = list(getattr(state, "battlefield_cards", ()) or ())
    for zone in state.battlefield:
        for pid in zone.permanents:
            if 0 <= pid < len(permanents):
                if permanents[pid].is_token:
                    tokens += 1
                damage += int(permanents[pid].damage)
    return {
        "mana": mana,
        "tokens": tokens,
        "life": sum(int(v) for v in state.life),
        "damage": damage,
        "casts": int(cast_count),
        "spells_resolved": int(spells_resolved),
        "extra_phases": int(extra_phases),
        "permanents": sum(len(list(z.permanents)) for z in state.battlefield),
        # Zone *volumes*: discard/draw/mill grow these monotonically, so they
        # are resources, not structure (see :func:`witness_signature`).
        "graveyard": sum(_zone_count(z) for z in state.graveyard),
        "library": sum(_zone_count(z) for z in state.library),
        "hand": sum(_zone_count(z) for z in state.hand),
    }


def build_observation(
    iteration: int,
    state: pb.FullState,
    *,
    cast_count: int = 0,
    spells_resolved: int = 0,
    extra_phases: int = 0,
    event_seq: int = 0,
) -> Observation:
    fields = witness_signature(state)
    return Observation(
        iteration=int(iteration),
        signature=_signature_hash(fields),
        resources=resource_totals(
            state,
            cast_count=cast_count,
            spells_resolved=spells_resolved,
            extra_phases=extra_phases,
        ),
        signature_fields=fields,
        state_hash=str(state.state_hash),
        event_seq=int(event_seq),
        turn=int(getattr(state, "turn", 0) or 0),
        phase=str(getattr(state, "phase", "") or ""),
    )


def _grown_between(before: dict[str, int], after: dict[str, int]) -> list[str]:
    grown = [
        key
        for key in GROWTH_KEYS
        if int(after.get(key, 0)) > int(before.get(key, 0))
    ]
    # Also honour any extra keys a caller supplied.
    for key in after:
        if key not in GROWTH_KEYS and int(after[key]) > int(before.get(key, 0)):
            if key not in grown:
                grown.append(key)
    return sorted(grown)


def _game_state_grew(before: dict[str, int], after: dict[str, int]) -> bool:
    """True when any *game-state* resource grew between two samples.

    Restricted to :data:`GAME_STATE_GROWTH_KEYS` on purpose: the event counters
    ``casts``/``spells_resolved`` are driven by the policy's own repeated
    actions, so a static board the pilot keeps poking shows counter growth with
    no real progress.  Such a pair is a *spin*, not a loop iteration.
    """
    return any(
        int(after.get(key, 0)) > int(before.get(key, 0))
        for key in GAME_STATE_GROWTH_KEYS
    )


def _pass_option(options: Sequence[pb.Option]) -> pb.Option | None:
    """The first ``kind == "pass"`` option in a PRIORITY request, if any."""
    for option in options:
        if str(option.kind or "").strip().lower() == "pass":
            return option
    return None


def detect_loop(
    observations: Sequence[Observation],
) -> tuple[str, dict[str, Any]]:
    """Classify a witness run from its per-iteration observations.

    Returns ``(verdict, evidence)`` with verdict one of ``loops`` / ``no_loop``
    / ``inconclusive``.

    Rule:

    * ``inconclusive`` with fewer than two *post-baseline* observations;
    * ``loops`` (degenerate) when two CONSECUTIVE post-baseline observations in
      the same turn share a non-empty identical ``state_hash`` **and** a durable
      resource grew between them (identical states with no growth is a stall);
    * ``loops`` when two post-baseline observations in the same turn share a
      non-empty witness signature and a tracked resource grew between them;
    * ``no_loop`` when a same-turn signature recurs but nothing grew, when a
      recurrence only happens across turns, or when no signature recurs.

    Two independent traps motivated the post-baseline / same-turn guards (both
    observed live on the Forge harness):

    * **Baseline pair.** Observation 0 is sampled *before* the first iteration
      completes. When the engine has not acted yet, observations 0 and 1 are
      bit-identical, and the old code certified ``loops`` from that pair alone.
      That fired on every candidate whose policy made no immediate progress
      (Keldon Overseer, Elven Raft-Steerer, Firbolg Flutist), regardless of
      whether a loop existed.
    * **Cross-turn recurrence.** A creature that untaps during ordinary play
      (or a token army that attacks each turn) reproduces a structural
      signature across turns while a resource grows. A real infinite combo
      iterates *within one turn*, so both endpoints must share a known turn.
      An unknown turn (0) stays permissive for harnesses that do not report it.
    """
    if len(observations) < 2:
        return "inconclusive", {
            "reason": "need at least two observations",
            "n": len(observations),
        }

    # Drop the pre-injection baseline: identical baseline/post samples are not
    # evidence that anything looped.
    samples = [o for o in observations if int(o.iteration) >= 1]
    if len(samples) < 2:
        return "inconclusive", {
            "reason": "need at least two post-baseline observations",
            "n": len(samples),
        }

    def same_turn(a: Observation, b: Observation) -> bool:
        if a.turn <= 0 or b.turn <= 0:
            return True  # unknown turn: do not exclude
        return a.turn == b.turn

    for i in range(1, len(samples)):
        prev, cur = samples[i - 1], samples[i]
        if not same_turn(prev, cur):
            continue
        if prev.state_hash and cur.state_hash and prev.state_hash == cur.state_hash:
            # Bit-identical consecutive states only certify a loop when a
            # durable resource grew across the step. Two identical samples with
            # no growth is a *stall* (nothing happened), not an infinite loop —
            # the same trap as the original baseline-pair bug, one layer deeper.
            # A genuinely stationary loop with zero net growth is therefore not
            # certified; it is indistinguishable from a stall, and reporting
            # "no loop" beats guessing.
            grown = [
                k for k in _grown_between(prev.resources, cur.resources)
                if k in GAME_STATE_GROWTH_KEYS
            ]
            if not grown:
                continue
            return "loops", {
                "kind": "degenerate",
                "pair": [prev.iteration, cur.iteration],
                "turn": cur.turn,
                "state_hash": cur.state_hash,
                "grown": grown,
            }

    first_repeat: tuple[int, int] | None = None
    counter_only_repeat: tuple[int, int] | None = None
    mana_draining_repeat: tuple[int, int] | None = None
    cross_turn_repeat: tuple[int, int] | None = None
    for j in range(1, len(samples)):
        for i in range(j):
            oi, oj = samples[i], samples[j]
            sig = oi.signature
            if not sig or sig != oj.signature:
                continue
            if not same_turn(oi, oj):
                if cross_turn_repeat is None:
                    cross_turn_repeat = (oi.iteration, oj.iteration)
                continue
            grown = _grown_between(oi.resources, oj.resources)
            game_grown = [k for k in grown if k in GAME_STATE_GROWTH_KEYS]
            if game_grown:
                if int(oj.resources.get("mana", 0)) < int(oi.resources.get("mana", 0)):
                    # The loop strictly consumes mana every pass and nothing in
                    # the pair replenishes it, so it is bounded by the starting
                    # pool, not infinite (Orthion / Jolly Balloon Man-style copy
                    # engines whose activation costs mana while the untapper
                    # untaps the engine, not the lands).
                    if mana_draining_repeat is None:
                        mana_draining_repeat = (oi.iteration, oj.iteration)
                    continue
                return "loops", {
                    "kind": "recurrence",
                    "pair": [oi.iteration, oj.iteration],
                    "turn": oj.turn,
                    "signature": sig,
                    "grown": game_grown,
                }
            if grown:
                # Only policy-driven event counters grew (casts/spells_resolved):
                # a static board the policy kept poking, not an unbounded loop.
                if counter_only_repeat is None:
                    counter_only_repeat = (oi.iteration, oj.iteration)
                continue
            if first_repeat is None:
                first_repeat = (oi.iteration, oj.iteration)

    if first_repeat is not None:
        return "no_loop", {
            "reason": "signature recurred but no tracked resource grew",
            "pair": list(first_repeat),
        }
    if counter_only_repeat is not None:
        return "inconclusive", {
            "reason": "signature recurred but only policy-driven event counters grew "
            "(no game-state resource); cannot confirm a loop",
            "pair": list(counter_only_repeat),
        }
    if mana_draining_repeat is not None:
        return "inconclusive", {
            "reason": "recurrence consumes mana each pass (bounded by the pool, "
            "not an infinite loop)",
            "pair": list(mana_draining_repeat),
        }
    if cross_turn_repeat is not None:
        return "no_loop", {
            "reason": "recurrence only across turns (not an infinite loop within a turn)",
            "pair": list(cross_turn_repeat),
        }
    return "no_loop", {"reason": "no signature recurrence"}
