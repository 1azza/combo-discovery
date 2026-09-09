import grpc
import pytest

from combo_discovery.env import HarnessConnectionError
from combo_discovery.generated import forge_env_pb2 as pb
from combo_discovery.runner import (
    MAX_DECISION_TIMEOUTS,
    DeterminismError,
    compare_event_streams,
    determinism_check,
    run_game,
)

DECKS = [("a", "/tmp/a.dck"), ("b", "/tmp/b.dck")]
REMOTE = [pb.PLAYER_TYPE_REMOTE, pb.PLAYER_TYPE_REMOTE]
GOLDFISH = [pb.PLAYER_TYPE_GOLDFISH, pb.PLAYER_TYPE_GOLDFISH]


def ev(seq, type_, turn=1, phase="Main1", player=0, card="", detail=""):
    return pb.GameEvent(
        seq=seq, type=type_, turn=turn, phase=phase, player=player, card_name=card, detail=detail
    )


class FakeClient:
    """Mimics ForgeEnvClient v2 just enough for run_game/determinism_check.

    In remote mode each submit appends one event from the scripted stream;
    the game is over once the whole stream has been produced. In goldfish
    mode the game flips to over after two IsGameOver polls.
    """

    def __init__(self, streams, goldfish=False, over_outcome=pb.OUTCOME_WIN):
        self.streams = [list(s) for s in streams]
        self.goldfish = goldfish
        self.over_outcome = over_outcome
        self.calls = 0
        self.game_id = 0
        self.collected: list[pb.GameEvent] = []
        self.submits: list[tuple[int, int, int]] = []
        self.get_decision_calls = 0
        self.stopped: int | None = None
        self._over_polls = 0

    def start_game(self, decks, seed, player_types=None, **kw):
        self.calls += 1
        self.collected = []
        self.submits = []
        self._over_polls = 0
        self.game_id = self.calls * 100 + seed
        return self.game_id

    def is_game_over(self, game_id):
        if self.goldfish:
            self._over_polls += 1
            over = self._over_polls >= 2
        else:
            over = len(self.collected) >= len(self.streams[self.calls - 1])
        return pb.GameOver(over=over, winner=0, outcome=self.over_outcome, reason="lethal")

    def get_decision(self, game_id):
        self.get_decision_calls += 1
        return pb.DecisionRequest(
            game_id=game_id,
            decision_id=len(self.collected) + 1,
            player=0,
            turn=len(self.collected) + 1,
            phase="Main1",
            options=[pb.Option(id=0, kind="pass")],
        )

    def submit_decision(self, game_id, decision_id, option_id):
        self.submits.append((game_id, decision_id, option_id))
        e = self.streams[self.calls - 1][len(self.collected)]
        self.collected.append(e)
        return pb.StepResult(events=[e])

    def drain_events(self, game_id, cursor=0):
        return list(self.collected)

    def get_state(self, game_id):
        return pb.FullState(game_id=game_id, turn=1)

    def stop_game(self, game_id):
        self.stopped = game_id


class TestRunnerV2:
    def test_run_game_collects_events_from_poll_stream(self):
        client = FakeClient([[ev(1, "TurnStarted", detail="1")]])
        result = run_game(client, DECKS, seed=3, player_types=REMOTE)
        assert result.seed == 3
        assert result.game_id == client.game_id
        assert result.n_events == 1
        assert result.events == [(1, "TurnStarted", 1, "Main1", 0, "", "1")]
        assert result.outcome == pb.OUTCOME_WIN
        assert result.reason == "lethal"
        assert client.stopped == client.game_id  # stop_game called, game_id routed

    def test_run_game_echoes_decision_id(self):
        stream = [ev(1, "A"), ev(2, "B")]
        client = FakeClient([stream])
        run_game(client, DECKS, seed=1, player_types=REMOTE)
        assert client.submits == [(client.game_id, 1, 0), (client.game_id, 2, 0)]

    def test_goldfish_game_never_calls_get_decision(self):
        client = FakeClient([[]], goldfish=True)
        result = run_game(client, DECKS, seed=5, player_types=GOLDFISH)
        assert client.get_decision_calls == 0
        assert client.submits == []
        assert result.outcome == pb.OUTCOME_WIN


class TestCompareEventStreams:
    def test_accepts_identical_streams(self):
        s = [ev(1, "A"), ev(2, "B", turn=2)]
        compare_event_streams(s, list(s))

    def test_accepts_same_events_with_different_game_ids(self):
        a = pb.GameEvent(seq=1, game_id=1, type="A")
        b = pb.GameEvent(seq=1, game_id=999, type="A")
        compare_event_streams([a], [b])

    def test_rejects_divergent_streams(self):
        a = [ev(1, "A"), ev(2, "B", detail="20->17")]
        b = [ev(1, "A"), ev(2, "B", detail="20->18")]
        with pytest.raises(DeterminismError, match="20->18"):
            compare_event_streams(a, b)

    def test_rejects_length_mismatch(self):
        with pytest.raises(DeterminismError, match="length"):
            compare_event_streams([ev(1, "A"), ev(2, "B")], [ev(1, "A")])

    def test_rejects_seq_mismatch(self):
        with pytest.raises(DeterminismError, match="index 0"):
            compare_event_streams([ev(1, "A")], [ev(2, "A")])

    def test_rejects_game_over_mismatch(self):
        compare_event_streams([], [], (pb.OUTCOME_WIN, 0, "lethal"), (pb.OUTCOME_WIN, 0, "lethal"))
        with pytest.raises(DeterminismError, match="GameOver differs"):
            compare_event_streams([], [], (pb.OUTCOME_WIN, 0, "lethal"), (pb.OUTCOME_DRAW, -1, ""))


class DeadlineFlakyClient(FakeClient):
    """get_decision raises a deadline error for the first `fail_calls` calls,
    mimicking a harness that does not wake a blocked waiter in time."""

    def __init__(self, stream, fail_calls, code=grpc.StatusCode.DEADLINE_EXCEEDED):
        super().__init__([stream])
        self.fail_calls = fail_calls
        self.code = code

    def get_decision(self, game_id):
        if self.get_decision_calls < self.fail_calls:
            self.get_decision_calls += 1
            raise HarnessConnectionError(f"{self.code.name} from harness", code=self.code)
        return super().get_decision(game_id)


class TestDecisionTimeoutRetry:
    STREAM = [ev(1, "A"), ev(2, "B")]

    def test_recovers_from_transient_decision_timeouts(self):
        client = DeadlineFlakyClient(self.STREAM, fail_calls=2)
        result = run_game(client, DECKS, seed=1, player_types=REMOTE)
        assert [e[0] for e in result.events] == [1, 2]  # full stream drained
        assert result.outcome == pb.OUTCOME_WIN

    def test_raises_after_bounded_consecutive_timeouts(self):
        client = DeadlineFlakyClient(self.STREAM, fail_calls=MAX_DECISION_TIMEOUTS)
        with pytest.raises(HarnessConnectionError, match="DEADLINE_EXCEEDED"):
            run_game(client, DECKS, seed=1, player_types=REMOTE)
        assert client.get_decision_calls == MAX_DECISION_TIMEOUTS
        assert client.stopped is None  # no stop_game on failure

    def test_non_deadline_connection_error_propagates_immediately(self):
        client = DeadlineFlakyClient(
            self.STREAM, fail_calls=1, code=grpc.StatusCode.UNAVAILABLE
        )
        with pytest.raises(HarnessConnectionError, match="UNAVAILABLE"):
            run_game(client, DECKS, seed=1, player_types=REMOTE)
        assert client.get_decision_calls == 1


class TestDeterminismCheck:
    def test_same_seed_identical_streams_pass(self):
        stream = [ev(1, "SpellResolved", card="Bolt"), ev(2, "LifeChanged", detail="20->17")]
        client = FakeClient([stream, stream])
        determinism_check(client, DECKS, seed=1)

    def test_detects_divergence(self):
        s1 = [ev(1, "SpellResolved", card="Bolt"), ev(2, "LifeChanged", detail="20->17")]
        s2 = [ev(1, "SpellResolved", card="Bolt"), ev(2, "LifeChanged", detail="20->18")]
        client = FakeClient([s1, s2])
        with pytest.raises(DeterminismError, match="20->18"):
            determinism_check(client, DECKS, seed=1)

    def test_detects_length_mismatch(self):
        client = FakeClient([[ev(1, "A"), ev(2, "B")], [ev(1, "A")]])
        with pytest.raises(DeterminismError, match="length"):
            determinism_check(client, DECKS, seed=1)

    def test_divergent_seed_must_differ(self):
        stream = [ev(1, "A")]
        other = [ev(1, "A"), ev(2, "B")]
        client = FakeClient([stream, stream, other])
        determinism_check(client, DECKS, seed=1, divergent_seed=2)

    def test_divergent_seed_identical_raises(self):
        stream = [ev(1, "A")]
        client = FakeClient([stream, stream, list(stream)])
        with pytest.raises(DeterminismError, match="identical"):
            determinism_check(client, DECKS, seed=1, divergent_seed=2)
