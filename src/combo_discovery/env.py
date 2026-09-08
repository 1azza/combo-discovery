"""Typed synchronous client for the forge-harness gRPC service.

Mirrors the service defined in proto/forge_env.proto. All methods are blocking;
use WorkerPool for parallelism.
"""

from __future__ import annotations

import grpc

from .generated import forge_env_pb2 as pb
from .generated.forge_env_pb2_grpc import ForgeEnvStub


class ForgeEnvError(RuntimeError):
    pass


class HarnessConnectionError(ForgeEnvError):
    pass


class DecisionRejected(ForgeEnvError):
    pass


class ForgeEnvClient:
    def __init__(self, host: str = "localhost", port: int = 50051, timeout: float = 30.0):
        self._target = f"{host}:{port}"
        self._timeout = timeout
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
            if e.code() in (grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED):
                self._stub = None
                self._channel = None
                raise HarnessConnectionError(f"{e.code().name} from {self._target}") from e
            raise ForgeEnvError(f"{e.code().name}: {e.details()}") from e

    def ping(self) -> pb.Pong:
        return self._call(self._ensure_stub().Ping, pb.Empty())

    def start_game(
        self,
        decks: list[tuple[str, str]],
        seed: int,
        player_types: list["pb.PlayerType"] | None = None,
        max_turns: int = 0,
        timeout_seconds: int = 0,
    ) -> pb.StartResponse:
        if player_types is None:
            player_types = [pb.PLAYER_TYPE_REMOTE] * len(decks)
        req = pb.StartRequest(
            decks=[pb.DeckSpec(name=n, path=p) for n, p in decks],
            seed=seed,
            player_types=player_types,
            max_turns=max_turns,
            timeout_seconds=timeout_seconds,
        )
        return self._call(self._ensure_stub().StartGame, req)

    def get_decision(self) -> pb.DecisionRequest:
        return self._call(self._ensure_stub().GetDecision, pb.Empty())

    def submit_decision(self, option_id: int) -> pb.StepResult:
        return self._call(
            self._ensure_stub().SubmitDecision, pb.DecisionResponse(option_id=option_id)
        )

    def get_state(self) -> pb.FullState:
        return self._call(self._ensure_stub().GetState, pb.Empty())

    def snapshot(self) -> pb.StateToken:
        return self._call(self._ensure_stub().Snapshot, pb.Empty())

    def restore(self, token: pb.StateToken) -> None:
        self._call(self._ensure_stub().Restore, token)

    def is_game_over(self) -> pb.GameOver:
        return self._call(self._ensure_stub().IsGameOver, pb.Empty())

    def stop_game(self) -> None:
        self._call(self._ensure_stub().StopGame, pb.Empty())

    def close(self) -> None:
        if self._channel is not None:
            self._channel.close()
            self._channel = None
            self._stub = None

    def __enter__(self) -> "ForgeEnvClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
