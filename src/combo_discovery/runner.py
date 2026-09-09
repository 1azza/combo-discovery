"""Game drivers: single game, multi-seed runs, and determinism checking (v2).

The canonical event source is the PollEvents stream (drained via
client.drain_events); StepResult.events is only a server-side convenience.
Only PLAYER_TYPE_REMOTE players are driven through GetDecision/SubmitDecision;
for all-AI/goldfish games the runner simply waits for game over.
"""

from __future__ import annotations

import grpc
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

from .env import ForgeEnvClient, GameNotActiveError, HarnessConnectionError
from .generated import forge_env_pb2 as pb

# Consecutive get_decision deadline failures tolerated before giving up. The
# server guarantees wake-on-game-over, so a deadline usually means a slow
# engine stretch; each failure is followed by an is_game_over re-check.
MAX_DECISION_TIMEOUTS = 3

# Identity tuple for one event: everything deterministic about it, minus the
# game_id (which legitimately differs between runs).
EventKey = tuple[int, str, int, str, int, str, str]
# (seq, type, turn, phase, player, card_name, detail)


def event_identity(e: pb.GameEvent) -> EventKey:
    """Deterministic identity of a GameEvent: seq included, game_id excluded."""
    return (e.seq, e.type, e.turn, e.phase, e.player, e.card_name, e.detail)


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


def run_game(
    client,
    decks: list[tuple[str, str]],
    seed: int,
    policy: Callable[[pb.DecisionRequest], int] | None = None,
    max_turns: int = 0,
    timeout_seconds: int = 0,
    collect_events: bool = True,
    player_types: Sequence["pb.PlayerType"] | None = None,
) -> GameResult:
    """Run one game to completion and return its result.

    The trajectory is taken from the poll event stream (drain_events), which
    works for goldfish-only and Forge-AI-only games as well as remote-driven
    ones. GetDecision/SubmitDecision are only used when at least one player
    is PLAYER_TYPE_REMOTE.
    """
    start = time.monotonic()
    if player_types is None:
        player_types = [pb.PLAYER_TYPE_REMOTE] * len(decks)
    has_remote = any(t == pb.PLAYER_TYPE_REMOTE for t in player_types)
    game_id = client.start_game(
        decks,
        seed,
        player_types=list(player_types),
        max_turns=max_turns,
        timeout_seconds=timeout_seconds,
    )
    turns = 0
    if has_remote:
        deadline_failures = 0
        while True:
            over = client.is_game_over(game_id)
            if over.over:
                break
            try:
                req = client.get_decision(game_id)
            except GameNotActiveError:
                # The game ended while we were blocked waiting for a decision
                # (turn limit, timeout, opponent win). This is a normal exit.
                over = client.is_game_over(game_id)
                if not over.over:
                    raise
                break
            except HarnessConnectionError as e:
                if e.code is not grpc.StatusCode.DEADLINE_EXCEEDED:
                    raise
                # Blocking wait timed out (slow engine stretch). Re-check game
                # over at the top of the loop and retry; raise after too many
                # consecutive deadlines.
                deadline_failures += 1
                if deadline_failures >= MAX_DECISION_TIMEOUTS:
                    raise
                time.sleep(0.1)
                continue
            deadline_failures = 0
            turns = req.turn
            if policy is None:
                option_id = sorted(req.options, key=lambda o: o.id)[-1].id
            else:
                option_id = policy(req)
            # Echo the outstanding decision id back to the server.
            try:
                client.submit_decision(game_id, req.decision_id, option_id)
            except GameNotActiveError:
                # Game ended between fetching and submitting (e.g. turn-limit
                # fired mid-decision). Normal exit path.
                over = client.is_game_over(game_id)
                if not over.over:
                    raise
                break
    else:
        # No remote decisions: the harness drives itself; just wait it out.
        while True:
            over = client.is_game_over(game_id)
            if over.over:
                break
            time.sleep(0.1)
            turns = client.get_state(game_id).turn
    # Drain the full event buffer before StopGame drops it.
    events = [event_identity(e) for e in client.drain_events(game_id)] if collect_events else []
    client.stop_game(game_id)
    return GameResult(
        game_id=game_id,
        seed=seed,
        winner=over.winner,
        turns=turns,
        n_events=len(events),
        duration_s=time.monotonic() - start,
        outcome=over.outcome,
        reason=over.reason,
        events=events,
    )


def run_games(
    client,
    decks: list[tuple[str, str]],
    seeds: list[int],
    policy: Callable[[pb.DecisionRequest], int] | None = None,
    max_turns: int = 0,
    timeout_seconds: int = 0,
    player_types: Sequence["pb.PlayerType"] | None = None,
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
