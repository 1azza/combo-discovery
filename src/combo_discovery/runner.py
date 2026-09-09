"""Game drivers: single game, multi-seed runs, and determinism checking."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .env import ForgeEnvClient
from .generated import forge_env_pb2 as pb


@dataclass
class GameResult:
    game_id: int
    seed: int
    winner: int
    turns: int
    n_events: int
    duration_s: float
    events: list[tuple[str, str, str]] = field(default_factory=list)


def _event_key(e: pb.GameEvent) -> tuple[str, str, str]:
    return (e.type, e.card_name, e.detail)


def run_game(
    client: ForgeEnvClient,
    decks: list[tuple[str, str]],
    seed: int,
    policy: Callable[[pb.DecisionRequest], int] | None = None,
    max_turns: int = 0,
    timeout_seconds: int = 0,
    collect_events: bool = True,
    drive: bool = True,
) -> GameResult:
    start = time.monotonic()
    resp = client.start_game(
        decks, seed, max_turns=max_turns, timeout_seconds=timeout_seconds
    )
    events: list[tuple[str, str, str]] = []
    turns = 0
    while True:
        over = client.is_game_over()
        if over.over:
            result = over
            break
        if not drive:
            time.sleep(0.2)
            turns = client.get_state().turn
            continue
        req = client.get_decision()
        turns = req.turn
        if policy is None:
            option_id = sorted(req.options, key=lambda o: o.id)[-1].id
        else:
            option_id = policy(req)
        step = client.submit_decision(option_id)
        if collect_events:
            events.extend(_event_key(e) for e in step.events)
    return GameResult(
        game_id=resp.game_id,
        seed=seed,
        winner=result.winner,
        turns=turns,
        n_events=len(events),
        duration_s=time.monotonic() - start,
        events=events,
    )


def run_games(
    client: ForgeEnvClient,
    decks: list[tuple[str, str]],
    seeds: list[int],
    policy: Callable[[pb.DecisionRequest], int] | None = None,
    max_turns: int = 0,
    timeout_seconds: int = 0,
) -> list[GameResult]:
    return [
        run_game(
            client, decks, seed, policy, max_turns=max_turns, timeout_seconds=timeout_seconds
        )
        for seed in seeds
    ]


class DeterminismError(AssertionError):
    pass


def determinism_check(
    client: ForgeEnvClient,
    decks: list[tuple[str, str]],
    seed: int,
    max_turns: int = 0,
    timeout_seconds: int = 0,
    drive: bool = True,
) -> None:
    r1 = run_game(
        client, decks, seed, max_turns=max_turns, timeout_seconds=timeout_seconds, drive=drive
    )
    r2 = run_game(
        client, decks, seed, max_turns=max_turns, timeout_seconds=timeout_seconds, drive=drive
    )
    if r1.events != r2.events:
        for i, (a, b) in enumerate(zip(r1.events, r2.events)):
            if a != b:
                raise DeterminismError(
                    f"event streams diverge at index {i}: first={a!r} second={b!r}"
                )
        raise DeterminismError(
            f"event streams differ in length: {len(r1.events)} vs {len(r2.events)}"
        )
