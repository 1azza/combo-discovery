"""End-to-end smoke test against a live harness (protocol v2).

Checks: ping + protocol version, goldfish-vs-goldfish game via the raw client
(drain events, verify seq strictly increasing from 1, check GameOver.outcome,
stop_game), a remote-driven game, and a determinism section (two same-seed
goldfish games must produce identical event streams, excluding game_id; a
different seed must differ).

Requires a running harness on localhost:50051 (see the Java harness lane).
"""

import os
import sys
import time

from combo_discovery.env import ForgeEnvClient, ForgeEnvError, HarnessConnectionError
from combo_discovery.generated import forge_env_pb2 as pb
from combo_discovery.runner import determinism_check

HOST = os.environ.get("FORGE_HOST", "localhost")
PORT = int(os.environ.get("FORGE_PORT", "50051"))

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Deck paths must be absolute: the harness resolves them against its own
# working directory, not the client's.
DECKS = [
    ("goldfish_A", os.path.join(_REPO_ROOT, "decks", "goldfish_A.dck")),
    ("goldfish_B", os.path.join(_REPO_ROOT, "decks", "goldfish_B.dck")),
]
GOLDFISH = [pb.PLAYER_TYPE_GOLDFISH, pb.PLAYER_TYPE_GOLDFISH]


def wait_for_game_over(client: ForgeEnvClient, game_id: int, deadline_s: float = 120.0) -> pb.GameOver:
    deadline = time.monotonic() + deadline_s
    while True:
        over = client.is_game_over(game_id)
        if over.over:
            return over
        if time.monotonic() > deadline:
            raise TimeoutError(f"game {game_id} did not finish within {deadline_s}s")
        time.sleep(0.2)


def verify_seq(events: list[pb.GameEvent]) -> None:
    if not events:
        raise AssertionError("no events drained")
    if events[0].seq != 1:
        raise AssertionError(f"first event seq is {events[0].seq}, expected 1")
    for prev, cur in zip(events, events[1:]):
        if cur.seq <= prev.seq:
            raise AssertionError(f"seq not strictly increasing: {prev.seq} -> {cur.seq}")


def outcome_name(value: int) -> str:
    return pb.Outcome.Name(value) if value in pb.Outcome.values() else str(value)


def main() -> int:
    client = ForgeEnvClient(host=HOST, port=PORT)
    try:
        pong = client.connect()
    except HarnessConnectionError as e:
        print(f"FAIL: {e}")
        return 1
    except ForgeEnvError as e:
        print(f"FAIL: {e}")
        return 1
    print(
        f"OK: harness version={pong.version} game_active={pong.game_active} "
        f"protocol_version={pong.protocol_version}"
    )

    active_game: int | None = None
    try:
        # Bounded by max_turns: goldfish decks are not guaranteed to finish
        # naturally, so the expected result is a turn-limit draw.
        active_game = client.start_game(
            DECKS, seed=42, player_types=GOLDFISH, max_turns=20, timeout_seconds=120
        )
        print(f"OK: started goldfish game_id={active_game}")
        over = wait_for_game_over(client, active_game)
        if over.outcome != pb.OUTCOME_TURN_LIMIT or over.winner != -1:
            raise AssertionError(
                f"expected OUTCOME_TURN_LIMIT with winner=-1, got outcome={outcome_name(over.outcome)} "
                f"winner={over.winner} reason={over.reason!r}"
            )
        print(
            f"OK: game over winner={over.winner} outcome={outcome_name(over.outcome)} "
            f"reason={over.reason!r}"
        )
        events = client.drain_events(active_game)
        verify_seq(events)
        print(f"OK: drained {len(events)} events, seq strictly increasing from 1")
        if events[-1].type != "GameOver":
            raise AssertionError(f"last event type is {events[-1].type!r}, expected GameOver")
        client.stop_game(active_game)
        active_game = None
        print("OK: stop_game")

        # --- Section 2: remote-driven game (client answers decisions) --------
        from combo_discovery.runner import run_game

        result = run_game(
            client,
            DECKS,
            seed=7,
            player_types=[pb.PLAYER_TYPE_REMOTE, pb.PLAYER_TYPE_GOLDFISH],
            max_turns=20,
            timeout_seconds=120,
        )
        print(
            f"OK: remote-driven game_id={result.game_id} turns={result.turns} "
            f"n_events={result.n_events} outcome={outcome_name(result.outcome)}"
        )

        # --- Section 3: determinism ------------------------------------------
        determinism_check(
            client, DECKS, seed=123, player_types=GOLDFISH, max_turns=20,
            timeout_seconds=120, divergent_seed=124,
        )
        print("OK: determinism — same-seed streams identical (excl. game_id), "
              "different seed diverges")
    except Exception as e:
        print(f"FAIL: {type(e).__name__}: {e}")
        # Never leave a game active on the single-game harness.
        try:
            if active_game is not None:
                client.stop_game(active_game)
        except Exception:
            pass
        return 1
    finally:
        client.close()
    print("SMOKE PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
