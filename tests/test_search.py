"""Tests for the bounded branching search (``combo_discovery.search``)."""

from __future__ import annotations

from combo_discovery.env import GameNotActiveError
from combo_discovery.generated import forge_env_pb2 as pb
from combo_discovery.search import SearchResult, search_for_repeat

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
