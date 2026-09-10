"""Tests for the v6 real goldfish policy (GoldfishPolicy).

Covers: land-play preference, cheapest-cast preference, the mulligan ladder
and per-game reset, combat delegation, and determinism through run_game.
"""

from __future__ import annotations

from combo_discovery.generated import forge_env_pb2 as pb
from combo_discovery.goldfish import (
    KEEP_HAND_SIZE,
    GoldfishPolicy,
    _mana_cost,
)
from combo_discovery.runner import (
    DecisionContext,
    compare_event_streams,
    default_policy,
    run_game,
)

DECKS = [("a", "/tmp/a.dck"), ("b", "/tmp/b.dck")]
REMOTE = [pb.PLAYER_TYPE_REMOTE, pb.PLAYER_TYPE_REMOTE]


def _option(option_id: int, *, kind: str = "play_card", card_name: str = "",
            description: str = "") -> pb.Option:
    return pb.Option(id=option_id, kind=kind, card_name=card_name,
                     description=description)


def _priority(*options: pb.Option, decision_id: int = 1) -> DecisionContext:
    return DecisionContext(request=pb.DecisionRequest(
        game_id=1, decision_id=decision_id, player=0, turn=2, phase="Main1",
        decision_type=pb.DECISION_TYPE_PRIORITY, options=list(options),
    ))


def _mulligan(card_count: int, *, decision_id: int = 1) -> DecisionContext:
    return DecisionContext(request=pb.DecisionRequest(
        game_id=1, decision_id=decision_id, player=0, turn=1, phase="Beginning",
        decision_type=pb.DECISION_TYPE_MULLIGAN_KEEP,
        candidates=[pb.CardCandidate(card_id=i, name=f"Card{i}")
                    for i in range(card_count)],
        cards_to_return=max(0, 7 - card_count),
    ))


class TestManaCostParsing:
    def test_forge_brace_symbols(self):
        assert _mana_cost("Grizzly Bears {1}{G}") == 2
        assert _mana_cost("Giant Growth {G}") == 1
        assert _mana_cost("Wrath of God {2}{W}{W}") == 4
        assert _mana_cost("Fireball {X}{R}") == 1  # X counts 0

    def test_legacy_cost_spelling(self):
        assert _mana_cost("Cost: 5") == 5
        assert _mana_cost("cost 1 R") == 1

    def test_unparseable_is_most_expensive(self):
        assert _mana_cost("Play Forest") > 10


class TestPriorityPreference:
    def test_prefers_land_over_cheaper_spell(self):
        policy = GoldfishPolicy()
        ctx = _priority(
            _option(0, kind="play_card", card_name="Lightning Bolt",
                    description="Lightning Bolt {R}"),
            _option(1, kind="activate", card_name="Forest",
                    description="Play land"),
        )
        assert policy(ctx) == ("option_id", 1)

    def test_play_land_description_matches(self):
        policy = GoldfishPolicy()
        ctx = _priority(
            _option(0, kind="play_card", card_name="Bear",
                    description="Grizzly Bears {1}{G}"),
            _option(2, kind="activate", description="Play land"),
        )
        assert policy(ctx) == ("option_id", 2)

    def test_island_does_not_match_land(self):
        # "Island" contains the substring "land" but is not a land-play marker.
        policy = GoldfishPolicy()
        ctx = _priority(
            _option(0, kind="play_card", card_name="Island Drake",
                    description="Island Drake {2}{U}"),
            _option(1, kind="play_card", card_name="Bear",
                    description="Grizzly Bears {1}{G}"),
        )
        # No land found: cheapest spell wins (Bear at CMC 2 beats Drake at 3).
        assert policy(ctx) == ("option_id", 1)

    def test_oracle_text_mentioning_land_is_not_a_land_play(self):
        # "Destroy target land" starts with a verb, not a land-play marker.
        policy = GoldfishPolicy()
        ctx = _priority(
            _option(0, description="Destroy target land."),
            _option(1, description="Grizzly Bears {1}{G}"),
        )
        assert policy(ctx) == ("option_id", 1)

    def test_picks_cheapest_spell(self):
        policy = GoldfishPolicy()
        ctx = _priority(
            _option(0, description="Expensive {5}"),
            _option(1, description="Cheap {G}"),
        )
        assert policy(ctx) == ("option_id", 1)

    def test_cheapest_tie_breaks_on_lowest_id(self):
        policy = GoldfishPolicy()
        ctx = _priority(
            _option(7, description="A {1}{G}"),
            _option(3, description="B {2}"),
        )
        assert policy(ctx) == ("option_id", 3)

    def test_unknown_cost_loses_to_parseable(self):
        policy = GoldfishPolicy()
        ctx = _priority(
            _option(0, description="Mystery"),          # unparseable -> 99
            _option(1, description="Cheap {G}"),        # 1
        )
        assert policy(ctx) == ("option_id", 1)

    def test_no_land_no_spell_falls_back_to_default(self):
        policy = GoldfishPolicy()
        ctx = _priority(
            _option(0, kind="activate", description="Activate A"),
            _option(5, kind="activate", description="Activate B"),
        )
        assert policy(ctx) == default_policy(ctx) == ("option_id", 5)


class TestMulliganLadder:
    def test_mulligans_down_to_five_then_keeps(self):
        policy = GoldfishPolicy()
        policy.new_game()
        assert policy(_mulligan(7)) == ("boolean_answer", False)
        assert policy(_mulligan(6)) == ("boolean_answer", False)
        assert policy(_mulligan(KEEP_HAND_SIZE)) == ("boolean_answer", True)
        assert policy.mulligans == 2

    def test_never_mulligans_below_floor(self):
        policy = GoldfishPolicy()
        policy.new_game()
        assert policy(_mulligan(4)) == ("boolean_answer", True)
        assert policy(_mulligan(1)) == ("boolean_answer", True)

    def test_new_game_resets_counter(self):
        policy = GoldfishPolicy()
        policy.new_game()
        policy(_mulligan(7))
        policy(_mulligan(6))
        assert policy.mulligans == 2
        policy.new_game()
        assert policy.mulligans == 0


class TestCombatDelegation:
    def test_declare_attackers_attacks_everything(self):
        policy = GoldfishPolicy()
        ctx = DecisionContext(request=pb.DecisionRequest(
            game_id=1, decision_id=1, player=0,
            decision_type=pb.DECISION_TYPE_DECLARE_ATTACKERS,
            candidates=[pb.CardCandidate(card_id=20, name="Bear"),
                        pb.CardCandidate(card_id=21, name="Bear")],
            defender_players=[1],
        ))
        assert policy(ctx) == ("attackers", [(20, 1), (21, 1)])

    def test_assign_combat_damage_uses_default(self):
        policy = GoldfishPolicy()
        ctx = DecisionContext(request=pb.DecisionRequest(
            game_id=1, decision_id=1, player=0,
            decision_type=pb.DECISION_TYPE_ASSIGN_COMBAT_DAMAGE,
            candidates=[pb.CardCandidate(card_id=30, name="Blocker")],
            defender_players=[1], damage_amount=4, damage_source_card=20,
        ))
        assert policy(ctx) == default_policy(ctx)

    def test_other_types_use_default(self):
        policy = GoldfishPolicy()
        ctx = DecisionContext(request=pb.DecisionRequest(
            game_id=1, decision_id=1, player=0,
            decision_type=pb.DECISION_TYPE_SCRY_ARRANGE,
            candidates=[pb.CardCandidate(card_id=1, name="A"),
                        pb.CardCandidate(card_id=2, name="B")],
        ))
        assert policy(ctx) == default_policy(ctx)


class _FakeClient:
    """Minimal v6 fake: a scripted request per submit, one event per submit.

    Each get_decision returns the next scripted DecisionRequest (decision_id
    assigned monotonically); the game is over once the event stream is spent.
    Records every submitted answer so two runs can be compared.
    """

    def __init__(self, stream, requests):
        self._stream = list(stream)
        self._requests = list(requests)
        self.calls = 0
        self.answered = 0
        self.submits: list[tuple[int, int, object]] = []
        self.game_id = 0
        self.stopped: int | None = None

    def start_game(self, decks, seed, player_types=None, **kw):
        self.calls += 1
        self.answered = 0
        self.submits = []
        self.game_id = self.calls * 100 + seed
        return self.game_id

    def is_game_over(self, game_id):
        return pb.GameOver(over=self.answered >= len(self._requests),
                           winner=0, outcome=pb.OUTCOME_WIN, reason="lethal")

    def get_decision(self, game_id):
        template = self._requests[self.answered]
        req = pb.DecisionRequest()
        req.CopyFrom(template)
        req.game_id = game_id
        req.decision_id = self.answered + 1
        return req

    def submit_decision(self, game_id, decision_id, answer):
        self.submits.append((game_id, decision_id, answer))
        event = self._stream[self.answered]
        self.answered += 1
        return pb.StepResult(events=[event])

    def poll_events(self, game_id, cursor=0):
        events = [e for e in self._stream[: self.answered] if e.seq > cursor]
        next_cursor = events[-1].seq if events else cursor
        return pb.EventBatch(events=events, next_cursor=next_cursor)

    def drain_events(self, game_id, cursor=0):
        events: list[pb.GameEvent] = []
        while True:
            batch = self.poll_events(game_id, cursor)
            events.extend(batch.events)
            if batch.next_cursor <= cursor:
                break
            cursor = batch.next_cursor
        return events

    def stop_game(self, game_id):
        self.stopped = game_id


def _event(seq: int, type_: str) -> pb.GameEvent:
    return pb.GameEvent(seq=seq, type=type_, turn=1, phase="Main1", player=0)


def _priority_request(*options: pb.Option) -> pb.DecisionRequest:
    return pb.DecisionRequest(
        game_id=1, decision_id=0, player=0, turn=2, phase="Main1",
        decision_type=pb.DECISION_TYPE_PRIORITY, options=list(options),
    )


class TestGoldfishDeterminism:
    STREAM = [_event(1, "TurnStarted"), _event(2, "LandPlayed"),
              _event(3, "SpellCast"), _event(4, "GameOver")]
    REQUESTS = [
        _priority_request(
            _option(0, description="Lightning Bolt {R}"),
            _option(1, kind="activate", card_name="Forest",
                    description="Play land"),
        ),
        _priority_request(
            _option(0, description="Expensive {4}"),
            _option(1, description="Grizzly Bears {1}{G}"),
        ),
        _priority_request(
            _option(0, kind="activate", description="Activate A"),
            _option(2, kind="activate", description="Activate B"),
        ),
    ]

    def test_two_runs_same_seed_identical_trace(self):
        results = []
        for _ in range(2):
            client = _FakeClient(self.STREAM, self.REQUESTS)
            results.append(
                (client, run_game(client, DECKS, seed=11, policy=GoldfishPolicy(),
                                  player_types=REMOTE))
            )
        (c1, r1), (c2, r2) = results
        assert r1.decision_trace == r2.decision_trace
        assert c1.submits == c2.submits
        compare_event_streams(r1.events, r2.events,
                              (r1.outcome, r1.winner, r1.reason),
                              (r2.outcome, r2.winner, r2.reason))

    def test_goldfish_answers_land_then_cheapest(self):
        client = _FakeClient(self.STREAM, self.REQUESTS)
        result = run_game(client, DECKS, seed=11, policy=GoldfishPolicy(),
                          player_types=REMOTE)
        assert [answer for _, _, answer in result.decision_trace] == [
            ("option_id", 1),  # land play
            ("option_id", 1),  # cheapest spell
            ("option_id", 2),  # fallback (highest id)
        ]

    def test_run_game_resets_policy_mulligan_state(self):
        policy = GoldfishPolicy()
        policy.mulligans = 99  # stale state from a previous game
        client = _FakeClient(self.STREAM, self.REQUESTS)
        run_game(client, DECKS, seed=11, policy=policy, player_types=REMOTE)
        # new_game() ran at start; the script's PRIORITY decisions do not
        # mulligan, so the counter stays reset.
        assert policy.mulligans == 0
