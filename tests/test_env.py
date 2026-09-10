import grpc
import pytest
from unittest.mock import MagicMock

from combo_discovery.env import (
    DamageTarget,
    ForgeEnvClient,
    ForgeEnvError,
    GameNotActiveError,
    HarnessConnectionError,
    HarnessTimeoutError,
    ProtocolMismatchError,
    StaleDecisionError,
    build_submit,
)
from combo_discovery.generated import forge_env_pb2 as pb


def make_client(stub_factory=MagicMock):
    client = ForgeEnvClient(port=59999)
    stub = stub_factory()
    client._stub = stub
    return client, stub


def _rpc_error(code_name: str, details: str = ""):
    err = grpc.RpcError()
    status = grpc.StatusCode[code_name]
    err.code = lambda: status
    err.details = lambda: details
    return err


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
        stub.Ping.return_value = pb.Pong(version="0.3.0", game_active=False, protocol_version=3)
        pong = client.ping()
        assert pong.version == "0.3.0"
        assert pong.protocol_version == 3

    def test_connection_error_resets_stub(self):
        client, stub = make_client()
        stub.Ping.side_effect = _rpc_error("UNAVAILABLE")
        with pytest.raises(HarnessConnectionError):
            client.ping()
        assert client._stub is None

    def test_other_rpc_error_wrapped(self):
        client, stub = make_client()
        stub.Ping.side_effect = _rpc_error("INVALID_ARGUMENT", details="bad")
        with pytest.raises(ForgeEnvError, match="bad"):
            client.ping()

    def test_invalid_argument_maps_to_stale_decision(self):
        client, stub = make_client()
        stub.GetDecision.side_effect = _rpc_error("INVALID_ARGUMENT", details="unknown game 5")
        with pytest.raises(StaleDecisionError, match="unknown game 5") as excinfo:
            client.get_decision(5)
        assert excinfo.value.code == grpc.StatusCode.INVALID_ARGUMENT

    def test_failed_precondition_maps_to_game_not_active(self):
        client, stub = make_client()
        stub.SubmitDecision.side_effect = _rpc_error("FAILED_PRECONDITION", details="game over")
        with pytest.raises(GameNotActiveError, match="game over") as excinfo:
            client.submit_decision(5, 3, ("option_id", 1))
        assert excinfo.value.code == grpc.StatusCode.FAILED_PRECONDITION

    def test_connect_accepts_protocol_v3(self):
        client, stub = make_client()
        stub.Ping.return_value = pb.Pong(version="0.3.0", protocol_version=3)
        pong = client.connect()
        assert pong.protocol_version == 3

    def test_connect_rejects_protocol_mismatch(self):
        client, stub = make_client()
        stub.Ping.return_value = pb.Pong(version="0.2.0", protocol_version=2)
        with pytest.raises(ProtocolMismatchError, match="protocol version mismatch"):
            client.connect()
        with pytest.raises(ProtocolMismatchError, match="protocol_version=2"):
            client.connect()

    def test_protocol_version_exposed(self):
        client, stub = make_client()
        stub.Ping.return_value = pb.Pong(protocol_version=3)
        assert client.protocol_version() == 3

    def test_start_game_returns_game_id(self):
        client, stub = make_client()
        stub.StartGame.return_value = pb.StartResponse(game_id=7, starting_life=[20, 20])
        game_id = client.start_game(
            [("deckA", "/tmp/a.dck"), ("deckB", "/tmp/b.dck")], seed=42, max_turns=100
        )
        assert game_id == 7
        req = stub.StartGame.call_args[0][0]
        assert req.seed == 42
        assert req.max_turns == 100
        assert len(req.decks) == 2
        assert req.player_types == [pb.PLAYER_TYPE_REMOTE, pb.PLAYER_TYPE_REMOTE]

    def test_start_game_with_player_types(self):
        client, stub = make_client()
        stub.StartGame.return_value = pb.StartResponse(game_id=1)
        client.start_game(
            [("a", "/a.dck"), ("b", "/b.dck")],
            seed=1,
            player_types=[pb.PLAYER_TYPE_GOLDFISH, pb.PLAYER_TYPE_FORGE_AI],
        )
        req = stub.StartGame.call_args[0][0]
        assert req.player_types == [pb.PLAYER_TYPE_GOLDFISH, pb.PLAYER_TYPE_FORGE_AI]

    def test_per_game_rpcs_route_game_id(self):
        client, stub = make_client()
        stub.GetDecision.return_value = pb.DecisionRequest(game_id=9, decision_id=1)
        stub.GetState.return_value = pb.FullState(game_id=9)
        stub.Snapshot.return_value = pb.StateToken(token=b"tok")
        stub.IsGameOver.return_value = pb.GameOver(over=True, outcome=pb.OUTCOME_WIN)
        stub.PollEvents.return_value = pb.EventBatch(next_cursor=0)
        client.get_decision(9)
        client.get_state(9)
        client.snapshot(9)
        client.restore(9, b"tok")
        client.is_game_over(9)
        client.stop_game(9)
        client.poll_events(9, cursor=4)
        for name in ("GetDecision", "GetState", "Snapshot", "IsGameOver", "StopGame"):
            req = getattr(stub, name).call_args[0][0]
            assert req.game_id == 9, name
        assert stub.Restore.call_args[0][0].game_id == 9
        assert stub.Restore.call_args[0][0].token == b"tok"
        poll_req = stub.PollEvents.call_args[0][0]
        assert (poll_req.game_id, poll_req.cursor) == (9, 4)

    def test_get_decision_uses_decision_timeout(self):
        client, stub = make_client()
        stub.GetDecision.return_value = pb.DecisionRequest(game_id=9, decision_id=1)
        client.get_decision(9)
        assert stub.GetDecision.call_args.kwargs["timeout"] == 300.0

    def test_decision_timeout_configurable(self):
        client = ForgeEnvClient(port=59999, decision_timeout=5.0)
        stub = MagicMock()
        stub.GetDecision.return_value = pb.DecisionRequest(game_id=9, decision_id=1)
        client._stub = stub
        client.get_decision(9)
        assert stub.GetDecision.call_args.kwargs["timeout"] == 5.0

    def test_other_rpcs_use_general_timeout(self):
        client, stub = make_client()
        stub.IsGameOver.return_value = pb.GameOver(over=True)
        client.is_game_over(9)
        assert stub.IsGameOver.call_args.kwargs["timeout"] == 30.0

    def test_deadline_exceeded_is_timeout_not_connection_death(self):
        """DEADLINE_EXCEEDED must NOT be treated as server death: the channel
        stays usable and a distinct HarnessTimeoutError is raised."""
        client, stub = make_client()
        stub.Ping.side_effect = _rpc_error("DEADLINE_EXCEEDED")
        with pytest.raises(HarnessTimeoutError) as excinfo:
            client.ping()
        assert excinfo.value.code == grpc.StatusCode.DEADLINE_EXCEEDED
        assert isinstance(excinfo.value, ForgeEnvError)
        # Channel not reset: the server may still be healthy.
        assert client._stub is stub

    def test_unavailable_still_resets_stub(self):
        client, stub = make_client()
        stub.Ping.side_effect = _rpc_error("UNAVAILABLE")
        with pytest.raises(HarnessConnectionError):
            client.ping()
        assert client._stub is None

    def test_start_game_force_stop_active_default_off(self):
        client, stub = make_client()
        stub.StartGame.return_value = pb.StartResponse(game_id=1)
        client.start_game([("a", "/a.dck"), ("b", "/b.dck")], seed=1)
        req = stub.StartGame.call_args[0][0]
        assert req.force_stop_active is False

    def test_start_game_force_stop_active_passed_through(self):
        client, stub = make_client()
        stub.StartGame.return_value = pb.StartResponse(game_id=1)
        client.start_game([("a", "/a.dck"), ("b", "/b.dck")], seed=1, force_stop_active=True)
        req = stub.StartGame.call_args[0][0]
        assert req.force_stop_active is True

    def test_submit_decision_echoes_decision_id(self):
        client, stub = make_client()
        stub.SubmitDecision.return_value = pb.StepResult(game_over=True)
        client.submit_decision(9, 4, ("option_id", 2))
        req = stub.SubmitDecision.call_args[0][0]
        assert isinstance(req, pb.DecisionSubmit)
        assert req.game_id == 9
        assert req.decision_id == 4
        assert req.WhichOneof("answer") == "option_id"
        assert req.option_id == 2

    def test_drain_events_paginates_until_cursor_stops(self):
        client, stub = make_client()
        stub.PollEvents.side_effect = [
            pb.EventBatch(
                events=[
                    pb.GameEvent(seq=1, game_id=5, type="TurnStarted", detail="1"),
                    pb.GameEvent(seq=2, game_id=5, type="LifeChanged", detail="20->17"),
                ],
                next_cursor=2,
            ),
            pb.EventBatch(
                events=[pb.GameEvent(seq=3, game_id=5, type="GameOver")],
                next_cursor=3,
                game_over=True,
            ),
            # cursor stopped advancing: no new events
            pb.EventBatch(next_cursor=3, game_over=True),
        ]
        events = client.drain_events(5)
        assert [e.seq for e in events] == [1, 2, 3]
        assert stub.PollEvents.call_count == 3
        cursors = [c[0][0].cursor for c in stub.PollEvents.call_args_list]
        assert cursors == [0, 2, 3]

    def test_drain_events_terminates_on_immediate_empty(self):
        client, stub = make_client()
        stub.PollEvents.return_value = pb.EventBatch(next_cursor=0)
        events = client.drain_events(5)
        assert events == []
        assert stub.PollEvents.call_count == 1


class TestBuildSubmit:
    """Per-type answer builders produce the right oneof arm."""

    def test_option_id(self):
        req = build_submit(3, 7, ("option_id", 5))
        assert req.WhichOneof("answer") == "option_id"
        assert (req.game_id, req.decision_id, req.option_id) == (3, 7, 5)

    def test_boolean_answer(self):
        req = build_submit(3, 7, ("boolean_answer", False))
        assert req.WhichOneof("answer") == "boolean_answer"
        assert req.boolean_answer is False

    def test_card_ids(self):
        req = build_submit(3, 7, ("card_ids", [4, 2, 9]))
        assert req.WhichOneof("answer") == "card_ids"
        assert list(req.card_ids.values) == [4, 2, 9]

    def test_number_answer(self):
        req = build_submit(3, 7, ("number_answer", 3))
        assert req.WhichOneof("answer") == "number_answer"
        assert req.number_answer == 3

    def test_attackers(self):
        req = build_submit(3, 7, ("attackers", [(11, 1), (12, 0)]))
        assert req.WhichOneof("answer") == "attackers"
        assert [(a.attacker_card, a.defender_player) for a in req.attackers.values] == [
            (11, 1),
            (12, 0),
        ]

    def test_blockers(self):
        req = build_submit(3, 7, ("blockers", [(7, 11), (8, 12)]))
        assert req.WhichOneof("answer") == "blockers"
        assert [(b.blocker_card, b.attacker_card) for b in req.blockers.values] == [
            (7, 11),
            (8, 12),
        ]

    def test_damage(self):
        req = build_submit(
            3, 7, ("damage", [DamageTarget(card_id=5, amount=2), (None, 1, 3)])
        )
        assert req.WhichOneof("answer") == "damage"
        assert [(d.card_id, d.player, d.amount) for d in req.damage.values] == [
            (5, 0, 2),
            (0, 1, 3),
        ]

    def test_damage_rejects_both_or_neither_target(self):
        with pytest.raises(ValueError, match="exactly one"):
            build_submit(3, 7, ("damage", [(5, 1, 2)]))
        with pytest.raises(ValueError, match="exactly one"):
            build_submit(3, 7, ("damage", [(None, None, 2)]))
        with pytest.raises(ValueError, match="exactly one"):
            build_submit(3, 7, ("damage", [DamageTarget()]))  # neither set

    def test_scry(self):
        req = build_submit(3, 7, ("scry", ([2, 1], [3])))
        assert req.WhichOneof("answer") == "scry"
        assert (list(req.scry.top), list(req.scry.bottom)) == ([2, 1], [3])

    def test_unknown_arm_rejected(self):
        with pytest.raises(ValueError, match="unknown answer arm"):
            build_submit(3, 7, ("telepathy", 1))

    def test_prebuilt_submit_passthrough_rebinds_ids(self):
        pre = pb.DecisionSubmit(option_id=9)
        req = build_submit(5, 6, pre)
        assert (req.game_id, req.decision_id, req.option_id) == (5, 6, 9)
