from unittest.mock import MagicMock

import pytest

from combo_discovery.runner import DeterminismError, determinism_check


def ev(t, name="", detail=""):
    return (t, name, detail)


class FakeClient:
    """Mimics ForgeEnvClient just enough for run_game/determinism_check."""

    def __init__(self, streams):
        self.streams = [list(s) for s in streams]
        self.calls = 0
        self.collected: list[tuple[str, str, str]] = []
        self.game_id = 0

    def start_game(self, decks, seed, **kw):
        self.calls += 1
        self.collected = []
        self.game_id = self.calls
        return MagicMock(game_id=self.game_id)

    def is_game_over(self):
        return MagicMock(over=len(self.collected) >= len(self.streams[self.calls - 1]))

    def get_decision(self):
        return MagicMock(turn=1, options=[MagicMock(id=0, kind="pass")])

    def submit_decision(self, option_id):
        e = self.streams[self.calls - 1][len(self.collected)]
        self.collected.append(e)
        step = MagicMock()
        step.events = [MagicMock(type=e[0], card_name=e[1], detail=e[2])]
        return step


DECKS = [("a", "/tmp/a.dck"), ("b", "/tmp/b.dck")]


def test_determinism_pass():
    stream = [ev("SpellResolved", "Bolt"), ev("LifeChanged", "", "20->17")]
    client = FakeClient([stream, stream])
    determinism_check(client, DECKS, seed=1)


def test_determinism_detects_divergence():
    s1 = [ev("SpellResolved", "Bolt"), ev("LifeChanged", "", "20->17")]
    s2 = [ev("SpellResolved", "Bolt"), ev("LifeChanged", "", "20->18")]
    client = FakeClient([s1, s2])
    with pytest.raises(DeterminismError, match="20->18"):
        determinism_check(client, DECKS, seed=1)


def test_determinism_detects_length_mismatch():
    client = FakeClient([[ev("A"), ev("B")], [ev("A")]])
    with pytest.raises(DeterminismError, match="length"):
        determinism_check(client, DECKS, seed=1)


def test_run_game_collects_events():
    from combo_discovery.runner import run_game

    client = FakeClient([[ev("TurnStarted", "", "1")]])
    client.is_game_over = lambda: MagicMock(over=len(client.collected) >= 1)
    result = run_game(client, DECKS, seed=3)
    assert result.seed == 3
    assert result.n_events == 1
    assert result.events == [ev("TurnStarted", "", "1")]
