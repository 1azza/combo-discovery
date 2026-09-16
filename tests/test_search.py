"""Tests for the bounded branching search (``combo_discovery.search``)."""

from __future__ import annotations

from combo_discovery import search as search_mod
from combo_discovery.env import GameNotActiveError, InvalidRequestError
from combo_discovery.generated import forge_env_pb2 as pb
from combo_discovery.runner import DecisionContext
from combo_discovery.search import (
    SearchResult,
    SequentialPolicy,
    search_by_replay,
    search_for_loops,
    search_for_repeat,
    search_then_verify,
    witness_with_search,
)
from combo_discovery.witness import (
    Candidate,
    LinkPlan,
    WitnessPolicy,
    WitnessResult,
    build_scenario,
)

TARGET = "TargetCard"


class FakeSearchClient:
    """Scripted PRIORITY stream with a modelled snapshot/restore rewind.

    Every decision offers two ``activate`` options: ``Alpha`` (id 0) and the
    ``target`` (id 1).  Submitting the target option appends a target
    ``SpellCast`` event; submitting ``Alpha`` never does.  ``snapshot`` saves
    the decision counter, ``restore`` rewinds it (events stay append-only, as on
    the harness), so the driver must roll back its own counters.
    """

    def __init__(self, emit_target: bool = True, target: str = TARGET):
        self.game_id = 77
        self.decision_seq = 0
        self._event_seq = 0
        self.events: list[pb.GameEvent] = []
        self._saves: dict[bytes, int] = {}
        self._token_counter = 0
        self.snapshots = 0
        self.restores = 0
        self.submitted: list[tuple] = []
        self.stopped: int | None = None
        self.emit_target = bool(emit_target)
        self.target = target

    # -- game lifecycle (caller owns start/stop) ---------------------------
    def start_game(self, decks, seed, player_types=None, **kwargs):
        return self.game_id

    def stop_game(self, game_id):
        self.stopped = game_id

    # -- decision stream ----------------------------------------------------
    def get_decision(self, game_id):
        self.decision_seq += 1
        return pb.DecisionRequest(
            game_id=game_id,
            decision_id=self.decision_seq,
            player=0,
            turn=1,
            phase="Main1",
            decision_type=pb.DECISION_TYPE_PRIORITY,
            options=[
                pb.Option(id=0, kind="activate", card_name="Alpha"),
                pb.Option(id=1, kind="activate", card_name=self.target),
            ],
        )

    def submit_decision(self, game_id, decision_id, answer):
        self.submitted.append((decision_id, answer))
        if answer == ("option_id", 1) and self.emit_target:
            self._emit(self.target)
        return pb.StepResult()

    def _emit(self, card_name: str) -> None:
        self._event_seq += 1
        self.events.append(
            pb.GameEvent(
                seq=self._event_seq,
                game_id=self.game_id,
                type="SpellCast",
                player=0,
                card_name=card_name,
            )
        )

    # -- events -------------------------------------------------------------
    def poll_events(self, game_id, cursor=0):
        new = [e for e in self.events if int(e.seq) > int(cursor)]
        next_cursor = max((int(e.seq) for e in self.events), default=int(cursor))
        return pb.EventBatch(events=new, next_cursor=next_cursor)

    # -- snapshot / restore -------------------------------------------------
    def snapshot(self, game_id):
        self.snapshots += 1
        self._token_counter += 1
        token = bytes([self._token_counter])
        self._saves[token] = self.decision_seq
        return token, f"H{self.decision_seq}"

    def restore(self, game_id, token):
        self.restores += 1
        self.decision_seq = self._saves[token]


def _policy_picks_alpha(ctx):
    """The policy's first choice never fires the target (id 0 = Alpha)."""
    return ("option_id", 0)


def test_search_branches_to_the_option_that_repeats_target():
    client = FakeSearchClient(emit_target=True)
    result = search_for_repeat(
        client,
        client.game_id,
        target=TARGET,
        policy=_policy_picks_alpha,
        prefer_cards=(TARGET,),
        max_nodes=200,
        max_depth=4,
    )
    assert isinstance(result, SearchResult)
    assert result.success is True
    assert result.trigger_count >= 2
    # The policy's first candidate (Alpha) could never get there, so the search
    # must have backtracked to the target option.
    assert client.restores > 0
    assert client.snapshots > 0
    assert result.trace, "a successful search must report its decision trace"
    assert result.nodes > 0
    assert any(entry[2] == ("option_id", 1) for entry in result.trace)


def test_search_fails_and_stays_bounded_when_nothing_repeats():
    client = FakeSearchClient(emit_target=False)
    result = search_for_repeat(
        client,
        client.game_id,
        target=TARGET,
        policy=_policy_picks_alpha,
        prefer_cards=(TARGET,),
        max_nodes=12,
        max_depth=3,
    )
    assert result.success is False
    assert result.trigger_count == 0
    assert result.nodes <= 12
    assert result.trace == []
    assert "no legal branch" in result.reason


def test_search_reports_no_decision_when_game_not_active():
    client = FakeSearchClient()

    def dead_get_decision(game_id):
        raise GameNotActiveError("game is over")

    client.get_decision = dead_get_decision  # type: ignore[assignment]
    result = search_for_repeat(
        client, client.game_id, target=TARGET, policy=_policy_picks_alpha
    )
    assert result.success is False
    assert result.nodes == 0
    assert "no outstanding decision" in result.reason


class RaisingRestoreClient(FakeSearchClient):
    """The harness drops tokens on game over; a restore then raises."""

    def restore(self, game_id, token):
        raise InvalidRequestError(
            "INVALID_ARGUMENT: stale decision: unknown or expired snapshot token"
        )


def test_search_survives_expired_snapshot_token():
    # A branch that reaches game over clears the harness's tokens, so the
    # backtracking restore fails.  The search must fail cleanly, not raise.
    client = RaisingRestoreClient(emit_target=False)
    result = search_for_repeat(
        client,
        client.game_id,
        target=TARGET,
        policy=_policy_picks_alpha,
        prefer_cards=(TARGET,),
        max_nodes=50,
        max_depth=2,
    )
    assert result.success is False
    assert result.trace == []
    assert result.nodes <= 50


# ---------------------------------------------------------------------------
# SequentialPolicy (replay) + search_then_verify
# ---------------------------------------------------------------------------


def _priority_ctx(options):
    return DecisionContext(
        request=pb.DecisionRequest(
            game_id=1,
            decision_id=1,
            player=0,
            turn=1,
            phase="Main1",
            decision_type=pb.DECISION_TYPE_PRIORITY,
            options=list(options),
        )
    )


class _RecordingBase:
    def __init__(self, answer):
        self.answer = answer
        self.calls = 0

    def __call__(self, ctx):
        self.calls += 1
        return self.answer


def test_sequential_policy_replays_then_delegates():
    first, second = ("option_id", 1), ("option_id", 2)
    base = _RecordingBase(("option_id", 9))
    policy = SequentialPolicy([first, second], base=base, links=[LinkPlan("A", "B")])
    ctx = _priority_ctx([pb.Option(id=0, kind="pass", card_name="")])

    assert isinstance(policy, WitnessPolicy)
    assert policy(ctx) == first
    assert policy(ctx) == second
    assert base.calls == 0
    assert policy(ctx) == ("option_id", 9)
    assert base.calls == 1
    assert policy.replayed == 2

    # The driver calls new_game() on every fresh game; the replay resets.
    policy.new_game()
    assert policy.replayed == 0
    assert policy(ctx) == first

    # WitnessPolicy-compatible surface for the driver's diagnostics/gate.
    assert hasattr(policy, "note_card_event")
    assert hasattr(policy, "iterations")
    assert hasattr(policy, "link_hits")
    assert hasattr(policy, "trigger_hits")
    assert isinstance(policy.diagnostics(), dict)


class FakeVerifyClient:
    """Minimal harness fake where the Phase A search fails."""

    def __init__(self):
        self.game_id = 55
        self.start_calls = []
        self.stopped = []
        self._decision = 0

    def start_game(self, decks, seed, player_types=None, **kwargs):
        self.start_calls.append((list(decks), int(seed), list(player_types or [])))
        return self.game_id

    def stop_game(self, game_id):
        self.stopped.append(game_id)

    def is_game_over(self, game_id):
        return pb.GameOver(over=False)

    def get_decision(self, game_id):
        self._decision += 1
        return pb.DecisionRequest(
            game_id=game_id,
            decision_id=self._decision,
            player=0,
            turn=1,
            phase="Main1",
            decision_type=pb.DECISION_TYPE_PRIORITY,
            options=[pb.Option(id=0, kind="pass", card_name="")],
        )

    def setup_scenario(self, game_id, scenario):
        return "H0", 1

    def poll_events(self, game_id, cursor=0):
        return pb.EventBatch(next_cursor=int(cursor))

    def submit_decision(self, game_id, decision_id, answer):
        return pb.StepResult()


def test_search_then_verify_search_failure_is_inconclusive():
    client = FakeVerifyClient()
    combo = Candidate(cards=("A", "B"), type_lines=("Creature", "Enchantment"))
    scenario = build_scenario(combo)

    result = search_then_verify(
        client,
        scenario,
        combo,
        target=TARGET,
        decks=[("A", "decks/goldfish_A.dck"), ("B", "decks/goldfish_B.dck")],
        search_nodes=1,
        search_depth=1,
    )

    # No verdict is invented: the search failure is reported as inconclusive.
    assert result.verdict == "inconclusive"
    assert "search found no path" in result.evidence["reason"]
    assert result.evidence["search_nodes"] == 1
    assert result.evidence["search_depth"] == 0
    assert result.evidence["search_fires"] == 0
    assert result.evidence["search_trace_len"] == 0
    # Phase A started and stopped a game; Phase B never ran.
    assert len(client.start_calls) == 1
    assert client.stopped == [client.game_id]


# ---------------------------------------------------------------------------
# witness_with_search (ordinary run, search fallback)
# ---------------------------------------------------------------------------


def _witness_result(verdict, reason, evidence=None):
    combo = Candidate(cards=("A", "B"))
    return WitnessResult(
        verdict=verdict,
        scenario=build_scenario(combo),
        iterations=0,
        evidence={**(evidence or {}), "reason": reason},
    )


def _scenario_and_combo():
    combo = Candidate(cards=("A", "B"), kind="pair", key="7")
    return build_scenario(combo), combo


def test_witness_with_search_returns_non_inconclusive_search_verdict(monkeypatch):
    scenario, combo = _scenario_and_combo()
    normal = _witness_result("inconclusive", "normal run")
    searched = _witness_result(
        "loops",
        "judge verdict",
        {
            "search_nodes": 5,
            "search_depth": 2,
            "search_fires": 2,
            "search_reason": "target fired 2 times (>= 2)",
            "search_trace_len": 3,
            "replayed_answers": 3,
        },
    )
    calls = {"normal": 0, "search": 0}

    def fake_normal(client, scenario, policy, **kwargs):
        calls["normal"] += 1
        return normal

    def fake_search(client, scenario, combo_arg, **kwargs):
        calls["search"] += 1
        return searched

    monkeypatch.setattr(search_mod, "run_witness", fake_normal)
    monkeypatch.setattr(search_mod, "search_then_verify", fake_search)

    out = witness_with_search(
        object(), scenario, combo, target="A", decks=[("A", "a.dck"), ("B", "b.dck")]
    )

    assert out is searched
    assert out.verdict == "loops"
    assert out.evidence["fallback"] == "search"
    assert out.evidence["search_nodes"] == 5
    assert out.evidence["replayed_answers"] == 3
    assert calls == {"normal": 1, "search": 1}


def test_witness_with_search_keeps_original_when_both_inconclusive(monkeypatch):
    scenario, combo = _scenario_and_combo()
    normal = _witness_result("inconclusive", "normal run")
    searched = _witness_result(
        "inconclusive",
        "no path",
        {
            "search_nodes": 1,
            "search_depth": 0,
            "search_fires": 0,
            "search_reason": "no legal branch",
            "search_trace_len": 0,
        },
    )
    monkeypatch.setattr(search_mod, "run_witness", lambda *a, **k: normal)
    monkeypatch.setattr(search_mod, "search_then_verify", lambda *a, **k: searched)

    out = witness_with_search(
        object(), scenario, combo, target="A", decks=[("A", "a.dck"), ("B", "b.dck")]
    )

    assert out is normal  # never invents a verdict
    assert out.verdict == "inconclusive"
    assert out.evidence["reason"] == "normal run"
    assert out.evidence["fallback"] == "search"
    assert out.evidence["search_nodes"] == 1


def test_witness_with_search_skips_search_on_non_inconclusive(monkeypatch):
    scenario, combo = _scenario_and_combo()
    normal = _witness_result("loops", "already looped")

    def boom(*args, **kwargs):
        raise AssertionError("search must not run for a decided verdict")

    monkeypatch.setattr(search_mod, "run_witness", lambda *a, **k: normal)
    monkeypatch.setattr(search_mod, "search_then_verify", boom)

    out = witness_with_search(
        object(), scenario, combo, target="A", decks=[("A", "a.dck"), ("B", "b.dck")]
    )

    assert out is normal
    assert out.evidence["fallback"] == "none"
    assert "search_nodes" not in out.evidence


def test_witness_with_search_respects_search_false(monkeypatch):
    scenario, combo = _scenario_and_combo()
    normal = _witness_result("inconclusive", "normal run")

    def boom(*args, **kwargs):
        raise AssertionError("search disabled")

    monkeypatch.setattr(search_mod, "run_witness", lambda *a, **k: normal)
    monkeypatch.setattr(search_mod, "search_then_verify", boom)

    out = witness_with_search(
        object(),
        scenario,
        combo,
        target="A",
        decks=[("A", "a.dck"), ("B", "b.dck")],
        search=False,
    )

    assert out is normal
    assert out.evidence["fallback"] == "none"


# ---------------------------------------------------------------------------
# search_by_replay (branching over non-priority decisions)
# ---------------------------------------------------------------------------


class FakeReplayClient:
    """Scripted stream with a DECLARE_ATTACKERS decision.

    The target ``SpellCast`` fires twice only when the *variant* answer
    (declare no attackers) is submitted at decision index 3; the baseline's
    "declare all" never fires it.  ``start_game`` resets the script so a replay
    from the start is deterministic.
    """

    def __init__(self, emit_on_variant: bool = True, target: str = TARGET,
                 game_over_at: int = 7):
        self._next_gid = 100
        self.game_id = 0
        self.start_calls = 0
        self.stopped: list[int] = []
        self._idx = 0
        self._events: list[pb.GameEvent] = []
        self._seq = 0
        self.submissions: list = []
        self.emit_on_variant = bool(emit_on_variant)
        self.target = target
        self.game_over_at = int(game_over_at)

    def start_game(self, decks, seed, player_types=None, **kwargs):
        self.start_calls += 1
        self._next_gid += 1
        self.game_id = self._next_gid
        self._idx = 0
        self._events = []
        self._seq = 0
        return self.game_id

    def stop_game(self, game_id):
        self.stopped.append(game_id)

    def is_game_over(self, game_id):
        return pb.GameOver(over=False)

    def setup_scenario(self, game_id, scenario):
        return "H0", 1

    def get_decision(self, game_id):
        self._idx += 1
        if self._idx >= self.game_over_at:
            raise GameNotActiveError("game over")
        if self._idx == 3:
            return pb.DecisionRequest(
                game_id=game_id,
                decision_id=self._idx,
                player=0,
                turn=1,
                phase="Combat",
                decision_type=pb.DECISION_TYPE_DECLARE_ATTACKERS,
                candidates=[
                    pb.CardCandidate(card_id=10),
                    pb.CardCandidate(card_id=11),
                ],
                defender_players=[1],
                attacker_cards=[10, 11],
            )
        return pb.DecisionRequest(
            game_id=game_id,
            decision_id=self._idx,
            player=0,
            turn=1,
            phase="Main1",
            decision_type=pb.DECISION_TYPE_PRIORITY,
            options=[pb.Option(id=0, kind="pass", card_name="")],
        )

    def submit_decision(self, game_id, decision_id, answer):
        self.submissions.append(answer)
        if self._idx == 3 and answer == ("attackers", []) and self.emit_on_variant:
            self._emit(self.target)
            self._emit(self.target)
        return pb.StepResult()

    def _emit(self, card_name):
        self._seq += 1
        self._events.append(
            pb.GameEvent(
                seq=self._seq,
                game_id=self.game_id,
                type="SpellCast",
                player=0,
                card_name=card_name,
            )
        )

    def poll_events(self, game_id, cursor=0):
        events = [e for e in self._events if int(e.seq) > int(cursor)]
        next_cursor = max((int(e.seq) for e in self._events), default=int(cursor))
        return pb.EventBatch(events=events, next_cursor=next_cursor)


def _replay_combo_scenario():
    combo = Candidate(cards=("A", "B"), type_lines=("Creature", "Enchantment"))
    return build_scenario(combo), combo


DECKS = [("A", "decks/goldfish_A.dck"), ("B", "decks/goldfish_B.dck")]


def test_search_by_replay_finds_variant_that_fires_target():
    client = FakeReplayClient(emit_on_variant=True)
    scenario, combo = _replay_combo_scenario()

    result = search_by_replay(
        client, scenario, combo, target=TARGET, decks=DECKS,
        max_decisions=20, max_variants=3,
    )

    assert result.success is True
    assert result.trigger_count >= 2
    # The successful trace carries the variant (declare no attackers).
    assert any(entry[1] == ("attackers", []) for entry in result.trace)
    # The baseline alone could never fire it, so a replay game was started.
    assert client.start_calls > 1
    assert result.nodes > 0
    assert result.depth == len(result.trace)


def test_search_by_replay_is_bounded_when_no_variant_fires():
    client = FakeReplayClient(emit_on_variant=False)
    scenario, combo = _replay_combo_scenario()

    result = search_by_replay(
        client, scenario, combo, target=TARGET, decks=DECKS,
        max_decisions=20, max_variants=3,
    )

    assert result.success is False
    assert result.trace == []
    assert "no replay variant" in result.reason
    assert client.start_calls <= 3 + 1
    assert result.nodes <= 20 * (3 + 1)


# ---------------------------------------------------------------------------
# search_for_loops (goal = the judge's verdict)
# ---------------------------------------------------------------------------


def test_search_for_loops_prefers_variant_loops_over_inconclusive(monkeypatch):
    """A variant sequence submitted through SequentialPolicy wins over the
    baseline's inconclusive.  ``run_witness`` is stubbed because modelling the
    real loop detector is not the unit under test; sequence selection and
    verdict preference are."""
    client = FakeReplayClient()
    scenario, combo = _replay_combo_scenario()
    seen: list = []

    def fake_run_witness(client_arg, scenario_arg, policy, **kwargs):
        seen.append(policy)
        if isinstance(policy, SequentialPolicy) and (
            ("attackers", []) in policy._replay
        ):
            return WitnessResult(
                verdict="loops",
                scenario=scenario_arg,
                iterations=1,
                evidence={"reason": "judge says loops"},
            )
        return WitnessResult(
            verdict="inconclusive",
            scenario=scenario_arg,
            iterations=0,
            evidence={"reason": "baseline inconclusive"},
        )

    monkeypatch.setattr(search_mod, "run_witness", fake_run_witness)

    result = search_for_loops(
        client,
        scenario,
        combo,
        seeds=1,
        max_iterations=6,
        max_decisions=20,
        decks=DECKS,
        max_sequences=8,
    )

    assert result.verdict == "loops"
    assert result.evidence["reason"] == "judge says loops"
    assert result.evidence["search_kind"] == "loops"
    assert result.evidence["best_verdict"] == "loops"
    assert result.evidence["sequences_tried"] == 2
    # The baseline (plain WitnessPolicy) was verified first, then a
    # SequentialPolicy carrying the substituted variant.
    assert isinstance(seen[0], WitnessPolicy)
    assert not isinstance(seen[0], SequentialPolicy)
    assert any(
        isinstance(p, SequentialPolicy) and ("attackers", []) in p._replay
        for p in seen
    )


def test_search_for_loops_returns_strongest_without_loops(monkeypatch):
    client = FakeReplayClient()
    scenario, combo = _replay_combo_scenario()
    calls = {"n": 0}

    def fake_run_witness(client_arg, scenario_arg, policy, **kwargs):
        calls["n"] += 1
        verdict = "refuted" if calls["n"] == 2 else "inconclusive"
        return WitnessResult(
            verdict=verdict, scenario=scenario_arg, iterations=0,
            evidence={"reason": verdict},
        )

    monkeypatch.setattr(search_mod, "run_witness", fake_run_witness)

    result = search_for_loops(
        client, scenario, combo, seeds=1, max_decisions=20, decks=DECKS,
        max_sequences=2,
    )

    # inconclusive > refuted, and max_sequences=2 means baseline + one variant.
    assert result.verdict == "inconclusive"
    assert result.evidence["sequences_tried"] == 2
    assert result.evidence["best_verdict"] == "inconclusive"
