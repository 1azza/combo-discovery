from combo_discovery.generated import forge_env_pb2 as pb
from combo_discovery.goldfish import GoldfishPolicy


def _req(options: list[pb.Option]) -> pb.DecisionRequest:
    return pb.DecisionRequest(player=0, turn=2, phase="Main1", options=options)


def test_prefers_land_over_spell():
    req = _req(
        [
            pb.Option(id=0, kind="play_card", card_name="Lightning Bolt", description="Cost: 1 R"),
            pb.Option(id=1, kind="play_card", card_name="Forest", description="Land"),
        ]
    )
    assert GoldfishPolicy().decide(req) == 1


def test_prefers_cheapest_spell_when_no_land():
    req = _req(
        [
            pb.Option(id=0, kind="play_card", card_name="Expensive", description="Cost: 5"),
            pb.Option(id=1, kind="play_card", card_name="Cheap", description="Cost: 1"),
        ]
    )
    assert GoldfishPolicy().decide(req) == 1


def test_falls_back_to_activate_then_pass():
    activate = _req([pb.Option(id=3, kind="pass"), pb.Option(id=2, kind="activate")])
    assert GoldfishPolicy().decide(activate) == 2
    only_pass = _req([pb.Option(id=5, kind="pass"), pb.Option(id=4, kind="pass")])
    assert GoldfishPolicy().decide(only_pass) == 4


def test_deterministic_tiebreak():
    req = _req(
        [
            pb.Option(id=9, kind="activate"),
            pb.Option(id=2, kind="activate"),
        ]
    )
    assert GoldfishPolicy().decide(req) == 2
    assert GoldfishPolicy().decide(req) == GoldfishPolicy().decide(req)
