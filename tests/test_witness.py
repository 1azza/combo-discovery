"""Witness-search tests (fakes only; no live harness)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import pytest

from combo_discovery.env import GameNotActiveError
from combo_discovery.generated import forge_env_pb2 as pb
from combo_discovery.runner import DecisionContext
from combo_discovery.witness import (
    CardSpec,
    Candidate,
    DEFAULT_GRAVEYARD,
    GAME_STATE_GROWTH_KEYS,
    GROWTH_KEYS,
    LinkPlan,
    MAX_FORCED_PASSES,
    MAX_PREGAME_DECISIONS,
    Observation,
    PlayerScenario,
    SPIN_THRESHOLD,
    Scenario,
    WitnessPolicy,
    _absolute_deck_paths,
    build_observation,
    build_scenario,
    detect_loop,
    is_graveyard_gated,
    link_plans,
    resource_totals,
    run_witness,
    synthetic_cycle,
    witness_signature,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeContext:
    type_line: str = ""


class FakeAbility:
    def __init__(self, card_name: str, ability_kind: str = "", type_line: str = ""):
        self.card_name = card_name
        self.ability_kind = ability_kind
        self._ctx = FakeContext(type_line) if type_line else None

    def card_context(self):
        return self._ctx


@dataclass
class FakeRef:
    card_name: str


@dataclass
class FakeLink:
    src: FakeRef
    dst: FakeRef
    kind: str = "re_trigger"
    subkind: str = "copy"
    motif: str = "COPIES_CREATURE~ETB_TRIGGER"


@dataclass
class FakeCombo:
    cards: tuple[str, ...]
    links: tuple = ()
    abilities: tuple = ()
    card_ids: tuple[int, ...] = ()
    preconditions: tuple[str, ...] = ()
    infinite: bool = False


def combo_ab(**kwargs) -> FakeCombo:
    return FakeCombo(
        cards=("Altar", "Ghost"),
        links=(
            FakeLink(FakeRef("Altar"), FakeRef("Ghost")),
            FakeLink(FakeRef("Ghost"), FakeRef("Altar")),
        ),
        **kwargs,
    )


def priority_ctx(names, *, player=0, decision_id=1) -> DecisionContext:
    options = [
        pb.Option(id=i, kind="activate", card_name=n, description=f"Activate {n}")
        for i, n in enumerate(names)
    ]
    return DecisionContext(
        request=pb.DecisionRequest(
            game_id=1,
            decision_id=decision_id,
            player=player,
            turn=1,
            phase="Main1",
            decision_type=pb.DECISION_TYPE_PRIORITY,
            options=options,
        )
    )


def mode_ctx(
    options, *, player=0, decision_id=2, min_choices=1, max_choices=1
) -> DecisionContext:
    return DecisionContext(
        request=pb.DecisionRequest(
            game_id=1,
            decision_id=decision_id,
            player=player,
            decision_type=pb.DECISION_TYPE_CHOOSE_MODE,
            mode_options=[
                pb.ModeOption(id=mode_id, description=description)
                for mode_id, description in options
            ],
            min_choices=min_choices,
            max_choices=max_choices,
        )
    )


def target_ctx(
    candidates, *, player=0, decision_id=2, min_choices=1, max_choices=1
) -> DecisionContext:
    return DecisionContext(
        request=pb.DecisionRequest(
            game_id=1,
            decision_id=decision_id,
            player=player,
            decision_type=pb.DECISION_TYPE_CHOOSE_TARGETS,
            candidates=[
                pb.CardCandidate(card_id=card_id, name=name)
                for card_id, name in candidates
            ],
            min_choices=min_choices,
            max_choices=max_choices,
        )
    )


def obs(iteration: int, signature: str, **resources) -> Observation:
    return Observation(iteration=iteration, signature=signature, resources=dict(resources))


# ---------------------------------------------------------------------------
# Scenario build
# ---------------------------------------------------------------------------


class TestBuildScenario:
    def test_permanents_start_on_battlefield(self):
        scenario = build_scenario(combo_ab())
        assert len(scenario.players) == 2
        combo_player, opponent = scenario.players
        assert combo_player.player == 0
        assert [c.name for c in combo_player.battlefield] == ["Altar", "Ghost"]
        assert combo_player.hand == []
        assert combo_player.life == 20
        assert combo_player.mana  # generous starting pool
        assert combo_player.mana == {"W": 8, "U": 8, "B": 8, "R": 8, "G": 8, "C": 8}
        assert opponent.player == 1
        assert opponent.battlefield == []
        assert opponent.life == 20
        assert scenario.active_player == 0
        assert scenario.turn == 1
        assert scenario.phase == "Main1"
        assert scenario.require_outstanding_decision is True

    def test_non_permanent_starts_in_hand(self):
        combo = FakeCombo(
            cards=("Ritual", "Ghost"),
            abilities=(
                FakeAbility("Ritual", type_line="Instant"),
                FakeAbility("Ghost", type_line="Creature — Spirit"),
            ),
            links=(),
        )
        scenario = build_scenario(combo)
        combo_player = scenario.players[0]
        assert [c.name for c in combo_player.hand] == ["Ritual"]
        assert [c.name for c in combo_player.battlefield] == ["Ghost"]

    def test_ability_kind_fallback_marks_cast(self):
        combo = FakeCombo(
            cards=("Ritual",),
            abilities=(FakeAbility("Ritual", ability_kind="spell"),),
        )
        scenario = build_scenario(combo)
        assert [c.name for c in scenario.players[0].hand] == ["Ritual"]

    def test_candidate_pair_names_accepted(self):
        scenario = build_scenario(("A", "B"))
        assert [c.name for c in scenario.players[0].battlefield] == ["A", "B"]

    def test_aura_attaches_to_non_aura_partner(self):
        candidate = Candidate(
            cards=("Splinter Twin", "Deceiver Exarch"),
            type_lines=("Enchantment — Aura", "Creature — Efreet"),
        )
        scenario = build_scenario(candidate)
        battlefield = scenario.players[0].battlefield
        assert [c.name for c in battlefield] == ["Splinter Twin", "Deceiver Exarch"]
        # Deterministic ids in emission order, starting at 1.
        assert [c.id for c in battlefield] == [1, 2]
        assert battlefield[0].attached_to == 2  # Aura -> first non-Aura
        assert battlefield[1].attached_to == 0

    def test_aura_without_host_still_assigns_ids(self):
        candidate = Candidate(
            cards=("Splinter Twin",),
            type_lines=("Enchantment — Aura",),
        )
        scenario = build_scenario(candidate)
        battlefield = scenario.players[0].battlefield
        assert [c.id for c in battlefield] == [1]
        assert battlefield[0].attached_to == 0  # no non-Aura host: left unattached

    def test_equipment_attaches_to_non_attachment_partner(self):
        # Equipment that grants an activated untap ability (Umbral Mantle,
        # Thornbite Staff) is staged attached, like an Aura.
        candidate = Candidate(
            cards=("Umbral Mantle", "Fanatic of Rhonas"),
            type_lines=("Artifact — Equipment", "Creature — Snake Druid"),
        )
        scenario = build_scenario(candidate)
        battlefield = scenario.players[0].battlefield
        assert [c.id for c in battlefield] == [1, 2]
        assert battlefield[0].attached_to == 2  # Equipment -> first non-attachment
        assert battlefield[1].attached_to == 0

    def test_no_type_lines_leaves_ids_and_attachments_unset(self):
        scenario = build_scenario(combo_ab())
        battlefield = scenario.players[0].battlefield
        assert battlefield
        assert all(c.id == 0 and c.attached_to == 0 for c in battlefield)

    def test_empty_combo_rejected(self):
        with pytest.raises(ValueError, match="at least one"):
            build_scenario(None)

    def test_explicit_overrides(self):
        scenario = build_scenario(
            combo_ab(),
            life=7,
            opponent_life=3,
            mana={"U": 2},
            turn=4,
            phase="Combat",
            active_player=1,
        )
        assert scenario.players[0].life == 7
        assert scenario.players[1].life == 3
        assert scenario.players[0].mana == {"U": 2}
        assert scenario.active_player == 1
        assert (scenario.turn, scenario.phase) == (4, "Combat")


class TestGraveyardGating:
    DELIRIUM = (
        "Delirium — Whenever this creature attacks for the first time each "
        "turn, if there are four or more card types among cards in your "
        "graveyard, untap target creature."
    )

    def test_detects_delirium_text(self):
        assert is_graveyard_gated([self.DELIRIUM]) is True
        assert is_graveyard_gated(["Delirium — draw a card."]) is True

    def test_detects_generic_graveyard_marker(self):
        assert is_graveyard_gated(["four or more card types among cards in your graveyard"]) is True
        assert is_graveyard_gated(["Return two target cards in your graveyard to your hand."]) is True

    def test_ordinary_text_is_not_gated(self):
        assert is_graveyard_gated(["Flying, haste", "Whenever this attacks, untap it."]) is False

    def test_empty_text_is_not_gated(self):
        assert is_graveyard_gated([]) is False
        assert is_graveyard_gated([""]) is False
        assert is_graveyard_gated(()) is False

    def test_gated_combo_stages_default_graveyard(self):
        candidate = Candidate(
            cards=("Fear of Missing Out", "Helm of the Host"),
            oracle_texts=(self.DELIRIUM, ""),
        )
        scenario = build_scenario(candidate)
        graveyard = scenario.players[0].graveyard
        assert tuple(c.name for c in graveyard) == DEFAULT_GRAVEYARD
        # Four distinct primary types satisfy delirium.
        assert len(DEFAULT_GRAVEYARD) == 4

    def test_ordinary_combo_still_stages_empty_graveyard(self):
        candidate = Candidate(
            cards=("Splinter Twin", "Deceiver Exarch"),
            oracle_texts=("Enchant creature", "Flash"),
        )
        scenario = build_scenario(candidate)
        assert scenario.players[0].graveyard == []

    def test_explicit_graveyard_argument_wins(self):
        candidate = Candidate(
            cards=("Fear of Missing Out", "Helm of the Host"),
            oracle_texts=(self.DELIRIUM, ""),
        )
        explicit = [CardSpec(name="Custom Graveyard Card")]
        scenario = build_scenario(candidate, graveyard=explicit)
        assert [c.name for c in scenario.players[0].graveyard] == ["Custom Graveyard Card"]
        # Even an explicitly empty graveyard is honoured (caller said so).
        empty = build_scenario(candidate, graveyard=[])
        assert empty.players[0].graveyard == []

    def test_no_oracle_text_is_not_gated(self):
        combo = combo_ab()  # fakes carry no oracle text at all
        assert build_scenario(combo).players[0].graveyard == []


class TestCanonical:
    def test_canonical_is_stable(self):
        scenario = build_scenario(combo_ab())
        assert scenario.canonical() == scenario.canonical()
        assert scenario.scenario_hash() == scenario.scenario_hash()

    def test_non_library_zone_order_is_irrelevant(self):
        a = Scenario(players=[PlayerScenario(player=0, battlefield=[CardSpec("A"), CardSpec("B")])])
        b = Scenario(players=[PlayerScenario(player=0, battlefield=[CardSpec("B"), CardSpec("A")])])
        assert a.canonical() == b.canonical()
        assert a.scenario_hash() == b.scenario_hash()

    def test_library_order_is_significant(self):
        a = Scenario(players=[PlayerScenario(player=0, library=[CardSpec("A"), CardSpec("B")])])
        b = Scenario(players=[PlayerScenario(player=0, library=[CardSpec("B"), CardSpec("A")])])
        assert a.scenario_hash() != b.scenario_hash()
        assert [c["name"] for c in a.canonical()["players"][0]["library"]] == ["A", "B"]

    def test_card_spec_canonical_sorts_counters(self):
        spec = CardSpec("X", counters={"+1/+1": 2, "loyalty": 1})
        assert spec.canonical()["counters"] == {"+1/+1": 2, "loyalty": 1}

    def test_card_spec_canonical_includes_id_and_attachment(self):
        spec = CardSpec("X", id=3, attached_to=1)
        canon = spec.canonical()
        assert canon["id"] == 3
        assert canon["attached_to"] == 1
        # Round-trips: equal fields produce an equal canonical form.
        assert CardSpec("X", id=3, attached_to=1).canonical() == canon
        # Defaults stay unset.
        assert CardSpec("X").canonical()["id"] == 0
        assert CardSpec("X").canonical()["attached_to"] == 0

    def test_attachment_changes_scenario_hash(self):
        unattached = Scenario(
            players=[PlayerScenario(player=0, battlefield=[CardSpec("X", id=1)])]
        )
        attached = Scenario(
            players=[PlayerScenario(player=0, battlefield=[CardSpec("X", id=1)])]
        )
        attached.players[0].battlefield[0].attached_to = 1
        assert unattached.scenario_hash() != attached.scenario_hash()


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


class TestWitnessPolicy:
    def test_walks_synthetic_two_link_cycle(self):
        policy = WitnessPolicy(links=[LinkPlan("A", "B"), LinkPlan("B", "A")], player=0)
        policy.new_game()
        first = policy(priority_ctx(["A", "B"]))
        second = policy(priority_ctx(["A", "B"]))
        third = policy(priority_ctx(["A", "B"]))
        fourth = policy(priority_ctx(["A", "B"]))
        assert first == ("option_id", 0)
        assert second == ("option_id", 1)
        assert third == ("option_id", 0)
        assert fourth == ("option_id", 1)
        assert policy.iterations == 2

    def test_cursor_searches_forward_when_current_link_unavailable(self):
        policy = WitnessPolicy(links=[LinkPlan("A", "B"), LinkPlan("B", "A")], player=0)
        policy.new_game()
        answer = policy(priority_ctx(["B"]))  # A is unavailable; skip to B
        assert answer == ("option_id", 0)
        assert policy.cursor == 0  # B was the last link, so it wrapped
        assert policy.iterations == 1

    def test_match_link_falls_back_to_dst(self):
        # An Aura's granted ability is offered under the host creature's name.
        combo = Candidate(
            cards=("Splinter Twin", "Deceiver Exarch"),
            type_lines=("Enchantment — Aura", "Creature — Efreet"),
        )
        link = LinkPlan("Splinter Twin", "Deceiver Exarch")
        policy = WitnessPolicy(combo=combo, links=[link], player=0)
        options = [pb.Option(id=0, kind="activate", card_name="Deceiver Exarch")]
        hit = policy._match_link(options, link)
        assert hit is not None
        assert hit.card_name == "Deceiver Exarch"

    def test_match_link_does_not_fall_back_for_non_aura_src(self):
        policy = WitnessPolicy(links=[LinkPlan("Hippo", "Kiki")], player=0)
        options = [pb.Option(id=0, kind="activate", card_name="Kiki")]
        assert policy._match_link(options, policy.links[0]) is None

    def test_ordered_preferences_prefers_host_for_aura_source(self):
        # The loop-closing target of an Aura's granted ability is the tapped host
        # creature, not the Aura itself (live: Splinter Twin + Deceiver Exarch,
        # where targeting the Aura left the host tapped and broke the loop).
        combo = Candidate(
            cards=("Splinter Twin", "Deceiver Exarch"),
            type_lines=("Enchantment — Aura", "Creature — Efreet"),
        )
        link = LinkPlan("Splinter Twin", "Deceiver Exarch")
        policy = WitnessPolicy(combo=combo, links=[link], player=0)
        assert policy._ordered_preferences(link)[0] == "Deceiver Exarch"

    def test_ordered_preferences_keeps_source_first_without_aura(self):
        link = LinkPlan("Hippo", "Kiki")
        policy = WitnessPolicy(links=[link], player=0)
        assert policy._ordered_preferences(link)[0] == "Hippo"

    def test_non_priority_uses_active_link_params(self):
        link = LinkPlan("A", "B", params={"targets": ["A"]})
        policy = WitnessPolicy(links=[link], player=0)
        policy.new_game()
        policy(priority_ctx(["A"]))  # sets active = link
        target_ctx = DecisionContext(
            request=pb.DecisionRequest(
                game_id=1,
                decision_id=2,
                player=0,
                decision_type=pb.DECISION_TYPE_CHOOSE_TARGETS,
                candidates=[pb.CardCandidate(card_id=5, name="A")],
                min_choices=1,
                max_choices=1,
            )
        )
        assert policy(target_ctx) == ("targets", ([5], []))

    def test_mode_and_announce_params(self):
        link = LinkPlan("A", "B", params={"modes": [1, 2], "number": 5})
        policy = WitnessPolicy(links=[link], player=0)
        policy.new_game()
        policy(priority_ctx(["A"]))
        mode_ctx = DecisionContext(
            request=pb.DecisionRequest(
                game_id=1,
                decision_id=3,
                player=0,
                decision_type=pb.DECISION_TYPE_CHOOSE_MODE,
                mode_options=[pb.ModeOption(id=1), pb.ModeOption(id=2)],
                min_choices=2,
                max_choices=2,
            )
        )
        assert policy(mode_ctx) == ("mode_selection", [1, 2])
        announce_ctx = DecisionContext(
            request=pb.DecisionRequest(
                game_id=1,
                decision_id=4,
                player=0,
                decision_type=pb.DECISION_TYPE_ANNOUNCE,
                min_number=0,
                max_number=10,
            )
        )
        assert policy(announce_ctx) == ("number_answer", 5)

    # -- choice-aware selections (Fix 1) ------------------------------------

    def _kiki_pest_policy(self):
        # Synthetic ping-pong line: the first link (engine -> untapper) is the
        # active link while the untapper's ETB modal/target decision fires.
        policy = WitnessPolicy(
            links=[
                LinkPlan("Kiki-Jiki, Mirror Breaker", "Pestermite"),
                LinkPlan("Pestermite", "Kiki-Jiki, Mirror Breaker"),
            ],
            player=0,
        )
        policy.new_game()
        policy(priority_ctx(["Kiki-Jiki, Mirror Breaker"]))
        return policy

    def test_modal_tap_untap_prefers_untap(self):
        policy = self._kiki_pest_policy()
        ctx = mode_ctx(
            [(0, "Tap target creature."), (1, "Untap target creature.")]
        )
        assert policy(ctx) == ("mode_selection", [1])
        assert any("untap" in note for note in policy.notes)

    def test_modal_untap_not_chosen_when_link_wants_tap(self):
        # A tap-only line must not be flipped to untap just because the modal
        # offers it: the link's effect verb wins.
        policy = WitnessPolicy(
            links=[LinkPlan("Tapper", "Victim", kind="tap")], player=0
        )
        policy.new_game()
        policy(priority_ctx(["Tapper"]))
        ctx = mode_ctx(
            [(0, "Tap target creature."), (1, "Untap target creature.")]
        )
        assert policy(ctx) == ("mode_selection", [0])

    def test_mode_matches_link_effect_verb(self):
        policy = WitnessPolicy(
            links=[LinkPlan("Engine", "Bear", kind="pump")], player=0
        )
        policy.new_game()
        policy(priority_ctx(["Engine"]))
        ctx = mode_ctx(
            [(0, "Tap target creature."), (1, "Pump target creature.")]
        )
        assert policy(ctx) == ("mode_selection", [1])

    def test_target_prefers_engine_card(self):
        policy = self._kiki_pest_policy()
        # Engine card listed second: preference (not request order) must win.
        ctx = target_ctx(
            [(5, "Pestermite"), (7, "Kiki-Jiki, Mirror Breaker")]
        )
        assert policy(ctx) == ("targets", ([7], []))

    def test_target_falls_back_to_first_legal_candidate(self):
        policy = self._kiki_pest_policy()
        # Neither combo card is targetable: the first legal candidate is used.
        ctx = target_ctx([(5, "Grizzly Bears"), (6, "Forest")])
        assert policy(ctx) == ("targets", ([5], []))

    def test_choice_is_deterministic(self):
        first = self._kiki_pest_policy()
        second = self._kiki_pest_policy()
        modal = [(0, "Tap target creature."), (1, "Untap target creature.")]
        targets = [(5, "Pestermite"), (7, "Kiki-Jiki, Mirror Breaker")]
        assert first(mode_ctx(modal, decision_id=2)) == second(
            mode_ctx(modal, decision_id=2)
        )
        assert first(target_ctx(targets, decision_id=3)) == second(
            target_ctx(targets, decision_id=3)
        )

    def test_stall_is_bounded_and_iterations_advance(self):
        policy = WitnessPolicy(links=[LinkPlan("A", "B")], player=0, max_stall=3)
        policy.new_game()
        for _ in range(30):
            policy(priority_ctx(["Pass"]))  # no link ever matches
        assert policy.decisions == 30
        assert policy.iterations == 10  # every 3 stalls force one advance
        assert policy.cursor == 0

    def test_opponent_decisions_fall_back(self):
        policy = WitnessPolicy(links=[LinkPlan("A", "B")], player=0)
        policy.new_game()
        answer = policy(priority_ctx(["A"], player=1))
        # Opponent is not the combo player: default policy takes the highest id.
        assert answer == ("option_id", 0)
        assert policy.cursor == 0

    # -- robust matching (the zero-iteration bug) ---------------------------

    def test_backward_wrap_counts_a_full_iteration(self):
        # Regression: a two-link line whose second link is a passive trigger is
        # never offered as a PRIORITY option.  Re-offering the first link must
        # close the pass and increment ``iterations`` rather than pinning the
        # cursor forever (which produced the original zero-iteration result).
        policy = WitnessPolicy(
            links=[LinkPlan("Kiki", "Hippo"), LinkPlan("Hippo", "Kiki")], player=0
        )
        policy.new_game()
        ctx = priority_ctx(["Kiki"])
        assert policy(ctx) == ("option_id", 0)
        assert policy.iterations == 0
        assert policy(ctx) == ("option_id", 0)
        assert policy.iterations == 1
        assert policy(ctx) == ("option_id", 0)
        assert policy.iterations == 2
        assert policy.link_hits == [3, 0]

    def test_matches_source_in_description_when_card_name_empty(self):
        policy = WitnessPolicy(links=[LinkPlan("Altar", "Ghost")], player=0)
        policy.new_game()
        option = pb.Option(
            id=7, kind="activate", card_name="", description="Activate Altar ability"
        )
        ctx = DecisionContext(
            request=pb.DecisionRequest(
                game_id=1,
                decision_id=1,
                player=0,
                decision_type=pb.DECISION_TYPE_PRIORITY,
                options=[option],
            )
        )
        assert policy(ctx) == ("option_id", 7)

    def test_matches_option_kind_when_source_name_empty(self):
        policy = WitnessPolicy(links=[LinkPlan("", "", kind="activate")], player=0)
        policy.new_game()
        assert policy(priority_ctx(["Something"])) == ("option_id", 0)

    def test_short_name_does_not_match_inside_a_word(self):
        # Word-boundary matching: the one-letter card "a" must not match the
        # "a" inside "Activate".
        options = [pb.Option(id=0, kind="activate", card_name="", description="Activate")]
        assert WitnessPolicy._match_option(options, "a") is None

    def test_note_card_event_credits_matching_link_src(self):
        policy = WitnessPolicy(
            links=[
                LinkPlan("Krovikan Vampire", "B"),
                LinkPlan("A Killer Among Us", "C"),
            ],
            player=0,
        )
        policy.new_game()
        policy.note_card_event("Krovikan Vampire")
        assert policy.trigger_hits == [1, 0]
        diagnostics = policy.diagnostics()
        assert diagnostics["trigger_hits"] == [1, 0]
        assert diagnostics["link_hits"] == [0, 0]
        # executed_actions sums trigger hits with (unchanged) link hits.
        assert diagnostics["executed_actions"] == 1

    def test_note_card_event_is_case_insensitive_and_word_bounded(self):
        policy = WitnessPolicy(
            links=[LinkPlan("Sheoldred, Whispering One", "B")], player=0
        )
        policy.new_game()
        policy.note_card_event("sheoldred, whispering one")
        policy.note_card_event("SHEOLDRED, WHISPERING ONE")
        assert policy.trigger_hits == [2]

    def test_note_card_event_ignores_unrelated_and_empty_names(self):
        policy = WitnessPolicy(links=[LinkPlan("Altar", "Ghost")], player=0)
        policy.new_game()
        policy.note_card_event("Some Other Card")
        policy.note_card_event("")
        policy.note_card_event("   ")
        assert policy.trigger_hits == [0]
        assert policy.diagnostics()["executed_actions"] == 0

    def test_note_card_event_word_boundary_rejects_substring(self):
        # Same rule as ``_match_option``: a short source name must not match a
        # longer event name that merely contains it inside a word.
        policy = WitnessPolicy(links=[LinkPlan("Rat", "Ghost")], player=0)
        policy.new_game()
        policy.note_card_event("Rats of Rath")
        assert policy.trigger_hits == [0]

    def test_per_iteration_decision_budget_is_bounded(self):
        policy = WitnessPolicy(
            links=[LinkPlan("Z", "Y")], player=0,
            max_stall=1000, max_decisions_per_iteration=4,
        )
        policy.new_game()
        for _ in range(5):
            policy(priority_ctx(["Pass"]))
        assert any("budget" in note for note in policy.notes)
        assert policy.iterations >= 1


# ---------------------------------------------------------------------------
# Signature + loop detection
# ---------------------------------------------------------------------------


def make_state(*, mana: int = 0, life: tuple[int, int] = (20, 20), hash_: str = "H") -> pb.FullState:
    state = pb.FullState(game_id=1, turn=1, phase="Main1", active_player=0, state_hash=hash_)
    state.life.extend(life)
    state.typed_mana_pools.add(colorless=mana)
    perm = pb.Permanent(id=0, card_name="Altar")
    state.battlefield_cards.append(perm)
    zone = state.battlefield.add()
    zone.permanents.append(0)
    for _ in range(1):
        state.hand.add()
        state.graveyard.add()
        state.library.add()
        state.exile.add()
        state.command.add()
    return state


def add_token(state: pb.FullState, name: str = "Altar", *, tapped: bool = False) -> int:
    """Append a token permanent to player 0's battlefield; return its engine id."""
    pid = len(state.battlefield_cards)
    state.battlefield_cards.append(
        pb.Permanent(id=pid, card_name=name, is_token=True, tapped=tapped)
    )
    state.battlefield[0].permanents.append(pid)
    return pid


class TestWitnessSignature:
    def test_signature_excludes_growing_mana(self):
        sig_a = witness_signature(make_state(mana=0))
        sig_b = witness_signature(make_state(mana=9))
        assert sig_a == sig_b

    def test_signature_excludes_token_permanents(self):
        # A token-growing loop must keep a stable structural signature; tokens
        # are a *resource*, not part of the structure.
        base = make_state()
        with_tokens = make_state()
        add_token(with_tokens, "Hippocamp")
        add_token(with_tokens, "Hippocamp")
        assert witness_signature(base) == witness_signature(with_tokens)
        obs = build_observation(0, with_tokens)
        assert obs.resources["tokens"] == 2
        assert obs.resources["permanents"] == 3

    def test_signature_tracks_non_token_tapped_state(self):
        base = make_state()
        changed = make_state()
        add_token(changed)  # a token is invisible...
        assert witness_signature(base) == witness_signature(changed)
        changed.battlefield_cards[0].tapped = True  # ...a real permanent is not
        assert witness_signature(base) != witness_signature(changed)

    def test_resource_totals_track_mana(self):
        low = build_observation(0, make_state(mana=1))
        high = build_observation(1, make_state(mana=4))
        assert low.signature == high.signature
        assert high.resources["mana"] - low.resources["mana"] == 3

    def test_zone_volumes_are_not_structural(self):
        # Discard/draw/mill grow hand/graveyard/library counts monotonically, so
        # they are resources, not structure.  Regression for Fear of Missing
        # Out's ETB ("discard a card, then draw a card") growing the graveyard
        # every iteration and stopping a genuine loop from recurring.
        base = make_state()
        changed = make_state()
        changed.graveyard[0].cards.add(name="Dead", count=3)
        changed.library[0].cards.add(name="Top", count=2)
        changed.hand[0].cards.add(name="Held", count=1)
        assert witness_signature(base) == witness_signature(changed)

    def test_resource_totals_report_zone_volumes(self):
        state = make_state()
        state.graveyard[0].cards.add(name="Dead", count=3)
        state.library[0].cards.add(name="Top", count=2)
        state.hand[0].cards.add(name="Held", count=1)
        totals = resource_totals(state)
        assert totals["graveyard"] == 3
        assert totals["library"] == 2
        assert totals["hand"] == 1

    def test_signature_changes_with_battlefield(self):
        base = witness_signature(make_state())
        changed = make_state()
        changed.battlefield_cards[0].tapped = True
        assert witness_signature(changed) != base

    def test_life_is_not_in_the_structural_signature(self):
        # Life is a monotonic scalar: a combat loop damages the opponent every
        # iteration, so life must not gate structural recurrence (regression for
        # the Combat Celebrant + Kiki-Jiki false negative).
        assert "life" not in witness_signature(make_state())


class TestDetectLoop:
    def test_accepts_growing_resource_cycle(self):
        observations = [
            obs(0, "s1", mana=0),
            obs(1, "s2", mana=1),
            obs(2, "s1", mana=2),
            obs(3, "s2", mana=3),
        ]
        verdict, evidence = detect_loop(observations)
        assert verdict == "loops"
        assert evidence["kind"] == "recurrence"
        assert evidence["pair"] == [1, 3]
        assert evidence["grown"] == ["mana"]

    def test_token_growth_with_recurring_structure_is_loop(self):
        # Kiki-Jiki-style loop: the non-token board recurs while tokens grow.
        def state_with(tokens: int) -> pb.FullState:
            state = make_state(hash_=f"H{tokens}")
            for _ in range(tokens):
                add_token(state, "Hippocamp")
            return state

        observations = [build_observation(i, state_with(i)) for i in range(3)]
        assert len({o.signature for o in observations}) == 1
        verdict, evidence = detect_loop(observations)
        assert verdict == "loops"
        assert evidence["kind"] == "recurrence"
        assert "tokens" in evidence["grown"]

    def test_rejects_non_repeating_line(self):
        observations = [obs(0, "s1", mana=0), obs(1, "s2", mana=1), obs(2, "s3", mana=2)]
        verdict, evidence = detect_loop(observations)
        assert verdict == "no_loop"
        assert "no signature recurrence" in evidence["reason"]

    def test_rejects_growth_without_recurrence(self):
        observations = [obs(0, "a", mana=0), obs(1, "b", mana=5), obs(2, "c", mana=9)]
        verdict, _ = detect_loop(observations)
        assert verdict == "no_loop"

    def test_recurrence_without_growth_is_no_loop(self):
        observations = [
            obs(0, "s0", mana=2),
            obs(1, "s1", mana=2),
            obs(2, "s2", mana=2),
            obs(3, "s1", mana=2),
        ]
        verdict, evidence = detect_loop(observations)
        assert verdict == "no_loop"
        assert "no tracked resource grew" in evidence["reason"]

    def test_degenerate_identical_hash_with_growth_is_loop(self):
        observations = [
            Observation(iteration=0, signature="s1", state_hash="BASE",
                        resources={"tokens": 0}),
            Observation(iteration=1, signature="s2", state_hash="SAME",
                        resources={"tokens": 1}),
            Observation(iteration=2, signature="s3", state_hash="SAME",
                        resources={"tokens": 2}),
        ]
        verdict, evidence = detect_loop(observations)
        assert verdict == "loops"
        assert evidence["kind"] == "degenerate"
        assert evidence["pair"] == [1, 2]
        assert "tokens" in evidence["grown"]

    def test_identical_states_without_growth_is_a_stall(self):
        # Regression (live: The Fire Crystal + Captain of the Mists): two
        # consecutive identical states with nothing growing is a stall, not a
        # loop. The degenerate branch must require durable growth.
        observations = [
            Observation(iteration=0, signature="base", state_hash="BASE",
                        resources={"tokens": 0, "mana": 42}),
            Observation(iteration=1, signature="same", state_hash="SAME",
                        resources={"tokens": 0, "mana": 42}),
            Observation(iteration=2, signature="same", state_hash="SAME",
                        resources={"tokens": 0, "mana": 42}),
        ]
        verdict, _evidence = detect_loop(observations)
        assert verdict == "no_loop"

    def test_identical_baseline_pair_is_not_a_loop(self):
        # Regression (live Keldon Overseer / Elven Raft-Steerer): the baseline
        # sample and the first post-injection sample are bit-identical while
        # nothing has happened yet. That must not certify a loop.
        observations = [
            Observation(iteration=0, signature="s1", state_hash="SAME", turn=1),
            Observation(iteration=1, signature="s1", state_hash="SAME", turn=1),
        ]
        verdict, evidence = detect_loop(observations)
        assert verdict == "inconclusive"
        assert "post-baseline" in evidence["reason"]

    def test_cross_turn_recurrence_with_growth_is_not_a_loop(self):
        # Regression: a creature that untaps each turn (or a token army that
        # attacks) reproduces the signature across turns while a resource grows.
        # That is ordinary play, not an infinite loop.
        observations = [
            Observation(iteration=1, signature="same", resources={"mana": 0, "tokens": 1, "life": 40}, turn=1),
            Observation(iteration=3, signature="same", resources={"mana": 0, "tokens": 2, "life": 34}, turn=3),
            Observation(iteration=5, signature="same", resources={"mana": 0, "tokens": 3, "life": 28}, turn=5),
        ]
        verdict, evidence = detect_loop(observations)
        assert verdict == "no_loop"
        assert "across turns" in evidence["reason"]

    def test_mana_consuming_recurrence_is_not_infinite(self):
        # Orthion / Jolly Balloon Man shape: the copy ability costs mana every
        # pass and the untapper untaps the engine, not the lands, so the loop is
        # bounded by the starting pool, not infinite.
        observations = [
            Observation(iteration=0, signature="s0", resources={"mana": 40, "tokens": 0}, turn=1),
            Observation(iteration=1, signature="same", resources={"mana": 40, "tokens": 1}, turn=1),
            Observation(iteration=2, signature="same", resources={"mana": 39, "tokens": 2}, turn=1),
            Observation(iteration=3, signature="same", resources={"mana": 38, "tokens": 3}, turn=1),
        ]
        verdict, evidence = detect_loop(observations)
        assert verdict == "inconclusive"
        assert "mana" in evidence["reason"]

    def test_counter_only_growth_is_not_a_loop(self):
        # Live combat false positive (Aurelia/Genji Glove/Hexplate Wallbreaker):
        # once-per-turn extra-combat cards leave the board static while the
        # policy's own casts/activations accrue. That is not an infinite loop.
        observations = [
            Observation(iteration=0, signature="s0", resources={}, turn=1),
            Observation(
                iteration=1, signature="same",
                resources={"casts": 1, "spells_resolved": 1}, turn=1,
            ),
            Observation(
                iteration=2, signature="same",
                resources={"casts": 2, "spells_resolved": 2}, turn=1,
            ),
        ]
        verdict, evidence = detect_loop(observations)
        assert verdict == "inconclusive"
        assert "counters" in evidence["reason"]

    def test_zone_volume_growth_alone_is_not_a_loop(self):
        # graveyard/library/hand growth is reported as a resource but is not a
        # durable enough one to certify a loop: pure mill/discard stays
        # rejected, exactly as before this change.
        observations = [
            obs(0, "s0", graveyard=0, library=0),
            obs(1, "same", graveyard=1, library=1),
            obs(2, "same", graveyard=2, library=2),
        ]
        verdict, _ = detect_loop(observations)
        assert verdict == "inconclusive"

    def test_zone_volumes_are_growth_keys_but_not_game_state(self):
        for key in ("graveyard", "library", "hand"):
            assert key in GROWTH_KEYS
            assert key not in GAME_STATE_GROWTH_KEYS

    def test_insufficient_observations_is_inconclusive(self):
        assert detect_loop([])[0] == "inconclusive"
        assert detect_loop([obs(0, "s1", mana=0)])[0] == "inconclusive"


# ---------------------------------------------------------------------------
# run_witness (fake client)
# ---------------------------------------------------------------------------


class FakeWitnessClient:
    """Deterministic fake: each submitted decision adds one colorless mana."""

    def __init__(self):
        self.game_id = 77
        self.mana = 0
        self.decision_seq = 0
        self.stopped: int | None = None
        self.setup_calls: list[Scenario] = []
        self.view_players: list[int] = []

    def start_game(self, decks, seed, player_types=None, **kwargs):
        return self.game_id

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
                pb.Option(id=0, kind="activate", card_name="A"),
                pb.Option(id=1, kind="activate", card_name="B"),
            ],
        )

    def setup_scenario(self, game_id, scenario):
        self.setup_calls.append(scenario)
        return "H0", 3

    def submit_decision(self, game_id, decision_id, answer):
        self.mana += 1
        return pb.StepResult()

    def is_game_over(self, game_id):
        return pb.GameOver(over=False)

    def get_state(self, game_id, view_as_player=0):
        self.view_players.append(view_as_player)
        state = make_state(mana=self.mana, hash_=f"H{self.mana}")
        return state

    def poll_events(self, game_id, cursor=0):
        return pb.EventBatch(next_cursor=cursor)

    def stop_game(self, game_id):
        self.stopped = game_id


class FakePregameClient(FakeWitnessClient):
    """Answers ``mulligans`` MULLIGAN_KEEP decisions, then PRIORITY.

    Records the ordered RPC narrative (``"mulligan"`` / ``"priority"`` /
    ``"setup"``) and every submitted answer so a test can assert the
    drive-past-pre-game-then-inject sequence and that injection happens once.
    """

    def __init__(self, mulligans: int = 2):
        super().__init__()
        self.mulligans_left = int(mulligans)
        self.sequence: list[str] = []
        self.answers: list = []

    def get_decision(self, game_id):
        if self.mulligans_left > 0:
            self.mulligans_left -= 1
            self.decision_seq += 1
            self.sequence.append("mulligan")
            return pb.DecisionRequest(
                game_id=game_id,
                decision_id=self.decision_seq,
                player=0,
                turn=0,
                decision_type=pb.DECISION_TYPE_MULLIGAN_KEEP,
            )
        self.sequence.append("priority")
        return super().get_decision(game_id)

    def setup_scenario(self, game_id, scenario):
        self.sequence.append("setup")
        return super().setup_scenario(game_id, scenario)

    def submit_decision(self, game_id, decision_id, answer):
        self.answers.append(answer)
        return super().submit_decision(game_id, decision_id, answer)


class FakeEndlessPregameClient(FakeWitnessClient):
    """Always a MULLIGAN_KEEP decision: the pre-game window never ends."""

    def get_decision(self, game_id):
        self.decision_seq += 1
        return pb.DecisionRequest(
            game_id=game_id,
            decision_id=self.decision_seq,
            player=0,
            turn=0,
            decision_type=pb.DECISION_TYPE_MULLIGAN_KEEP,
        )


class FakeEventClient(FakeWitnessClient):
    """Broadcasts one card event (default a trigger's ``SpellCast``) once.

    The driver's first ``poll_events`` only establishes the event cursor, so
    the event is emitted on the second poll (the pre-loop baseline capture).
    """

    def __init__(
        self, card_name: str = "TriggerCard", player: int = 0,
        event_type: str = "SpellCast",
    ):
        super().__init__()
        self.card_name = card_name
        self.event_player = player
        self.event_type = event_type
        self._polls = 0
        self._emitted = False

    def poll_events(self, game_id, cursor=0):
        self._polls += 1
        if self._polls == 1 or self._emitted:
            return pb.EventBatch(next_cursor=cursor)
        self._emitted = True
        return pb.EventBatch(
            events=[
                pb.GameEvent(
                    seq=cursor + 1,
                    game_id=game_id,
                    type=self.event_type,
                    player=self.event_player,
                    card_name=self.card_name,
                )
            ],
            next_cursor=cursor + 1,
        )


class FakeRepeatingEventClient(FakeWitnessClient):
    """Broadcasts a card event on every poll after the cursor-setup poll.

    Models a trigger that fires on each decision (e.g. an upkeep/ETB trigger)
    while the driven policy never completes a line iteration, so the driver
    must advance observation boundaries off the trigger credit.
    """

    def __init__(
        self, card_name: str = "TriggerCard", player: int = 0,
        event_type: str = "SpellCast",
    ):
        super().__init__()
        self.card_name = card_name
        self.event_player = player
        self.event_type = event_type
        self._polls = 0

    def poll_events(self, game_id, cursor=0):
        self._polls += 1
        if self._polls == 1:
            return pb.EventBatch(next_cursor=cursor)
        return pb.EventBatch(
            events=[
                pb.GameEvent(
                    seq=cursor + 1,
                    game_id=game_id,
                    type=self.event_type,
                    player=self.event_player,
                    card_name=self.card_name,
                )
            ],
            next_cursor=cursor + 1,
        )


class FakeSpinClient(FakeWitnessClient):
    """A no-op spin: stable structural signature, only event counters grow.

    Every PRIORITY request offers a ``kind == "pass"`` option, so without the
    spin guard the policy would keep taking the activate option forever.  The
    ``state_hash`` varies per sample so the run is not a degenerate-loop false
    positive; it is the *signature* + game-state resources that stay put.
    """

    def __init__(self):
        super().__init__()
        self._polls = 0
        self._state_calls = 0
        self.submitted: list = []

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
                pb.Option(id=0, kind="activate", card_name="A"),
                pb.Option(id=1, kind="pass", description="Pass"),
            ],
        )

    def get_state(self, game_id, view_as_player=0):
        self.view_players.append(view_as_player)
        self._state_calls += 1
        # Same board/mana/tokens every sample; only ``casts`` (events) grows.
        return make_state(mana=5, hash_=f"SPIN{self._state_calls}")

    def poll_events(self, game_id, cursor=0):
        self._polls += 1
        if self._polls == 1:  # cursor-establishment poll
            return pb.EventBatch(next_cursor=cursor)
        return pb.EventBatch(
            events=[
                pb.GameEvent(
                    seq=cursor + 1,
                    game_id=game_id,
                    type="SpellCast",
                    player=0,
                    card_name="Unrelated",
                )
            ],
            next_cursor=cursor + 1,
        )

    def submit_decision(self, game_id, decision_id, answer):
        self.submitted.append(answer)
        return super().submit_decision(game_id, decision_id, answer)


class FakeProgressClient(FakeWitnessClient):
    """Genuine progress: a game-state resource (mana) grows every sample.

    Pass options are offered on every request, so a test can assert the guard
    does *not* take one when the run is really advancing.
    """

    def __init__(self):
        super().__init__()
        self.submitted: list = []

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
                pb.Option(id=0, kind="activate", card_name="A"),
                pb.Option(id=1, kind="pass", description="Pass"),
            ],
        )

    def submit_decision(self, game_id, decision_id, answer):
        self.submitted.append(answer)
        return super().submit_decision(game_id, decision_id, answer)


class FakePhaseBoundaryClient(FakeWitnessClient):
    """Policy completes one iteration, then stalls; a Phase event fires after.

    The first two submitted decisions offer the link options so the policy
    completes exactly one iteration (the wrap).  After that the only option
    matches no link, so the policy stops completing iterations.  A single
    ``Phase`` event is armed for the poll after the next decision request —
    the phase/turn-boundary poll the driver added.
    """

    def __init__(self, *, duplicate: bool = False):
        super().__init__()
        self.duplicate = bool(duplicate)
        self.submits = 0
        self._emit_phase = False
        self._phase_emitted = False

    def get_decision(self, game_id):
        if self.submits >= 2 and not self._emit_phase and not self._phase_emitted:
            # Two link decisions have been answered (one completed iteration);
            # arm the Phase event for the upcoming boundary poll.
            self._emit_phase = True
        if self.submits < 2:
            return super().get_decision(game_id)
        # No link option is offered any more: the policy cannot advance.
        self.decision_seq += 1
        return pb.DecisionRequest(
            game_id=game_id,
            decision_id=self.decision_seq,
            player=0,
            turn=1,
            phase="Main1",
            decision_type=pb.DECISION_TYPE_PRIORITY,
            options=[pb.Option(id=0, kind="activate", card_name="C")],
        )

    def submit_decision(self, game_id, decision_id, answer):
        self.submits += 1
        return super().submit_decision(game_id, decision_id, answer)

    def poll_events(self, game_id, cursor=0):
        if self._emit_phase and not self._phase_emitted:
            self._phase_emitted = True
            return pb.EventBatch(
                events=[
                    pb.GameEvent(
                        seq=cursor + 1,
                        game_id=game_id,
                        type="Phase",
                        turn=1,
                        phase="Combat",
                        player=0,
                    )
                ],
                next_cursor=cursor + 1,
            )
        return super().poll_events(game_id, cursor)

    def get_state(self, game_id, view_as_player=0):
        self.view_players.append(view_as_player)
        if self.duplicate:
            # Bit-identical to the previous sample on purpose: the phase
            # boundary must be dropped by the duplicate guard.
            return make_state(mana=2, hash_="HCONST")
        state = make_state(mana=self.mana, hash_=f"H{self.mana}")
        if self._phase_emitted:
            # The engine advanced a phase: a genuinely changed state.
            state.phase = "Combat"
            state.state_hash = f"H{self.mana}-combat"
        return state


class FakePhaseThenIterationClient(FakeWitnessClient):
    """A phase sample is followed by a policy iteration with the same state.

    Decision 1 offers the link options (no completed iteration); decision 2
    offers no link option and fires the Phase event (the phase-boundary
    sample); decision 3 offers the link options and completes an iteration
    without changing the state.  The duplicate guard must drop that policy
    sample so the phase boundary leaves no identical successor (the live
    Keldon Overseer / Elven Raft-Steerer / Firbolg Flutist regression).
    """

    def __init__(self):
        super().__init__()
        self._submits = 0
        self._emit_phase = False
        self._phase_emitted = False

    def get_decision(self, game_id):
        if self._submits == 1 and not self._phase_emitted:
            self._emit_phase = True
        self.decision_seq += 1
        if self._submits == 1:
            options = [pb.Option(id=0, kind="activate", card_name="C")]
        else:
            options = [
                pb.Option(id=0, kind="activate", card_name="A"),
                pb.Option(id=1, kind="activate", card_name="B"),
            ]
        return pb.DecisionRequest(
            game_id=game_id,
            decision_id=self.decision_seq,
            player=0,
            turn=1,
            phase="Main1",
            decision_type=pb.DECISION_TYPE_PRIORITY,
            options=options,
        )

    def submit_decision(self, game_id, decision_id, answer):
        self._submits += 1
        return super().submit_decision(game_id, decision_id, answer)

    def poll_events(self, game_id, cursor=0):
        if self._emit_phase and not self._phase_emitted:
            self._phase_emitted = True
            return pb.EventBatch(
                events=[
                    pb.GameEvent(
                        seq=cursor + 1,
                        game_id=game_id,
                        type="Phase",
                        turn=1,
                        phase="Combat",
                        player=0,
                    )
                ],
                next_cursor=cursor + 1,
            )
        return super().poll_events(game_id, cursor)

    def get_state(self, game_id, view_as_player=0):
        self.view_players.append(view_as_player)
        # Only the first submit changes the board; every later sample is
        # bit-identical (hash frozen at H1).
        return make_state(mana=0, hash_="H0" if self._submits == 0 else "H1")


class TestSpinGuard:
    def _policy(self):
        # One link whose action is always offered: every decision completes an
        # iteration, so the driver samples an observation per decision.
        return WitnessPolicy(links=[LinkPlan("A", "B")], player=0)

    def test_spin_forces_bounded_pass(self):
        client = FakeSpinClient()
        result = run_witness(
            client,
            build_scenario(combo_ab()),
            self._policy(),
            seeds=[1],
            max_iterations=40,
            max_decisions=40,
        )
        # The policy itself always answers with option 0 ("A"); the guard must
        # replace that with the pass option 1 once the spin threshold is hit.
        assert ("option_id", 0) in client.submitted
        assert ("option_id", 1) in client.submitted
        # First forced pass comes after SPIN_THRESHOLD spinned samples (the
        # baseline plus per-iteration samples), never earlier.
        assert client.submitted.index(("option_id", 1)) >= SPIN_THRESHOLD
        # Interventions are bounded, and the count is reported in the evidence.
        assert result.evidence["forced_passes"] == MAX_FORCED_PASSES
        assert (
            sum(1 for answer in client.submitted if answer == ("option_id", 1))
            == MAX_FORCED_PASSES
        )

    def test_real_progress_never_forces_pass(self):
        client = FakeProgressClient()
        result = run_witness(
            client,
            build_scenario(combo_ab()),
            self._policy(),
            seeds=[1],
            max_iterations=6,
            max_decisions=12,
        )
        # Mana grows between every pair of samples, so this is progress, not a
        # spin: the guard must never take the offered pass option.
        assert ("option_id", 1) not in client.submitted
        assert result.evidence["forced_passes"] == 0
        assert any(
            delta.get("mana", 0) > 0 for delta in result.resource_deltas
        )


class TestRunWitness:
    def _policy(self):
        return WitnessPolicy(links=[LinkPlan("A", "B"), LinkPlan("B", "A")], player=0)

    def test_growing_run_reports_loops_and_stops_game(self):
        client = FakeWitnessClient()
        scenario = build_scenario(combo_ab())
        result = run_witness(
            client, scenario, self._policy(), seeds=[1], max_iterations=3
        )
        assert result.verdict == "loops"
        assert result.iterations == 3
        # One baseline ("before") observation plus one per completed iteration.
        assert len(result.observations) == 4
        assert len(set(result.signatures)) == 1  # structure recurs
        assert result.resource_deltas[-1]["mana"] > 0
        assert result.state_hash_before == "H0"
        assert client.stopped == 77
        assert client.setup_calls and client.setup_calls[0] is scenario

    def test_single_iteration_records_before_and_after_but_cannot_loop(self):
        client = FakeWitnessClient()
        result = run_witness(
            client, build_scenario(combo_ab()), self._policy(), seeds=[1],
            max_iterations=1,
        )
        # A single completed iteration cannot prove a loop: the pre-iteration
        # baseline and the first post-iteration sample must never be compared.
        # That pair is exactly the live Keldon Overseer / Elven Raft-Steerer
        # false positive that this rule removes.
        assert result.verdict == "inconclusive"
        assert result.iterations == 1
        # One baseline ("before") + one completed-iteration ("after") sample.
        assert len(result.observations) == 2

    def test_unexecutable_line_is_inconclusive_with_diagnostic(self):
        client = FakeWitnessClient()
        # No link source is ever offered: the policy cannot execute the line.
        policy = WitnessPolicy(links=[LinkPlan("Never", "Offered")], player=0)
        result = run_witness(
            client, build_scenario(combo_ab()), policy, seeds=[1],
            max_iterations=1, max_decisions=3,
        )
        assert result.verdict == "inconclusive"
        assert "never matched" in result.evidence["reason"]
        assert result.evidence["diagnostics"]["executed_actions"] == 0

    def test_trigger_event_credits_executed_actions(self):
        # A link source that is only ever broadcast as a trigger (never offered
        # at PRIORITY) must still count as executed, so the "no loop action was
        # executed" gate does not force ``inconclusive``.
        client = FakeEventClient(card_name="TriggerCard", player=0)
        policy = WitnessPolicy(links=[LinkPlan("TriggerCard", "B")], player=0)
        result = run_witness(
            client, build_scenario(combo_ab()), policy, seeds=[1],
            max_iterations=1, max_decisions=2,
        )
        diagnostics = result.evidence["diagnostics"]
        assert diagnostics["trigger_hits"] == [1]
        assert diagnostics["link_hits"] == [0]
        assert diagnostics["executed_actions"] > 0
        assert "never matched" not in result.evidence.get("reason", "")

    def test_opponent_trigger_event_does_not_credit(self):
        client = FakeEventClient(card_name="TriggerCard", player=1)
        policy = WitnessPolicy(links=[LinkPlan("TriggerCard", "B")], player=0)
        result = run_witness(
            client, build_scenario(combo_ab()), policy, seeds=[1],
            max_iterations=1, max_decisions=2,
        )
        diagnostics = result.evidence["diagnostics"]
        assert diagnostics["trigger_hits"] == [0]
        assert diagnostics["executed_actions"] == 0

    def test_repeated_trigger_yields_post_baseline_observations(self):
        # A trigger-driven line whose policy never completes an iteration must
        # still advance observation boundaries, so the judge is not left with a
        # single baseline sample and the "need at least two post-baseline
        # observations" reason.
        client = FakeRepeatingEventClient(card_name="TriggerCard", player=0)
        policy = WitnessPolicy(links=[LinkPlan("TriggerCard", "B")], player=0)
        result = run_witness(
            client, build_scenario(combo_ab()), policy, seeds=[1],
            max_iterations=3, max_decisions=3,
        )
        # The policy never completed a line iteration; only trigger boundaries
        # advanced the run.
        assert policy.iterations == 0
        assert result.iterations >= 2
        assert len(result.observations) >= 2
        assert (
            "need at least two post-baseline observations"
            not in result.evidence.get("reason", "")
        )
        assert result.evidence["diagnostics"]["trigger_hits"][0] >= 2

    def test_pregame_drive_answers_mulligans_then_injects_once(self):
        client = FakePregameClient(mulligans=3)
        scenario = build_scenario(combo_ab())
        result = run_witness(
            client, scenario, self._policy(), seeds=[1], max_iterations=2
        )
        # The pre-game decisions were answered with the default keep-hands
        # policy (boolean_answer=True), never with the witness line policy.
        assert client.answers[:3] == [("boolean_answer", True)] * 3
        # Three keeps, then the first PRIORITY decision, then injection — and
        # the scenario is injected exactly once.
        assert client.sequence[:5] == [
            "mulligan",
            "mulligan",
            "mulligan",
            "priority",
            "setup",
        ]
        assert client.sequence.count("setup") == 1
        assert client.setup_calls == [scenario]
        assert result.verdict != "error"
        assert client.stopped == 77

    def test_endless_pregame_window_fails_cleanly(self):
        client = FakeEndlessPregameClient()
        result = run_witness(
            client, build_scenario(combo_ab()), self._policy(), seeds=[1]
        )
        assert result.verdict == "error"
        assert "pre-game window did not reach" in result.error
        # The bound is deterministic and injection never happened.
        assert client.decision_seq == MAX_PREGAME_DECISIONS
        assert client.setup_calls == []
        assert client.stopped == 77

    def test_relative_deck_paths_are_resolved_to_absolute(self):
        client = FakeWitnessClient()
        captured: dict = {}

        def spy_start_game(decks, seed, player_types=None, **kwargs):
            captured["decks"] = decks
            return client.game_id

        client.start_game = spy_start_game  # type: ignore[method-assign]
        run_witness(
            client,
            build_scenario(combo_ab()),
            self._policy(),
            seeds=[1],
            max_iterations=1,
            decks=[("a", "decks/a.dck"), ("b", "decks/b.dck")],
        )
        paths = [path for _, path in captured["decks"]]
        assert paths == [
            os.path.abspath("decks/a.dck"),
            os.path.abspath("decks/b.dck"),
        ]
        assert all(os.path.isabs(path) for path in paths)

    def test_setup_failure_reports_error(self):
        client = FakeWitnessClient()

        def boom(game_id, scenario):
            raise GameNotActiveError("requires an outstanding decision")

        client.setup_scenario = boom
        result = run_witness(
            client, build_scenario(combo_ab()), self._policy(), seeds=[1], max_iterations=2
        )
        assert result.verdict == "error"
        assert "requires an outstanding decision" in result.error
        assert client.stopped == 77

    def test_persist_witness_roundtrip(self, tmp_path):
        from combo_discovery.store import ExperimentStore
        from combo_discovery.witness import persist_witness

        client = FakeWitnessClient()
        result = run_witness(
            client, build_scenario(combo_ab()), self._policy(), seeds=[1], max_iterations=2
        )
        store = ExperimentStore(tmp_path / "w.sqlite")
        run_id, result_id = persist_witness(
            store, result, engine_commit="deadbeef", proto_version=7
        )
        runs = store.witness_runs()
        results = store.witness_results(run_id)
        store.close()
        assert runs[0]["scenario_json"] == result.scenario.canonical_json()
        assert runs[0]["proto_version"] == 7
        assert results[0]["id"] == result_id
        assert results[0]["verdict"] == result.verdict
        assert results[0]["iterations"] == result.iterations

    def test_recorder_receives_every_observation_in_order(self):
        client = FakeWitnessClient()
        captured: list[Observation] = []
        result = run_witness(
            client, build_scenario(combo_ab()), self._policy(), seeds=[1],
            max_iterations=3, recorder=captured.append,
        )
        # One synchronous call per captured observation, in capture order: the
        # pre-loop baseline (0) then one per completed iteration (1..3).
        assert len(captured) == len(result.observations)
        assert [o.iteration for o in captured] == [0, 1, 2, 3]
        assert [o.iteration for o in captured] == [
            o.iteration for o in result.observations
        ]
        # Each recorded observation carries the state's phase (v7 field).
        assert all(o.phase == "Main1" for o in captured)
        assert all(o.signature for o in captured)

    def test_recorder_none_does_not_change_behaviour(self):
        client = FakeWitnessClient()
        with_recorder: list[Observation] = []
        run_witness(
            client, build_scenario(combo_ab()), self._policy(), seeds=[1],
            max_iterations=3, recorder=with_recorder.append,
        )
        plain = run_witness(
            client, build_scenario(combo_ab()), self._policy(), seeds=[1],
            max_iterations=3,
        )
        assert [o.iteration for o in with_recorder] == [
            o.iteration for o in plain.observations
        ]


class TestPersistObservations:
    def test_persist_observations_appends_end_of_run(self, tmp_path):
        from combo_discovery.store import ExperimentStore
        from combo_discovery.witness import persist_observations

        client = FakeWitnessClient()
        result = run_witness(
            client, build_scenario(combo_ab()), 
            WitnessPolicy(links=[LinkPlan("A", "B"), LinkPlan("B", "A")], player=0),
            seeds=[1], max_iterations=2,
        )
        store = ExperimentStore(tmp_path / "w.sqlite")
        run_id = store.start_witness_run(seeds=[1])
        written = persist_observations(store, run_id, result)
        rows = store.witness_observations(run_id)
        store.close()
        assert written == len(result.observations) == 3
        assert [r["iteration"] for r in rows] == [0, 1, 2]
        assert [r["phase"] for r in rows] == ["Main1", "Main1", "Main1"]

    def test_recorder_then_persist_observations_does_not_duplicate(self, tmp_path):
        from combo_discovery.store import ExperimentStore
        from combo_discovery.witness import (
            observation_recorder,
            persist_observations,
        )

        store = ExperimentStore(tmp_path / "w.sqlite")
        run_id = store.start_witness_run(seeds=[1])
        result = run_witness(
            FakeWitnessClient(), build_scenario(combo_ab()),
            WitnessPolicy(links=[LinkPlan("A", "B"), LinkPlan("B", "A")], player=0),
            seeds=[1], max_iterations=2,
            recorder=observation_recorder(store, run_id),
        )
        written = persist_observations(store, run_id, result)
        rows = store.witness_observations(run_id)
        store.close()
        assert written == 0
        assert len(rows) == len(result.observations) == 3


class TestPhaseBoundarySampling:
    """The driver samples phase/turn boundaries even after the policy stalls."""

    def _policy(self):
        return WitnessPolicy(links=[LinkPlan("A", "B"), LinkPlan("B", "A")], player=0)

    def test_phase_boundary_after_policy_stops_is_sampled(self):
        client = FakePhaseBoundaryClient()
        policy = self._policy()
        result = run_witness(
            client, build_scenario(combo_ab()), policy, seeds=[1],
            max_iterations=4, max_decisions=4,
        )
        # The policy completed exactly one iteration and then stalled: the old
        # driver left the judge with a single post-baseline sample.
        assert policy.iterations == 1
        # Baseline + the completed iteration + the phase-boundary sample.
        assert len(result.observations) == 3
        assert result.iterations == 2
        # A verdict was reached instead of the "need at least two post-baseline
        # observations" bail-out.
        assert (
            "need at least two post-baseline observations"
            not in result.evidence.get("reason", "")
        )
        # The phase boundary changed the board, so no signature recurs.
        assert result.verdict == "no_loop"

    def test_phase_boundary_duplicate_hash_is_skipped(self):
        client = FakePhaseBoundaryClient(duplicate=True)
        result = run_witness(
            client, build_scenario(combo_ab()), self._policy(), seeds=[1],
            max_iterations=4, max_decisions=4,
        )
        # The Phase event fired, but its sample repeated the previous
        # state_hash and was dropped: only the baseline and the completed
        # iteration remain, so the judge cannot certify a degenerate loop.
        assert len(result.observations) == 2
        assert result.verdict == "inconclusive"
        assert "need at least two post-baseline observations" in result.evidence["reason"]

    def test_iteration_repeating_a_phase_sample_is_skipped(self):
        # Regression for the live false positives (Keldon Overseer / Elven
        # Raft-Steerer / Firbolg Flutist): a phase-boundary sample must not be
        # immediately followed by a policy-iteration sample with the same
        # state_hash, which detect_loop's degenerate branch would certify as a
        # loop.  (A policy sample repeating an earlier *policy* sample is left
        # untouched, which is what the genuine Kiki-Jiki loop relies on.)
        client = FakePhaseThenIterationClient()
        policy = self._policy()
        result = run_witness(
            client, build_scenario(combo_ab()), policy, seeds=[1],
            max_iterations=4, max_decisions=4,
        )
        assert policy.iterations == 1
        assert len(result.observations) == 2
        assert result.verdict != "loops"
        assert result.verdict == "inconclusive"


# ---------------------------------------------------------------------------
# Deck path resolution
# ---------------------------------------------------------------------------


class TestDeckPaths:
    def test_relative_path_resolved_against_cwd(self):
        resolved = _absolute_deck_paths([("a", "decks/a.dck")])
        assert resolved == [("a", os.path.abspath("decks/a.dck"))]
        assert os.path.isabs(resolved[0][1])

    def test_absolute_path_is_unchanged(self):
        already = os.path.abspath("decks/goldfish_A.dck")
        assert _absolute_deck_paths([("a", already)]) == [("a", already)]


# ---------------------------------------------------------------------------
# Link-plan helpers
# ---------------------------------------------------------------------------


class TestLinkHelpers:
    def test_link_plans_from_combo(self):
        plans = link_plans(combo_ab())
        assert [(p.src, p.dst) for p in plans] == [("Altar", "Ghost"), ("Ghost", "Altar")]

    def test_link_plans_accepts_dicts(self):
        plans = link_plans([{"src": "A", "dst": "B", "kind": "enables"}])
        assert plans[0].src == "A" and plans[0].kind == "enables"

    def test_synthetic_cycle(self):
        plans = synthetic_cycle(["A", "B", "C"])
        assert [(p.src, p.dst) for p in plans] == [("A", "B"), ("B", "C"), ("C", "A")]
        assert synthetic_cycle(["A"]) == []
