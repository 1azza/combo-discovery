"""Typed synchronous client for the forge-harness gRPC service (protocol v2).

Mirrors the service defined in proto/forge_env.proto. All methods are blocking;
use WorkerPool for parallelism.

v2 contract notes:
  - Every per-game RPC carries an explicit game_id; start_game returns it.
  - submit_decision echoes the outstanding DecisionRequest's
    (game_id, decision_id) plus the chosen option_id.
  - Events are independently pollable via poll_events/drain_events.
  - Server errors: INVALID_ARGUMENT (unknown/stale game or decision id, invalid
    option) maps to StaleDecisionError; FAILED_PRECONDITION (wrong lifecycle
    state) maps to GameNotActiveError.
"""

from __future__ import annotations

import grpc

from .generated import forge_env_pb2 as pb
from .generated.forge_env_pb2_grpc import ForgeEnvStub

PROTOCOL_VERSION = 2


class ForgeEnvError(RuntimeError):
    """Base error for harness RPC failures. Carries the gRPC status code."""

    def __init__(self, message: str, code: grpc.StatusCode | None = None):
        super().__init__(message)
        self.code = code


class HarnessConnectionError(ForgeEnvError):
    """The harness is unreachable or the channel broke mid-call."""


class StaleDecisionError(ForgeEnvError):
    """INVALID_ARGUMENT: unknown/stale game or decision id, or invalid option_id."""


class GameNotActiveError(ForgeEnvError):
    """FAILED_PRECONDITION: wrong lifecycle state (e.g. StartGame while active,
    submit after game over)."""


class ProtocolMismatchError(ForgeEnvError):
    """Harness speaks a different protocol version than this client."""


class ForgeEnvClient:
    def __init__(
        self,
        host: str = "localhost",
        port: int = 50051,
        timeout: float = 30.0,
        decision_timeout: float = 300.0,
    ):
        self._target = f"{host}:{port}"
        self._timeout = timeout
        # GetDecision blocks server-side until the acting remote player has a
        # decision. The server guarantees wake-on-game-over, so this is a
        # safety bound on a slow engine stretch, not the expected wait time —
        # hence much longer than the general per-RPC timeout.
        self._decision_timeout = decision_timeout
        self._channel: grpc.Channel | None = None
        self._stub: ForgeEnvStub | None = None

    def _ensure_stub(self) -> ForgeEnvStub:
        if self._stub is None:
            try:
                self._channel = grpc.insecure_channel(self._target)
                grpc.channel_ready_future(self._channel).result(timeout=self._timeout)
                self._stub = ForgeEnvStub(self._channel)
            except grpc.FutureTimeoutError as e:
                self._channel = None
                raise HarnessConnectionError(f"no harness at {self._target}") from e
        return self._stub

    def _call(self, fn, request, timeout: float | None = None):
        try:
            return fn(request, timeout=timeout or self._timeout)
        except grpc.RpcError as e:
            code = e.code()
            details = e.details()
            if code in (grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED):
                self._stub = None
                self._channel = None
                raise HarnessConnectionError(f"{code.name} from {self._target}", code=code) from e
            if code == grpc.StatusCode.INVALID_ARGUMENT:
                raise StaleDecisionError(f"INVALID_ARGUMENT: {details}", code=code) from e
            if code == grpc.StatusCode.FAILED_PRECONDITION:
                raise GameNotActiveError(f"FAILED_PRECONDITION: {details}", code=code) from e
            raise ForgeEnvError(f"{code.name}: {details}", code=code) from e

    # -- health / version ---------------------------------------------------

    def ping(self) -> pb.Pong:
        """Raw ping; the Pong carries protocol_version."""
        return self._call(self._ensure_stub().Ping, pb.Empty())

    def protocol_version(self) -> int:
        """Ask the harness which protocol version it speaks."""
        return self.ping().protocol_version

    def connect(self) -> pb.Pong:
        """Ping the harness and verify the protocol version.

        Raises ProtocolMismatchError with a clear message if the harness does
        not speak protocol v2.
        """
        pong = self.ping()
        if pong.protocol_version != PROTOCOL_VERSION:
            raise ProtocolMismatchError(
                f"protocol version mismatch: harness at {self._target} reports "
                f"protocol_version={pong.protocol_version}, this client requires "
                f"{PROTOCOL_VERSION} (regenerate stubs on one side?)"
            )
        return pong

    # -- game lifecycle -----------------------------------------------------

    def start_game(
        self,
        decks: list[tuple[str, str]],
        seed: int,
        player_types: list["pb.PlayerType"] | None = None,
        max_turns: int = 0,
        timeout_seconds: int = 0,
    ) -> int:
        """Start a game and return the server-assigned game_id.

        Fails with GameNotActiveError if a game is already active on the harness.
        """
        if player_types is None:
            player_types = [pb.PLAYER_TYPE_REMOTE] * len(decks)
        req = pb.StartRequest(
            decks=[pb.DeckSpec(name=n, path=p) for n, p in decks],
            seed=seed,
            player_types=player_types,
            max_turns=max_turns,
            timeout_seconds=timeout_seconds,
        )
        resp: pb.StartResponse = self._call(self._ensure_stub().StartGame, req)
        return resp.game_id

    def get_decision(self, game_id: int) -> pb.DecisionRequest:
        """Block until the acting player of this game has a decision pending.

        Uses decision_timeout (constructor arg, default 300s) instead of the
        general 30s per-RPC timeout: the server guarantees wake-on-game-over,
        so the long bound only guards against a hung engine, not the normal
        wait for the opponent to finish acting.
        """
        return self._call(
            self._ensure_stub().GetDecision,
            pb.GameQuery(game_id=game_id),
            timeout=self._decision_timeout,
        )

    def submit_decision(self, game_id: int, decision_id: int, option_id: int) -> pb.StepResult:
        """Echo the outstanding DecisionRequest's (game_id, decision_id) plus the chosen option."""
        req = pb.DecisionSubmit(game_id=game_id, decision_id=decision_id, option_id=option_id)
        return self._call(self._ensure_stub().SubmitDecision, req)

    def get_state(self, game_id: int) -> pb.FullState:
        return self._call(self._ensure_stub().GetState, pb.GameQuery(game_id=game_id))

    def poll_events(self, game_id: int, cursor: int = 0) -> pb.EventBatch:
        """Return all buffered events with seq > cursor, in seq order."""
        req = pb.PollRequest(game_id=game_id, cursor=cursor)
        return self._call(self._ensure_stub().PollEvents, req)

    def drain_events(self, game_id: int, cursor: int = 0) -> list[pb.GameEvent]:
        """Poll until next_cursor stops advancing; return all events concatenated."""
        events: list[pb.GameEvent] = []
        while True:
            batch = self.poll_events(game_id, cursor)
            events.extend(batch.events)
            if batch.next_cursor <= cursor:
                break
            cursor = batch.next_cursor
        return events

    def snapshot(self, game_id: int) -> pb.StateToken:
        return self._call(self._ensure_stub().Snapshot, pb.GameQuery(game_id=game_id))

    def restore(self, game_id: int, token: bytes) -> None:
        req = pb.RestoreRequest(game_id=game_id, token=token)
        self._call(self._ensure_stub().Restore, req)

    def is_game_over(self, game_id: int) -> pb.GameOver:
        return self._call(self._ensure_stub().IsGameOver, pb.GameQuery(game_id=game_id))

    def stop_game(self, game_id: int) -> None:
        """Stop the game, release resources, and drop its event buffer."""
        self._call(self._ensure_stub().StopGame, pb.GameQuery(game_id=game_id))

    def close(self) -> None:
        if self._channel is not None:
            self._channel.close()
            self._channel = None
            self._stub = None

    def __enter__(self) -> "ForgeEnvClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
