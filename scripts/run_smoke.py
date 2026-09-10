"""End-to-end smoke test against a live harness (protocol v6).

Checks: ping + protocol version, goldfish-vs-goldfish game via the raw client
(drain events, verify seq strictly increasing from 1, check GameOver.outcome,
stop_game), a remote-driven game over the v4 typed-decision interface (incl. the
spell-casting decisions), a determinism section (two same-seed goldfish games
must produce identical event streams, excluding game_id; a different seed must
differ), and a snapshot/restore replay determinism check.

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
    # Track every game id this run starts so failure cleanup can stop games
    # that run_game started and did not get to stop (run_game also does a
    # best-effort stop on failure, but this covers the window before it).
    started_games: list[int] = []
    _real_start_game = client.start_game
    _real_stop_game = client.stop_game

    def _tracking_start_game(*args, **kwargs) -> int:
        game_id = _real_start_game(*args, **kwargs)
        started_games.append(game_id)
        return game_id

    def _tracking_stop_game(game_id: int) -> None:
        _real_stop_game(game_id)
        if game_id in started_games:
            started_games.remove(game_id)

    client.start_game = _tracking_start_game  # type: ignore[method-assign]
    client.stop_game = _tracking_stop_game  # type: ignore[method-assign]

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
        # v6: every event type must come from the fixed normalized vocabulary.
        from combo_discovery.runner import EVENT_VOCABULARY, validate_event_stream

        seen_event_types = validate_event_stream(events)
        print(
            f"OK: {len(seen_event_types)} distinct event types, all in the v6 "
            f"vocabulary ({len(EVENT_VOCABULARY)} known): "
            f"{sorted(seen_event_types)}"
        )
        client.stop_game(active_game)
        active_game = None
        print("OK: stop_game")

        # --- Section 2: remote-driven game (client answers decisions) --------
        from combo_discovery.runner import DecisionContext, default_policy, run_game

        seen_types: set[int] = set()

        def tracking_policy(ctx: DecisionContext):
            seen_types.add(ctx.decision_type)
            return default_policy(ctx)

        result = run_game(
            client,
            DECKS,
            seed=7,
            policy=tracking_policy,
            player_types=[pb.PLAYER_TYPE_REMOTE, pb.PLAYER_TYPE_GOLDFISH],
            max_turns=20,
            timeout_seconds=120,
        )
        print(
            f"OK: remote-driven game_id={result.game_id} turns={result.turns} "
            f"n_events={result.n_events} outcome={outcome_name(result.outcome)} "
            f"decisions={len(result.decision_trace)} "
            f"types={sorted(pb.DecisionType.Name(t) for t in seen_types)}"
        )
        # v3: REQUIRE at least one non-PRIORITY decision type. The goldfish_A/B
        # decks always trigger a London mulligan at minimum, so this is
        # guaranteed for any game that got past mulligan; DECLARE_ATTACKERS is
        # also expected but deck-dependent, so it is not asserted here.
        known = set(pb.DecisionType.values())
        assert seen_types, "no decisions observed in remote-driven game"
        assert seen_types <= known, f"unknown decision types: {seen_types - known}"
        non_priority = seen_types - {pb.DECISION_TYPE_PRIORITY}
        assert non_priority, (
            "expected at least one non-PRIORITY decision (mulligan at minimum), "
            f"only saw {[pb.DecisionType.Name(t) for t in seen_types]}"
        )
        print(
            "OK: non-PRIORITY decision types observed: "
            f"{sorted(pb.DecisionType.Name(t) for t in non_priority)}"
        )
        # v4: the deck fixtures now include a targeted spell (Giant Growth) and
        # a modal card (Return to Nature), so the spell-casting path must
        # surface CHOOSE_TARGETS and CHOOSE_MODE. This is a real requirement,
        # not a tautology: if the remote policy never reaches those callbacks
        # the smoke fails.
        assert pb.DECISION_TYPE_CHOOSE_TARGETS in seen_types, (
            "CHOOSE_TARGETS never surfaced although the fixture decks include a "
            f"targeted spell; saw {sorted(pb.DecisionType.Name(t) for t in seen_types)}"
        )
        assert pb.DECISION_TYPE_CHOOSE_MODE in seen_types, (
            "CHOOSE_MODE never surfaced although the fixture decks include a modal "
            f"card; saw {sorted(pb.DecisionType.Name(t) for t in seen_types)}"
        )
        print("OK: v4 spell-casting decisions observed "
              "(CHOOSE_TARGETS, CHOOSE_MODE)")

        # --- Section 3: determinism ------------------------------------------
        determinism_check(
            client, DECKS, seed=123, player_types=GOLDFISH, max_turns=20,
            timeout_seconds=120, divergent_seed=124,
        )
        print("OK: determinism — same-seed streams identical (excl. game_id), "
              "different seed diverges")

        # --- Section 4: snapshot/restore replay (v5) -------------------------
        from combo_discovery.runner import snapshot_replay_check

        snapshot_replay_check(
            client,
            DECKS,
            seed=321,
            player_types=[pb.PLAYER_TYPE_REMOTE, pb.PLAYER_TYPE_GOLDFISH],
            max_turns=20,
            timeout_seconds=120,
        )
        print("OK: snapshot/restore replay — post-restore event suffix identical "
              "(MCTS determinism guarantee)")
    except Exception as e:
        print(f"FAIL: {type(e).__name__}: {e}")
        # Never leave a game active on the single-game harness, including a
        # remote game run_game started before failing.
        for gid in list(started_games):
            try:
                client.stop_game(gid)
            except Exception:
                pass
        return 1
    finally:
        client.close()
    print("SMOKE PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
