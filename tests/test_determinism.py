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
    InvalidRequestError,
    StaleDecisionError,
)
from combo_discovery.generated import forge_env_pb2 as pb
from combo_discovery.runner import (
    MAX_DECISION_TIMEOUTS,
    MAX_STALE_RETRIES,
    SNAPSHOT_RESTORED_TYPE,
    DecisionContext,
    DeterminismError,
    compare_event_streams,
    default_policy,
    determinism_check,
    run_game,
    snapshot_replay_check,
)

DECKS = [("a", "/tmp/a.dck"), ("b", "/tmp/b.dck")]
REMOTE = [pb.PLAYER_TYPE_REMOTE, pb.PLAYER_TYPE_REMOTE]
GOLDFISH = [pb.PLAYER_TYPE_GOLDFISH, pb.PLAYER_TYPE_GOLDFISH]


def _rpc_error(code_name: str, details: str = ""):
    err = grpc.RpcError()
    status = grpc.StatusCode[code_name]
    err.code = lambda: status
    err.details = lambda: details
    return err


def ev(seq, type_, turn=1, phase="Main1", player=0, card="", detail=""):
    return pb.GameEvent(
        seq=seq, type=type_, turn=turn, phase=phase, player=player, card_name=card, detail=detail
    )


def _decision(dtype: int, decision_id: int = 1, **payload) -> pb.DecisionRequest:
    """A synthetic v3 DecisionRequest of the given type with payload fields."""
    return pb.DecisionRequest(
        game_id=1, decision_id=decision_id, player=0, turn=1, phase="Main1",
        decision_type=dtype, **payload
    )


def _candidates(*ids):
    return [pb.CardCandidate(card_id=i, name=f"Card{i}") for i in ids]


class FakeClient:
    """Mimics ForgeEnvClient v5 just enough for run_game/determinism_check and
    snapshot_replay_check.

    In remote mode each submit appends one event from the scripted stream;
    the game is over once the whole stream has been produced. In goldfish
    mode the game flips to over after two IsGameOver polls. decision_script
    (if given) cycles the DecisionType of successive requests; the v4
    spell-casting types get synthetic payloads so default_policy can answer.

    v5: the fake models the server's snapshot/restore contract — the event
    buffer is the "state", snapshot requires an outstanding decision, restore
    truncates the buffer back to the snapshot point, appends a
    SnapshotRestored event (renumbering seqs monotonically) and re-parks the
    decision; unknown/foreign tokens are INVALID_ARGUMENT.
    """

    def __init__(self, streams, goldfish=False, over_outcome=pb.OUTCOME_WIN,
                 decision_script=None):
        self.streams = [list(s) for s in streams]
        self.goldfish = goldfish
        self.over_outcome = over_outcome
        self.decision_script = decision_script
        self.calls = 0
        self.game_id = 0
        self.collected: list[pb.GameEvent] = []
        self.submits: list[tuple[int, int, object]] = []
        self.get_decision_calls = 0
        self.stopped: int | None = None
        self._over_polls = 0
        self.outstanding: pb.DecisionRequest | None = None
        # v5: number of submit-produced events (buffer length excluding
        # SnapshotRestored markers) and the next seq to assign.
        self.answered = 0
        self._next_seq = 1
        self.snapshots: dict[bytes, tuple[int, int]] = {}
        self._snapshot_counter = 0

    def start_game(self, decks, seed, player_types=None, **kw):
        self.calls += 1
        self.collected = []
        self.submits = []
        self._over_polls = 0
        self.outstanding = None
        self.answered = 0
        self._next_seq = 1
        self.snapshots = {}
        self._snapshot_counter = 0
        self.game_id = self.calls * 100 + seed
        return self.game_id

    def _stream(self):
        return self.streams[self.calls - 1]

    def _state_hash(self) -> str:
        # Cache-key stand-in: deterministic function of the "state" (the number
        # of submit-produced events).
        return f"h{self.answered}"

    def _make_decision(self, game_id, n):
        dtype = (
            self.decision_script[(n - 1) % len(self.decision_script)]
            if self.decision_script else pb.DECISION_TYPE_PRIORITY
        )
        req = pb.DecisionRequest(
            game_id=game_id,
            decision_id=n,
            player=0,
            turn=n,
            phase="Main1",
            decision_type=dtype,
        )
        if dtype == pb.DECISION_TYPE_PRIORITY:
            req.options.add(id=0, kind="pass")
        elif dtype == pb.DECISION_TYPE_CHOOSE_TARGETS:
            req.spell_description = "Giant Growth: target creature gets +3/+3"
            req.candidates.add(card_id=101, name="Grizzly Bears")
            req.candidates.add(card_id=102, name="Forest")
            req.defender_players.extend([1])
            req.min_choices = 1
            req.max_choices = 2
            req.mandatory = True
        elif dtype == pb.DECISION_TYPE_CHOOSE_MODE:
            req.spell_description = "Return to Nature"
            req.mode_options.add(id=0, description="Destroy target artifact")
            req.mode_options.add(id=1, description="Destroy target enchantment")
            req.min_choices = 1
            req.max_choices = 1
            req.allow_repeat = False
        elif dtype == pb.DECISION_TYPE_OPTIONAL_COSTS:
            req.spell_description = "Kicker"
            req.mode_options.add(id=0, description="Kicker {2}")
            req.min_choices = 0
            req.max_choices = 1
        return req

    def is_game_over(self, game_id):
        if self.goldfish:
            self._over_polls += 1
            over = self._over_polls >= 2
        else:
            over = self.answered >= len(self._stream())
        return pb.GameOver(over=over, winner=0, outcome=self.over_outcome, reason="lethal")

    def get_decision(self, game_id):
        self.get_decision_calls += 1
        if self.outstanding is None:
            self.outstanding = self._make_decision(game_id, self.answered + 1)
        return self.outstanding

    def submit_decision(self, game_id, decision_id, answer):
        self.submits.append((game_id, decision_id, answer))
        src = self._stream()[self.answered]
        e = pb.GameEvent()
        e.CopyFrom(src)
        e.seq = self._next_seq
        self.collected.append(e)
        self.answered += 1
        self._next_seq += 1
        over = self.answered >= len(self._stream())
        self.outstanding = None if over else self._make_decision(game_id, self.answered + 1)
        return pb.StepResult(events=[e])

    def poll_events(self, game_id, cursor=0):
        events = [e for e in self.collected if e.seq > cursor]
        next_cursor = events[-1].seq if events else cursor
        return pb.EventBatch(
            events=events, next_cursor=next_cursor,
            game_over=self.answered >= len(self._stream()),
        )

    def drain_events(self, game_id, cursor=0):
        events: list[pb.GameEvent] = []
        while True:
            batch = self.poll_events(game_id, cursor)
            events.extend(batch.events)
            if batch.next_cursor <= cursor:
                break
            cursor = batch.next_cursor
        return events

    def get_state(self, game_id):
        return pb.FullState(
            game_id=game_id, turn=self.answered + 1, state_hash=self._state_hash()
        )

    # -- v5 snapshot/restore ------------------------------------------------

    def snapshot(self, game_id):
        if self.outstanding is None:
            raise GameNotActiveError(
                "FAILED_PRECONDITION: snapshot requires an outstanding decision"
            )
        self._snapshot_counter += 1
        token = f"tok-{game_id}-{self._snapshot_counter}-{self.answered}".encode()
        self.snapshots[token] = (game_id, self.answered)
        return (token, self._state_hash())

    def restore(self, game_id, token):
        if self.outstanding is None:
            raise GameNotActiveError(
                "FAILED_PRECONDITION: restore requires an outstanding decision"
            )
        entry = self.snapshots.get(token)
        if entry is None or entry[0] != game_id:
            raise InvalidRequestError("INVALID_ARGUMENT: unknown or foreign snapshot token")
        _, length = entry
        self.collected = self.collected[:length]
        self.answered = length
        self._next_seq = length + 1
        marker = pb.GameEvent(seq=self._next_seq, type=SNAPSHOT_RESTORED_TYPE, detail="restored")
        self.collected.append(marker)
        self._next_seq += 1
        # Restore re-parks the decision at the snapshot point (client refetch).
        self.outstanding = self._make_decision(game_id, length + 1)

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
        assert client.submits == [
            (client.game_id, 1, ("option_id", 0)),
            (client.game_id, 2, ("option_id", 0)),
        ]

    def test_decision_trace_recorded(self):
        stream = [ev(1, "A"), ev(2, "B")]
        client = FakeClient(
            [stream],
            decision_script=[pb.DECISION_TYPE_PRIORITY, pb.DECISION_TYPE_MULLIGAN_KEEP],
        )
        result = run_game(client, DECKS, seed=1, player_types=REMOTE)
        assert result.decision_trace == [
            (1, pb.DECISION_TYPE_PRIORITY, ("option_id", 0)),
            (2, pb.DECISION_TYPE_MULLIGAN_KEEP, ("boolean_answer", True)),
        ]

    def test_goldfish_game_never_calls_get_decision(self):
        client = FakeClient([[]], goldfish=True)
        result = run_game(client, DECKS, seed=5, player_types=GOLDFISH)
        assert client.get_decision_calls == 0
        assert client.submits == []
        assert result.outcome == pb.OUTCOME_WIN
        assert result.decision_trace == []


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

    def __init__(
        self,
        stream,
        fail_calls,
        code=grpc.StatusCode.DEADLINE_EXCEEDED,
        error_cls: "type[ForgeEnvError]" = HarnessTimeoutError,
    ):
        super().__init__([stream])
        self.fail_calls = fail_calls
        self.code = code
        self.error_cls = error_cls

    def get_decision(self, game_id):
        if self.get_decision_calls < self.fail_calls:
            self.get_decision_calls += 1
            raise self.error_cls(f"{self.code.name} from harness", code=self.code)
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
        with pytest.raises(HarnessTimeoutError, match="DEADLINE_EXCEEDED"):
            run_game(client, DECKS, seed=1, player_types=REMOTE)
        assert client.get_decision_calls == MAX_DECISION_TIMEOUTS
        # P3: failure cleanup best-effort stops the started game.
        assert client.stopped == client.game_id

    def test_non_deadline_connection_error_propagates_immediately(self):
        client = DeadlineFlakyClient(
            self.STREAM, fail_calls=1, code=grpc.StatusCode.UNAVAILABLE,
            error_cls=HarnessConnectionError,
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


class TestDefaultPolicy:
    """default_policy returns a legal answer for a synthetic request per type."""

    def _ctx(self, dtype, **payload):
        return DecisionContext(request=_decision(dtype, **payload))

    def test_priority_highest_option_id(self):
        req = _decision(
            pb.DECISION_TYPE_PRIORITY,
            options=[pb.Option(id=1, kind="pass"), pb.Option(id=3, kind="play_card")],
        )
        assert default_policy(DecisionContext(req)) == ("option_id", 3)
        # ids, not positions: highest id wins even if listed first
        req2 = _decision(pb.DECISION_TYPE_PRIORITY, options=[pb.Option(id=9, kind="pass")])
        assert default_policy(DecisionContext(req2)) == ("option_id", 9)

    def test_mulligan_keep_true(self):
        answer = default_policy(self._ctx(pb.DECISION_TYPE_MULLIGAN_KEEP))
        assert answer == ("boolean_answer", True)

    def test_mulligan_tuck_last_cards_in_order(self):
        ctx = self._ctx(
            pb.DECISION_TYPE_MULLIGAN_TUCK,
            candidates=_candidates(10, 11, 12, 13),
            cards_to_return=2,
        )
        assert default_policy(ctx) == ("card_ids", [12, 13])

    def test_declare_attackers_all_vs_first_defender(self):
        ctx = self._ctx(
            pb.DECISION_TYPE_DECLARE_ATTACKERS,
            candidates=_candidates(20, 21),
            defender_players=[1, 0],
        )
        assert default_policy(ctx) == ("attackers", [(20, 1), (21, 1)])

    def test_declare_blockers_nothing(self):
        ctx = self._ctx(
            pb.DECISION_TYPE_DECLARE_BLOCKERS,
            candidates=_candidates(30, 31),
            attacker_cards=[20],
        )
        assert default_policy(ctx) == ("blockers", [])

    def test_assign_combat_damage_to_first_blocker(self):
        # Blocked attacker: all damage must go to the first blocker. Sending it
        # to the defending player would be rule-invalid for a non-trampler.
        ctx = self._ctx(
            pb.DECISION_TYPE_ASSIGN_COMBAT_DAMAGE,
            candidates=_candidates(30, 31),
            defender_players=[1],
            damage_amount=4,
            damage_source_card=20,
        )
        answer = default_policy(ctx)
        arm, payload = answer
        assert arm == "damage"
        assert [item.as_tuple() if isinstance(item, DamageTarget) else item for item in payload] == [
            (30, None, 4)
        ]

    def test_assign_combat_damage_to_player_when_unblocked(self):
        # No blockers: route damage to the defending player.
        ctx = self._ctx(
            pb.DECISION_TYPE_ASSIGN_COMBAT_DAMAGE,
            candidates=[],
            defender_players=[1],
            damage_amount=4,
            damage_source_card=20,
        )
        answer = default_policy(ctx)
        arm, payload = answer
        assert arm == "damage"
        assert [item.as_tuple() if isinstance(item, DamageTarget) else item for item in payload] == [
            (None, 1, 4)
        ]

    def test_order_blockers_unchanged(self):
        ctx = self._ctx(pb.DECISION_TYPE_ORDER_BLOCKERS, candidates=_candidates(40, 41, 42))
        assert default_policy(ctx) == ("card_ids", [40, 41, 42])

    def test_choose_cards(self):
        mandatory = self._ctx(
            pb.DECISION_TYPE_CHOOSE_CARDS,
            candidates=_candidates(50, 51, 52),
            min_choices=2,
            max_choices=2,
        )
        assert default_policy(mandatory) == ("card_ids", [50, 51])
        optional = self._ctx(
            pb.DECISION_TYPE_CHOOSE_CARDS,
            candidates=_candidates(50, 51),
            min_choices=0,
            max_choices=1,
            optional=True,
        )
        assert default_policy(optional) == ("card_ids", [])

    def test_announce_max_number(self):
        ctx = self._ctx(pb.DECISION_TYPE_ANNOUNCE, min_number=0, max_number=5)
        assert default_policy(ctx) == ("number_answer", 5)

    def test_scry_keep_all_on_top(self):
        ctx = self._ctx(pb.DECISION_TYPE_SCRY_ARRANGE, candidates=_candidates(60, 61))
        assert default_policy(ctx) == ("scry", ([60, 61], []))

    # --- v4: spell-casting path -------------------------------------------

    def test_choose_targets_first_candidates(self):
        ctx = self._ctx(
            pb.DECISION_TYPE_CHOOSE_TARGETS,
            candidates=_candidates(101, 102),
            defender_players=[1],
            min_choices=1,
            max_choices=2,
            mandatory=True,
        )
        assert default_policy(ctx) == ("targets", ([101], []))

    def test_choose_targets_uses_player_slots_when_no_card_candidates(self):
        ctx = self._ctx(
            pb.DECISION_TYPE_CHOOSE_TARGETS,
            candidates=[],
            defender_players=[0, 1],
            min_choices=1,
            max_choices=1,
            mandatory=True,
        )
        assert default_policy(ctx) == ("targets", ([], [0]))

    def test_choose_targets_empty_when_not_required(self):
        ctx = self._ctx(
            pb.DECISION_TYPE_CHOOSE_TARGETS,
            candidates=_candidates(101),
            defender_players=[1],
            min_choices=0,
            max_choices=1,
            mandatory=False,
        )
        assert default_policy(ctx) == ("targets", ([], []))

    def test_choose_targets_multi(self):
        ctx = self._ctx(
            pb.DECISION_TYPE_CHOOSE_TARGETS,
            candidates=_candidates(101, 102, 103),
            defender_players=[],
            min_choices=2,
            max_choices=3,
            mandatory=True,
        )
        assert default_policy(ctx) == ("targets", ([101, 102], []))

    def test_choose_mode_first_modes(self):
        ctx = self._ctx(
            pb.DECISION_TYPE_CHOOSE_MODE,
            mode_options=[
                pb.ModeOption(id=0, description="Destroy target artifact"),
                pb.ModeOption(id=1, description="Destroy target enchantment"),
            ],
            min_choices=1,
            max_choices=1,
        )
        assert default_policy(ctx) == ("mode_selection", [0])

    def test_choose_mode_empty_when_min_zero(self):
        ctx = self._ctx(
            pb.DECISION_TYPE_CHOOSE_MODE,
            mode_options=[pb.ModeOption(id=3, description="mode")],
            min_choices=0,
            max_choices=1,
        )
        assert default_policy(ctx) == ("mode_selection", [])

    def test_optional_costs_never_paid(self):
        ctx = self._ctx(
            pb.DECISION_TYPE_OPTIONAL_COSTS,
            mode_options=[pb.ModeOption(id=0, description="Kicker {2}")],
            min_choices=0,
            max_choices=1,
        )
        assert default_policy(ctx) == ("mode_selection", [])

    def test_unspecified_type_raises(self):
        with pytest.raises(ValueError, match="no default policy"):
            default_policy(self._ctx(pb.DECISION_TYPE_UNSPECIFIED))


class TestRunnerDispatch:
    """The remote loop hands each DecisionRequest type to the policy."""

    def test_scripted_policy_receives_each_type(self):
        stream = [ev(1, "A"), ev(2, "B")]
        client = FakeClient(
            [stream],
            decision_script=[
                pb.DECISION_TYPE_MULLIGAN_KEEP,
                pb.DECISION_TYPE_DECLARE_ATTACKERS,
            ],
        )
        seen: list[int] = []

        def policy(ctx: DecisionContext):
            seen.append(ctx.decision_type)
            if ctx.decision_type == pb.DECISION_TYPE_MULLIGAN_KEEP:
                return ("boolean_answer", False)
            return ("attackers", [])

        result = run_game(client, DECKS, seed=1, policy=policy, player_types=REMOTE)
        assert seen == [pb.DECISION_TYPE_MULLIGAN_KEEP, pb.DECISION_TYPE_DECLARE_ATTACKERS]
        assert client.submits == [
            (client.game_id, 1, ("boolean_answer", False)),
            (client.game_id, 2, ("attackers", [])),
        ]
        assert [t for _, t, _ in result.decision_trace] == [
            pb.DECISION_TYPE_MULLIGAN_KEEP,
            pb.DECISION_TYPE_DECLARE_ATTACKERS,
        ]

    def test_default_policy_drives_mixed_script(self):
        stream = [ev(1, "A"), ev(2, "B"), ev(3, "C")]
        client = FakeClient(
            [stream],
            decision_script=[
                pb.DECISION_TYPE_PRIORITY,
                pb.DECISION_TYPE_SCRY_ARRANGE,
                pb.DECISION_TYPE_ANNOUNCE,
            ],
        )
        result = run_game(
            client,
            DECKS,
            seed=1,
            player_types=REMOTE,
        )
        assert [a for _, _, a in result.decision_trace] == [
            ("option_id", 0),
            ("scry", ([], [])),  # no candidates on the synthetic request
            ("number_answer", 0),  # max_number defaults to 0
        ]

    def test_default_policy_drives_v4_spellcasting_script(self):
        stream = [ev(1, "A"), ev(2, "B"), ev(3, "C"), ev(4, "D")]
        client = FakeClient(
            [stream],
            decision_script=[
                pb.DECISION_TYPE_CHOOSE_TARGETS,
                pb.DECISION_TYPE_CHOOSE_MODE,
                pb.DECISION_TYPE_OPTIONAL_COSTS,
                pb.DECISION_TYPE_PRIORITY,
            ],
        )
        result = run_game(client, DECKS, seed=1, player_types=REMOTE)
        assert [t for _, t, _ in result.decision_trace] == [
            pb.DECISION_TYPE_CHOOSE_TARGETS,
            pb.DECISION_TYPE_CHOOSE_MODE,
            pb.DECISION_TYPE_OPTIONAL_COSTS,
            pb.DECISION_TYPE_PRIORITY,
        ]
        assert [a for _, _, a in result.decision_trace] == [
            ("targets", ([101], [])),   # first min_choices card candidate
            ("mode_selection", [0]),    # first mode id
            ("mode_selection", []),     # kicker never paid
            ("option_id", 0),
        ]
        # The targets/mode submits still ride the wire correctly.
        assert client.submits[0][2] == ("targets", ([101], []))
        assert client.submits[1][2] == ("mode_selection", [0])


class TestValidationErrorPath:
    def test_wrong_answer_arm_is_invalid_request_and_decision_stays_outstanding(self):
        """Q4: a deterministic policy bug (wrong answer arm) is a malformed
        request, not a stale decision — it surfaces as InvalidRequestError and
        the outstanding decision is not consumed."""
        stub = MagicMock()
        stub.StartGame.return_value = pb.StartResponse(game_id=1)
        stub.IsGameOver.side_effect = [
            pb.GameOver(over=False),
            pb.GameOver(over=False),
            pb.GameOver(over=True),
        ]
        outstanding = pb.DecisionRequest(
            game_id=1,
            decision_id=4,
            player=0,
            turn=2,
            phase="Combat",
            decision_type=pb.DECISION_TYPE_DECLARE_ATTACKERS,
            candidates=[pb.CardCandidate(card_id=1, name="Grizzly Bears")],
            defender_players=[1],
        )
        stub.GetDecision.return_value = outstanding
        # First submit uses a wrong arm (option_id for an attackers decision):
        stub.SubmitDecision.side_effect = [
            _rpc_error("INVALID_ARGUMENT", "wrong answer kind"),
            pb.StepResult(events=[pb.GameEvent(seq=1, type="AttacksDeclared")]),
        ]

        client = ForgeEnvClient(port=59999)
        client._stub = stub
        with pytest.raises(InvalidRequestError, match="wrong answer kind"):
            client.submit_decision(1, 4, ("option_id", 0))
        # The outstanding decision is untouched: same decision_id still valid.
        assert stub.GetDecision.return_value.decision_id == 4
        # Corrected answer submits fine.
        client.submit_decision(1, 4, ("attackers", [(1, 1)]))
        assert stub.SubmitDecision.call_count == 2
        req = stub.SubmitDecision.call_args[0][0]
        assert req.WhichOneof("answer") == "attackers"
        assert req.decision_id == 4  # echoed, not a re-prompt

    def test_stale_answer_still_surfaces_stale_decision(self):
        """A genuinely stale submit (watchdog detail) stays retryable as
        StaleDecisionError."""
        stub = MagicMock()
        stub.StartGame.return_value = pb.StartResponse(game_id=1)
        stub.SubmitDecision.side_effect = [
            _rpc_error("INVALID_ARGUMENT", "stale decision 4, outstanding 5"),
            pb.StepResult(events=[pb.GameEvent(seq=1, type="TurnStarted")]),
        ]
        client = ForgeEnvClient(port=59999)
        client._stub = stub
        with pytest.raises(StaleDecisionError, match="stale decision"):
            client.submit_decision(1, 4, ("option_id", 0))


class StaleSubmitClient(FakeClient):
    """submit_decision raises StaleDecisionError (watchdog released the
    decision mid-submit) for the first `fail_submits` calls without consuming
    the outstanding decision; later submits succeed."""

    def __init__(self, stream, fail_submits):
        super().__init__([stream])
        self.fail_submits = fail_submits
        self.refetches = 0

    def submit_decision(self, game_id, decision_id, answer):
        if len(self.submits) < self.fail_submits:
            self.submits.append((game_id, decision_id, answer))
            raise StaleDecisionError(
                "INVALID_ARGUMENT: decision released by watchdog",
                code=grpc.StatusCode.INVALID_ARGUMENT,
            )
        return super().submit_decision(game_id, decision_id, answer)

    def get_decision(self, game_id):
        if self.submits and len(self.submits) <= self.fail_submits:
            self.refetches += 1  # each retry refetches the current decision
        return super().get_decision(game_id)


class TestStaleDecisionRetry:
    STREAM = [ev(1, "A"), ev(2, "B")]

    def test_recovers_from_stale_submit(self):
        client = StaleSubmitClient(self.STREAM, fail_submits=1)
        result = run_game(client, DECKS, seed=1, player_types=REMOTE)
        # Both events drained; the retried submit went through.
        assert [e[0] for e in result.events] == [1, 2]
        assert result.outcome == pb.OUTCOME_WIN
        assert client.refetches >= 1  # decision was refetched after the stale
        # Trace records only the accepted submissions.
        assert len(result.decision_trace) == 2
        assert client.stopped == client.game_id

    def test_stale_bounded_then_raises(self):
        # MAX_STALE_RETRIES is the number of retries AFTER the initial
        # rejection, i.e. up to MAX_STALE_RETRIES + 1 rejected attempts.
        client = StaleSubmitClient(self.STREAM, fail_submits=MAX_STALE_RETRIES + 1)
        with pytest.raises(StaleDecisionError, match="watchdog"):
            run_game(client, DECKS, seed=1, player_types=REMOTE)
        assert len(client.submits) == MAX_STALE_RETRIES + 1
        # Failure cleanup (P3): run_game best-effort stops the started game.
        assert client.stopped == client.game_id

    def test_success_resets_stale_counter(self):
        # One stale rejection per decision id (retry then accepted), never
        # three in a row → must complete.
        class OneStaleEachClient(FakeClient):
            def __init__(self, stream):
                super().__init__([stream])
                self.rejected: set[int] = set()

            def submit_decision(self, game_id, decision_id, answer):
                if decision_id not in self.rejected:
                    self.rejected.add(decision_id)
                    self.submits.append((game_id, decision_id, answer))
                    raise StaleDecisionError(
                        "INVALID_ARGUMENT: stale",
                        code=grpc.StatusCode.INVALID_ARGUMENT,
                    )
                return super().submit_decision(game_id, decision_id, answer)

        client = OneStaleEachClient(self.STREAM)
        result = run_game(client, DECKS, seed=1, player_types=REMOTE)
        assert [e[0] for e in result.events] == [1, 2]
        assert len(result.decision_trace) == 2

    def test_stale_on_get_decision_retries(self):
        class StaleGetClient(FakeClient):
            def __init__(self, stream, fail_gets):
                super().__init__([stream])
                self.fail_gets = fail_gets

            def get_decision(self, game_id):
                if self.get_decision_calls <= self.fail_gets:
                    self.get_decision_calls += 1
                    raise StaleDecisionError(
                        "INVALID_ARGUMENT: stale", code=grpc.StatusCode.INVALID_ARGUMENT
                    )
                return super().get_decision(game_id)

        client = StaleGetClient(self.STREAM, fail_gets=1)
        result = run_game(client, DECKS, seed=1, player_types=REMOTE)
        assert [e[0] for e in result.events] == [1, 2]


class TestRunGameCleanupAndPollRetry:
    """P3: transient poll timeouts are retried; any failure stops the game."""

    STREAM = [ev(1, "A")]

    def test_transient_poll_timeout_retried(self):
        class FlakyPollClient(FakeClient):
            def __init__(self, stream):
                super().__init__([stream])
                self.poll_failures = 2

            def is_game_over(self, game_id):
                if self.poll_failures > 0:
                    self.poll_failures -= 1
                    raise HarnessTimeoutError(
                        "DEADLINE_EXCEEDED", code=grpc.StatusCode.DEADLINE_EXCEEDED
                    )
                return super().is_game_over(game_id)

        client = FlakyPollClient(self.STREAM)
        result = run_game(client, DECKS, seed=1, player_types=REMOTE)
        assert result.n_events == 1

    def test_failure_cleanup_stops_started_game(self):
        class AlwaysTimeoutClient(FakeClient):
            def is_game_over(self, game_id):
                raise HarnessTimeoutError(
                    "DEADLINE_EXCEEDED", code=grpc.StatusCode.DEADLINE_EXCEEDED
                )

        client = AlwaysTimeoutClient([self.STREAM])
        with pytest.raises(HarnessTimeoutError):
            run_game(client, DECKS, seed=1, player_types=REMOTE)
        assert client.stopped == client.game_id  # best-effort stop on failure

    def test_cleanup_swallows_stop_errors(self):
        class BadStopClient(FakeClient):
            def is_game_over(self, game_id):
                raise HarnessTimeoutError(
                    "DEADLINE_EXCEEDED", code=grpc.StatusCode.DEADLINE_EXCEEDED
                )

            def stop_game(self, game_id):
                raise HarnessConnectionError("UNAVAILABLE during cleanup")

        client = BadStopClient([self.STREAM])
        # Cleanup failure must not mask the original error.
        with pytest.raises(HarnessTimeoutError):
            run_game(client, DECKS, seed=1, player_types=REMOTE)


class TestFakeSnapshotSemantics:
    """The v5 fake models the server snapshot/restore contract."""

    def test_snapshot_requires_outstanding_decision(self):
        c = FakeClient([[ev(1, "A")]])
        gid = c.start_game(DECKS, 1)
        with pytest.raises(GameNotActiveError, match="outstanding decision"):
            c.snapshot(gid)

    def test_restore_foreign_token_is_invalid_request(self):
        c = FakeClient([[ev(1, "A"), ev(2, "B")]])
        gid = c.start_game(DECKS, 1)
        c.get_decision(gid)  # outstanding decision
        c.snapshot(gid)
        c.submit_decision(gid, 1, ("option_id", 0))
        with pytest.raises(InvalidRequestError, match="foreign snapshot token"):
            c.restore(gid, b"bogus-token")

    def test_restore_truncates_appends_marker_and_repark(self):
        c = FakeClient([[ev(1, "A"), ev(2, "B"), ev(3, "C")]])
        gid = c.start_game(DECKS, 1)
        req = c.get_decision(gid)
        token, h1 = c.snapshot(gid)
        c.submit_decision(gid, req.decision_id, ("option_id", 0))
        assert c.answered == 1
        c.restore(gid, token)
        assert c.answered == 0
        assert c.collected[-1].type == SNAPSHOT_RESTORED_TYPE
        # Restore re-parks the decision at the snapshot point.
        refetched = c.get_decision(gid)
        assert refetched.decision_id == req.decision_id
        assert c.get_state(gid).state_hash == h1


class TestSnapshotReplayCheck:
    STREAM = [ev(1, "TurnStarted"), ev(2, "SpellCast"), ev(3, "SpellResolved")]

    def test_identical_replay_suffixes_pass(self):
        client = FakeClient([list(self.STREAM)])
        snapshot_replay_check(client, DECKS, seed=1)
        assert client.stopped == client.game_id

    def test_branch_action_override_used(self):
        client = FakeClient([list(self.STREAM)])
        seen: list[object] = []

        def branch(ctx):
            seen.append(ctx.decision_type)
            return ("option_id", 0)

        snapshot_replay_check(client, DECKS, seed=1, branch_action=branch)
        assert seen == [pb.DECISION_TYPE_PRIORITY]

    def test_divergent_replay_fails_with_clear_message(self):
        class DivergentReplayClient(FakeClient):
            """Simulates a non-deterministic engine: the replay submit's event
            differs from the forward run's."""

            def __init__(self, stream):
                super().__init__([stream])
                self.replaying = False

            def restore(self, game_id, token):
                super().restore(game_id, token)
                self.replaying = True

            def submit_decision(self, game_id, decision_id, answer):
                step = super().submit_decision(game_id, decision_id, answer)
                if self.replaying:
                    # Mutate the buffered event (StepResult copies its input).
                    self.collected[-1].detail = "diverged"
                return step

        client = DivergentReplayClient(list(self.STREAM))
        with pytest.raises(DeterminismError, match="diverges at relative index 0"):
            snapshot_replay_check(client, DECKS, seed=1)

    def test_hash_mismatch_across_restore_raises(self):
        class SkipTruncate(FakeClient):
            def restore(self, game_id, token):
                if self.outstanding is None:
                    raise GameNotActiveError("no outstanding decision")
                # Do not truncate; re-park at a LATER decision so the hash differs.
                self.answered += 1
                self.outstanding = self._make_decision(game_id, self.answered + 1)

        client = SkipTruncate([list(self.STREAM)])
        with pytest.raises(DeterminismError, match="state_hash changed across restore"):
            snapshot_replay_check(client, DECKS, seed=1)

    def test_game_over_before_first_priority_raises(self):
        client = FakeClient([[]])  # game is instantly over
        with pytest.raises(DeterminismError, match="before the first PRIORITY"):
            snapshot_replay_check(client, DECKS, seed=1)

    def test_game_ending_at_branch_raises(self):
        # A one-event stream: the branch submit ends the game, so there is no
        # outstanding decision to restore.
        client = FakeClient([[ev(1, "GameOver")]])
        with pytest.raises(DeterminismError, match="ended at the branch decision"):
            snapshot_replay_check(client, DECKS, seed=1)
