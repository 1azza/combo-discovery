from unittest.mock import MagicMock

import pytest

from combo_discovery.env import ForgeEnvClient, ForgeEnvError, HarnessConnectionError
from combo_discovery.generated import forge_env_pb2 as pb


def make_client(stub_factory=MagicMock):
    client = ForgeEnvClient(port=59999)
    stub = stub_factory()
    client._stub = stub
    return client, stub


class TestEnvClient:
    def test_context_manager_closes_channel(self):
        client = ForgeEnvClient()
        channel = MagicMock()
        client._channel = channel
        with client as c:
            assert c is client
        channel.close.assert_called_once()

    def test_ping_returns_pong(self):
        client, stub = make_client()
        stub.Ping.return_value = pb.Pong(version="0.1.0", game_active=False)
        pong = client.ping()
        assert pong.version == "0.1.0"

    def test_connection_error_resets_stub(self):
        client, stub = make_client()
        error = MagicMock(spec=grpc.RpcError) if False else _rpc_error("UNAVAILABLE")
        stub.Ping.side_effect = error
        with pytest.raises(HarnessConnectionError):
            client.ping()
        assert client._stub is None

    def test_other_rpc_error_wrapped(self):
        client, stub = make_client()
        stub.Ping.side_effect = _rpc_error("INVALID_ARGUMENT", details="bad")
        with pytest.raises(ForgeEnvError, match="bad"):
            client.ping()

    def test_start_game_builds_request(self):
        client, stub = make_client()
        stub.StartGame.return_value = pb.StartResponse(game_id=7, starting_life=[20, 20])
        resp = client.start_game([("deckA", "/tmp/a.dck"), ("deckB", "/tmp/b.dck")], seed=42)
        assert resp.game_id == 7
        req = stub.StartGame.call_args[0][0]
        assert req.seed == 42
        assert len(req.decks) == 2
        assert req.player_types == [pb.PLAYER_TYPE_REMOTE, pb.PLAYER_TYPE_REMOTE]


def _rpc_error(code_name: str, details: str = ""):
    err = grpc.RpcError()
    status = grpc.StatusCode[code_name]
    err.code = lambda: status
    err.details = lambda: details
    return err


import grpc  # noqa: E402
