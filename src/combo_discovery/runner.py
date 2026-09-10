"""Game drivers: single game, multi-seed runs, and determinism checking (v3).

The canonical event source is the PollEvents stream (drained via
client.drain_events); StepResult.events is only a server-side convenience.
Only PLAYER_TYPE_REMOTE players are driven through GetDecision/SubmitDecision;
for all-AI/goldfish games the runner simply waits for game over.

v3: decisions are typed. Policies receive a DecisionContext (wrapping the raw
pb.DecisionRequest) and return an answer tuple consumable by
ForgeEnvClient.submit_decision — e.g. ("option_id", 3), ("attackers", [...]),
("scry", (top, bottom)). default_policy implements a simple legal strategy for
every DecisionType.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import cast

from .env import (
    Answer,
    DamageTarget,
    ForgeEnvClient,
    GameNotActiveError,
    HarnessTimeoutError,
    StaleDecisionError,
)
from .generated import forge_env_pb2 as pb

logger = logging.getLogger(__name__)

# Consecutive get_decision deadline failures tolerated before giving up. The
# server guarantees wake-on-game-over, so a deadline usually means a slow
# engine stretch; each failure is followed by an is_game_over re-check.
MAX_DECISION_TIMEOUTS = 3

# Retries after the INITIAL stale-decision rejection (so up to
# MAX_STALE_RETRIES + 1 total rejected attempts are allowed before raising).
# The harness decision watchdog (120s) can release a decision while a slow
# policy's submit is in flight, surfacing as INVALID_ARGUMENT. Each rejection
# is followed by a game-over re-check and a refetch of the current decision;
# the counter resets only on a successful submit.
MAX_STALE_RETRIES = 3

# Retries for a TRANSIENT HarnessTimeoutError on a quick polling RPC
# (is_game_over / get_state). A deadline on a fast RPC is not server death;
# retry a couple of times with backoff, then let the caller act.
MAX_POLL_TIMEOUTS = 3
POLL_BACKOFF_S = 0.1

# Identity tuple for one event: everything deterministic about it, minus the
# game_id (which legitimately differs between runs).
EventKey = tuple[int, str, int, str, int, str, str]
# (seq, type, turn, phase, player, card_name, detail)

# One entry of the decision trace: (decision_id, decision_type, answer).
# decision_type is the pb.DecisionType enum value; answer is the typed answer
# exactly as submitted.
DecisionTraceEntry = tuple[int, int, Answer]


def event_identity(e: pb.GameEvent) -> EventKey:
    """Deterministic identity of a GameEvent: seq included, game_id excluded."""
    return (e.seq, e.type, e.turn, e.phase, e.player, e.card_name, e.detail)


def _retry_transient_timeout(fn, *args):
    """Call a quick polling RPC, retrying HarnessTimeoutError with backoff.

    A DEADLINE_EXCEEDED on a fast RPC (is_game_over / get_state) is not server
    death — under load even quick calls can exceed the general timeout while
    the harness stays healthy. Retry a bounded number of times, then re-raise
    so the caller (or the worker pool) can decide what to do.
    """
    for attempt in range(MAX_POLL_TIMEOUTS):
        try:
            return fn(*args)
        except HarnessTimeoutError:
            if attempt + 1 >= MAX_POLL_TIMEOUTS:
                raise
            time.sleep(POLL_BACKOFF_S * (attempt + 1))
    raise HarnessTimeoutError("poll retry loop exhausted")  # pragma: no cover


@dataclass
class DecisionContext:
    """Typed view over a pb.DecisionRequest, used by policies.

    Exposes the per-type payload fields of the raw request with friendlier
    names; the raw pb message stays available as `.raw`.
    """

    request: pb.DecisionRequest

    # -- identity -----------------------------------------------------------

    @property
    def game_id(self) -> int:
        return self.request.game_id

    @property
    def decision_id(self) -> int:
        return self.request.decision_id

    @property
    def player(self) -> int:
        return self.request.player

    @property
    def turn(self) -> int:
        return self.request.turn

    @property
    def phase(self) -> str:
        return self.request.phase

    @property
    def decision_type(self) -> int:
        """pb.DecisionType enum value of the outstanding decision."""
        return self.request.decision_type

    @property
    def type_name(self) -> str:
        return pb.DecisionType.Name(self.request.decision_type)

    @property
    def prompt(self) -> str:
        return self.request.prompt

    # -- PRIORITY -----------------------------------------------------------

    @property
    def options(self) -> list[pb.Option]:
        return list(self.request.options)

    # -- card-selection payloads --------------------------------------------

    @property
    def candidate_ids(self) -> list[int]:
        """Engine card ids of the candidate cards, in request order."""
        return [c.card_id for c in self.request.candidates]

    @property
    def candidate_names(self) -> list[str]:
        return [c.name for c in self.request.candidates]

    @property
    def candidates(self) -> list[pb.CardCandidate]:
        return list(self.request.candidates)

    @property
    def min_choices(self) -> int:
        return self.request.min_choices

    @property
    def max_choices(self) -> int:
        return self.request.max_choices

    @property
    def is_optional(self) -> bool:
        return self.request.optional

    # -- ANNOUNCE ------------------------------------------------------------

    @property
    def min_number(self) -> int:
        return self.request.min_number

    @property
    def max_number(self) -> int:
        return self.request.max_number

    # -- combat ---------------------------------------------------------------

    @property
    def defender_players(self) -> list[int]:
        return list(self.request.defender_players)

    @property
    def attacker_cards(self) -> list[int]:
        return list(self.request.attacker_cards)

    @property
    def damage_amount(self) -> int:
        return self.request.damage_amount

    @property
    def damage_source_card(self) -> int:
        return self.request.damage_source_card

    # -- mulligan --------------------------------------------------------------

    @property
    def cards_to_return(self) -> int:
        return self.request.cards_to_return

    # -- CHOOSE_TARGETS / CHOOSE_MODE / OPTIONAL_COSTS (v4) --------------------

    @property
    def spell_description(self) -> str:
        return self.request.spell_description

    @property
    def mandatory(self) -> bool:
        """CHOOSE_TARGETS: min_choices must be reached."""
        return self.request.mandatory

    @property
    def allow_repeat(self) -> bool:
        """CHOOSE_MODE: the same mode may be chosen more than once."""
        return self.request.allow_repeat

    @property
    def mode_options(self) -> list[tuple[int, str]]:
        """(id, description) pairs for CHOOSE_MODE / OPTIONAL_COSTS, in order."""
        return [(m.id, m.description) for m in self.request.mode_options]


def default_policy(ctx: DecisionContext) -> Answer:
    """A simple legal answer for every decision type.

    Deliberately naive (this is a research harness client, not an AI): the
    only goal is to return answers the server accepts so games reach an end.
    """
    t = ctx.decision_type
    if t == pb.DECISION_TYPE_PRIORITY:
        # Previous v2 behavior: highest option id.
        if not ctx.options:
            raise ValueError("PRIORITY decision with no options")
        return ("option_id", sorted(ctx.options, key=lambda o: o.id)[-1].id)
    if t == pb.DECISION_TYPE_MULLIGAN_KEEP:
        return ("boolean_answer", True)
    if t == pb.DECISION_TYPE_MULLIGAN_TUCK:
        # Tuck the required number of cards, last candidates first, keeping
        # their current order (bottom-stacking order, first = topmost).
        ids = ctx.candidate_ids
        return ("card_ids", ids[-ctx.cards_to_return :] if ctx.cards_to_return else [])
    if t == pb.DECISION_TYPE_DECLARE_ATTACKERS:
        defender = ctx.defender_players[0] if ctx.defender_players else 0
        return ("attackers", [(card_id, defender) for card_id in ctx.candidate_ids])
    if t == pb.DECISION_TYPE_DECLARE_BLOCKERS:
        return ("blockers", [])
    if t == pb.DECISION_TYPE_ASSIGN_COMBAT_DAMAGE:
        # candidates = remaining blockers; defender_players = defending player.
        # Assign ALL damage to the FIRST blocker: that is always legal (the
        # at-least-lethal ordering is trivially satisfied, and excess damage to
        # the first blocker is allowed). Assigning it all to the defending
        # player would be rule-invalid whenever the attacker is blocked by a
        # non-trampling creature. Only route to the player arm when the
        # attacker is unblocked (no blockers). Full trample/deathtouch-aware
        # assignment validation is deferred: this default policy is
        # always-legal by construction, not by modelling combat.
        if ctx.candidate_ids:
            return (
                "damage",
                [DamageTarget(card_id=ctx.candidate_ids[0], amount=ctx.damage_amount)],
            )
        defender = ctx.defender_players[0] if ctx.defender_players else 0
        return ("damage", [DamageTarget(player=defender, amount=ctx.damage_amount)])
    if t == pb.DECISION_TYPE_ORDER_BLOCKERS:
        # Current order unchanged.
        return ("card_ids", ctx.candidate_ids)
    if t == pb.DECISION_TYPE_CHOOSE_CARDS:
        if ctx.is_optional:
            return ("card_ids", [])
        return ("card_ids", ctx.candidate_ids[: ctx.min_choices])
    if t == pb.DECISION_TYPE_ANNOUNCE:
        # Just max_number; range validation is server-side.
        return ("number_answer", ctx.max_number)
    if t == pb.DECISION_TYPE_SCRY_ARRANGE:
        # Keep everything on top in the current order.
        return ("scry", (ctx.candidate_ids, []))
    if t == pb.DECISION_TYPE_CHOOSE_TARGETS:
        # Meet min_choices with the first legal candidates: card targets
        # first, then player slots (defender_players) for the remainder.
        # When min_choices is 0 the empty selection is legal, so choose
        # nothing (never commit targets "just because").
        need = ctx.min_choices
        if need <= 0:
            return ("targets", ([], []))
        card_ids = ctx.candidate_ids[:need]
        player_slots = ctx.defender_players[: max(0, need - len(card_ids))]
        return ("targets", (card_ids, player_slots))
    if t == pb.DECISION_TYPE_CHOOSE_MODE:
        # First min_choices mode ids, in request order. (allow_repeat only
        # matters if the same mode is chosen twice; first-N distinct is legal.)
        need = ctx.min_choices
        if need <= 0:
            return ("mode_selection", [])
        return ("mode_selection", [mode_id for mode_id, _ in ctx.mode_options[:need]])
    if t == pb.DECISION_TYPE_OPTIONAL_COSTS:
        # Never pay an optional cost (kicker etc.) by default: the empty
        # selection is legal and is the conservative research default.
        return ("mode_selection", [])
    raise ValueError(f"no default policy for decision type {ctx.type_name}")


@dataclass
class GameResult:
    game_id: int
    seed: int
    winner: int
    turns: int
    n_events: int
    duration_s: float
    outcome: int = 0  # pb.Outcome enum value (OUTCOME_UNSPECIFIED if unknown)
    reason: str = ""
    events: list[EventKey] = field(default_factory=list)
    decision_trace: list[DecisionTraceEntry] = field(default_factory=list)


def run_game(
    client,
    decks: list[tuple[str, str]],
    seed: int,
    policy: Callable[[DecisionContext], Answer] | None = None,
    max_turns: int = 0,
    timeout_seconds: int = 0,
    collect_events: bool = True,
    player_types: Sequence["pb.PlayerType"] | None = None,
    force_stop_active: bool = False,
) -> GameResult:
    """Run one game to completion and return its result.

    The trajectory is taken from the poll event stream (drain_events), which
    works for goldfish-only and Forge-AI-only games as well as remote-driven
    ones. GetDecision/SubmitDecision are only used when at least one player
    is PLAYER_TYPE_REMOTE. ``policy`` receives a DecisionContext and returns
    a typed answer; None means default_policy. ``force_stop_active`` tells
    the harness to stop any leftover active game before starting (used by the
    worker pool when recovering a worker).
    """
    if policy is None:
        policy = default_policy
    start = time.monotonic()
    if player_types is None:
        player_types = [pb.PLAYER_TYPE_REMOTE] * len(decks)
    has_remote = any(t == pb.PLAYER_TYPE_REMOTE for t in player_types)
    # started_game_id is set once StartGame succeeds. The guard below
    # best-effort stops the game on ANY failure path so a crashed run cannot
    # leave an active game behind and brick the harness for later jobs.
    started_game_id: int | None = None
    try:
        started_game_id = int(
            client.start_game(
                decks,
                seed,
                player_types=list(player_types),
                max_turns=max_turns,
                timeout_seconds=timeout_seconds,
                force_stop_active=force_stop_active,
            )
        )
        game_id = started_game_id
        turns = 0
        trace: list[DecisionTraceEntry] = []
        if has_remote:
            deadline_failures = 0
            stale_failures = 0
            while True:
                # IsGameOver is atomic with respect to the latest classification
                # read (over == a GameOver classification exists), so over flips
                # only once the GameOver event has been appended. Polling may
                # therefore run slightly longer near the end; the sleeps in the
                # retry branches keep it off a busy-tight-loop.
                over = _retry_transient_timeout(client.is_game_over, game_id)
                if over.over:
                    break
                try:
                    req = client.get_decision(game_id)
                except GameNotActiveError:
                    # The game ended while we were blocked waiting for a
                    # decision (turn limit, timeout, opponent win). Normal exit.
                    over = _retry_transient_timeout(client.is_game_over, game_id)
                    if not over.over:
                        raise
                    break
                except HarnessTimeoutError:
                    # Blocking wait timed out (slow engine stretch). Re-check
                    # game over at the top of the loop and retry; raise after
                    # too many consecutive deadlines.
                    deadline_failures += 1
                    if deadline_failures >= MAX_DECISION_TIMEOUTS:
                        raise
                    time.sleep(0.1)
                    continue
                except StaleDecisionError:
                    # The watchdog released this decision before our submit (or
                    # a re-prompt invalidated it). Re-check game over at the top
                    # of the loop and refetch; raise after too many retries.
                    stale_failures += 1
                    if stale_failures > MAX_STALE_RETRIES:
                        raise
                    time.sleep(0.05)
                    continue
                deadline_failures = 0
                turns = req.turn
                ctx = DecisionContext(request=req)
                answer = policy(ctx)
                # Echo the outstanding decision id back with the typed answer.
                try:
                    client.submit_decision(game_id, req.decision_id, answer)
                except GameNotActiveError:
                    # Game ended between fetching and submitting (e.g.
                    # turn-limit fired mid-decision). Normal exit path.
                    over = _retry_transient_timeout(client.is_game_over, game_id)
                    if not over.over:
                        raise
                    break
                except StaleDecisionError:
                    # The watchdog released this decision while our (possibly
                    # slow) submit was in flight; the server's next decision
                    # has a NEW id. Re-check, refetch, retry — bounded.
                    stale_failures += 1
                    if stale_failures > MAX_STALE_RETRIES:
                        raise
                    time.sleep(0.05)
                    continue
                stale_failures = 0
                trace.append((req.decision_id, req.decision_type, answer))
        else:
            # No remote decisions: the harness drives itself; wait it out.
            # IsGameOver now reflects an atomic classification read (over is
            # false until the GameOver event is appended), so the poll may run
            # a little longer near the end; the 0.1s sleep keeps it from being
            # a busy-tight-loop.
            while True:
                over = _retry_transient_timeout(client.is_game_over, game_id)
                if over.over:
                    break
                time.sleep(0.1)
                turns = _retry_transient_timeout(client.get_state, game_id).turn
        # Drain the full event buffer before StopGame drops it.
        events = (
            [event_identity(e) for e in client.drain_events(game_id)]
            if collect_events
            else []
        )
        client.stop_game(game_id)
        result = GameResult(
            game_id=game_id,
            seed=seed,
            winner=over.winner,
            turns=turns,
            n_events=len(events),
            duration_s=time.monotonic() - start,
            outcome=over.outcome,
            reason=over.reason,
            events=events,
            decision_trace=trace,
        )
        started_game_id = None  # stopped cleanly; suppress failure cleanup
        return result
    except BaseException:
        # ANY failure after StartGame leaves a live game on the harness; stop
        # it best-effort so the worker stays usable (pool recovery). Swallow
        # and log secondary cleanup errors, then re-raise the original.
        if started_game_id is not None:
            try:
                client.stop_game(started_game_id)
            except Exception as cleanup_err:  # best effort
                logger.warning(
                    "best-effort stop_game(%s) failed after run error: %s",
                    started_game_id,
                    cleanup_err,
                )
        raise


def run_games(
    client,
    decks: list[tuple[str, str]],
    seeds: list[int],
    policy: Callable[[DecisionContext], Answer] | None = None,
    max_turns: int = 0,
    timeout_seconds: int = 0,
    player_types: Sequence["pb.PlayerType"] | None = None,
    force_stop_active: bool = False,
) -> list[GameResult]:
    return [
        run_game(
            client,
            decks,
            seed,
            policy,
            max_turns=max_turns,
            timeout_seconds=timeout_seconds,
            player_types=player_types,
            force_stop_active=force_stop_active,
        )
        for seed in seeds
    ]


class DeterminismError(AssertionError):
    pass


def _as_identity(x: pb.GameEvent | EventKey) -> EventKey:
    if isinstance(x, pb.GameEvent):
        return event_identity(x)
    return x


def compare_event_streams(
    stream_a: Iterable[pb.GameEvent | EventKey],
    stream_b: Iterable[pb.GameEvent | EventKey],
    game_over_a: tuple[int, int, str] | None = None,
    game_over_b: tuple[int, int, str] | None = None,
) -> None:
    """Assert two event streams are identical (identity tuples incl. seq).

    game_over_* are optional (outcome, winner, reason) triples from the final
    GameOver of each run; when both are given they are compared too.
    Raises DeterminismError on the first divergence.
    """
    a = [_as_identity(x) for x in stream_a]
    b = [_as_identity(x) for x in stream_b]
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            raise DeterminismError(f"event streams diverge at index {i}: first={x!r} second={y!r}")
    if len(a) != len(b):
        raise DeterminismError(f"event streams differ in length: {len(a)} vs {len(b)}")
    if game_over_a is not None and game_over_b is not None:
        if game_over_a != game_over_b:
            raise DeterminismError(
                f"final GameOver differs: first={game_over_a!r} second={game_over_b!r}"
            )


def determinism_check(
    client,
    decks: list[tuple[str, str]],
    seed: int,
    max_turns: int = 0,
    timeout_seconds: int = 0,
    player_types: Sequence["pb.PlayerType"] | None = None,
    divergent_seed: int | None = None,
) -> None:
    """Run the same game twice with the same (decks, seed) and require
    identical event streams (game_id excluded). If divergent_seed is given,
    also run with that seed and require the stream to differ.
    """
    r1 = run_game(
        client,
        decks,
        seed,
        max_turns=max_turns,
        timeout_seconds=timeout_seconds,
        player_types=player_types,
    )
    r2 = run_game(
        client,
        decks,
        seed,
        max_turns=max_turns,
        timeout_seconds=timeout_seconds,
        player_types=player_types,
    )
    compare_event_streams(
        r1.events, r2.events, (r1.outcome, r1.winner, r1.reason), (r2.outcome, r2.winner, r2.reason)
    )
    if divergent_seed is not None:
        r3 = run_game(
            client,
            decks,
            divergent_seed,
            max_turns=max_turns,
            timeout_seconds=timeout_seconds,
            player_types=player_types,
        )
        if r3.events == r1.events:
            raise DeterminismError(
                f"seed {divergent_seed} produced an event stream identical to seed {seed}"
            )


# -- snapshot/restore replay (v5 MCTS determinism guarantee) -----------------

# The harness event type appended on restore; excluded from suffix comparison.
SNAPSHOT_RESTORED_TYPE = "SnapshotRestored"


def _resolve_branch_action(
    branch_action: Answer | Callable[[DecisionContext], Answer] | None,
    ctx: DecisionContext,
    drive_policy,
) -> Answer:
    """The answer used at the branch decision: an explicit Answer, a callable
    ``ctx -> Answer``, or (default) the drive policy."""
    if branch_action is None:
        return drive_policy(ctx)
    if callable(branch_action):
        return branch_action(ctx)
    return cast(Answer, branch_action)


def _relative_identity(identity: EventKey, base_seq: int) -> EventKey:
    """Identity with seq shifted to be relative to ``base_seq``."""
    return (identity[0] - base_seq, *identity[1:])


def _compare_replay_suffixes(
    forward: list[EventKey],
    replay: list[EventKey],
    *,
    base_forward: int,
    base_replay: int,
    context: str,
) -> None:
    """Assert two post-branch event suffixes are byte-identical modulo seq
    offsets, comparing relative seqs (the restore marker shifts the replay).

    Raises DeterminismError with the first divergence and both raw events.
    """
    rel_forward = [_relative_identity(x, base_forward) for x in forward]
    rel_replay = [_relative_identity(x, base_replay) for x in replay]
    for i, (x, y) in enumerate(zip(rel_forward, rel_replay)):
        if x != y:
            raise DeterminismError(
                f"snapshot replay suffix diverges at relative index {i} ({context}): "
                f"forward={x!r} replay={y!r} "
                f"(forward_event={forward[i]!r}, replay_event={replay[i]!r})"
            )
    if len(rel_forward) != len(rel_replay):
        raise DeterminismError(
            f"snapshot replay suffix length mismatch ({context}): "
            f"forward={len(rel_forward)} replay={len(rel_replay)}; "
            f"forward={forward!r} replay={replay!r}"
        )


def snapshot_replay_check(
    client,
    decks: list[tuple[str, str]],
    seed: int,
    player_types: Sequence["pb.PlayerType"] | None = None,
    max_turns: int = 0,
    branch_action: Answer | Callable[[DecisionContext], Answer] | None = None,
    timeout_seconds: int = 0,
    policy: Callable[[DecisionContext], Answer] | None = None,
) -> None:
    """Verify the snapshot/restore determinism guarantee.

    Drives a game to the FIRST PRIORITY decision, snapshots there (recording
    the state hash H1 and the event-buffer cursor S0), submits the branch
    answer, then restores the token and replays the SAME answer. Asserts the
    game is still alive after restore, the state hash is unchanged, and the
    post-``SnapshotRestored`` event suffix is byte-identical to the forward
    suffix (seq offsets differ by the marker, so comparison is on RELATIVE
    seqs). Raises DeterminismError on any mismatch.

    The forward branch stops after the branch answer: the next decision is
    left outstanding, which Restore requires (the server parks the engine on
    an outstanding decision). The game must therefore not end at the branch
    decision.

    ``branch_action`` overrides the answer at the branch decision: an explicit
    Answer tuple, or a callable ``ctx -> Answer``; None means ``policy`` (or
    default_policy). ``policy`` is the drive policy used to reach the branch.
    """
    if player_types is None:
        player_types = [pb.PLAYER_TYPE_REMOTE] * len(decks)
    drive_policy = policy or default_policy
    started_game_id: int | None = None
    try:
        started_game_id = int(
            client.start_game(
                decks,
                seed,
                player_types=list(player_types),
                max_turns=max_turns,
                timeout_seconds=timeout_seconds,
            )
        )
        game_id = started_game_id
        context = f"seed={seed} game_id={game_id}"

        # --- drive to the first PRIORITY decision --------------------------
        while True:
            if _retry_transient_timeout(client.is_game_over, game_id).over:
                raise DeterminismError(
                    f"snapshot_replay_check: game ended before the first PRIORITY "
                    f"decision ({context})"
                )
            req = client.get_decision(game_id)
            if req.decision_type == pb.DECISION_TYPE_PRIORITY:
                break
            client.submit_decision(
                game_id, req.decision_id, drive_policy(DecisionContext(request=req))
            )

        # --- snapshot at the branch point ----------------------------------
        token, hash_at_snapshot = client.snapshot(game_id)
        s0 = client.poll_events(game_id, 0).next_cursor

        # --- forward branch: record and submit the branch answer -----------
        # Restore requires an outstanding decision, so we answer the branch
        # decision and leave the NEXT decision outstanding (the loop above has
        # already confirmed the game is not over before the snapshot).
        branch_answer = _resolve_branch_action(
            branch_action, DecisionContext(request=req), drive_policy
        )
        answers: list[tuple[int, Answer]] = [(req.decision_type, branch_answer)]
        client.submit_decision(game_id, req.decision_id, branch_answer)
        if _retry_transient_timeout(client.is_game_over, game_id).over:
            raise DeterminismError(
                f"snapshot_replay_check: game ended at the branch decision, so there "
                f"is no outstanding decision to restore ({context})"
            )
        forward_suffix = [event_identity(e) for e in client.drain_events(game_id, s0)]

        # --- restore and replay the same answers ---------------------------
        client.restore(game_id, token)
        if _retry_transient_timeout(client.is_game_over, game_id).over:
            raise DeterminismError(
                f"snapshot_replay_check: game reports over immediately after restore "
                f"({context})"
            )
        hash_after = client.get_state(game_id).state_hash
        if hash_after != hash_at_snapshot:
            raise DeterminismError(
                f"snapshot_replay_check: state_hash changed across restore ({context}): "
                f"H1={hash_at_snapshot!r} H2={hash_after!r}"
            )

        for expected_type, ans in answers:
            refetched = client.get_decision(game_id)
            if refetched.decision_type != expected_type:
                raise DeterminismError(
                    f"snapshot_replay_check: replay decision type mismatch ({context}): "
                    f"expected {pb.DecisionType.Name(expected_type)}, got "
                    f"{pb.DecisionType.Name(refetched.decision_type)}"
                )
            client.submit_decision(game_id, refetched.decision_id, ans)

        restored_events = client.drain_events(game_id, s0)
        marker_index = next(
            (i for i, e in enumerate(restored_events) if e.type == SNAPSHOT_RESTORED_TYPE),
            None,
        )
        if marker_index is None:
            raise DeterminismError(
                f"snapshot_replay_check: no {SNAPSHOT_RESTORED_TYPE} event found after "
                f"restore ({context})"
            )
        marker_seq = restored_events[marker_index].seq
        replay_suffix = [event_identity(e) for e in restored_events[marker_index + 1 :]]

        _compare_replay_suffixes(
            forward_suffix,
            replay_suffix,
            base_forward=s0,
            base_replay=marker_seq,
            context=context,
        )
    finally:
        if started_game_id is not None:
            try:
                client.stop_game(started_game_id)
            except Exception as cleanup_err:  # best effort
                logger.warning(
                    "best-effort stop_game(%s) after snapshot replay failed: %s",
                    started_game_id,
                    cleanup_err,
                )
