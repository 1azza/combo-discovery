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
    LinkPlan,
    MAX_PREGAME_DECISIONS,
    Observation,
    PlayerScenario,
    Scenario,
    WitnessPolicy,
    _absolute_deck_paths,
    build_observation,
    build_scenario,
    detect_loop,
    link_plans,
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

    def test_degenerate_identical_hash_is_loop(self):
        observations = [
            Observation(iteration=0, signature="s1", state_hash="BASE"),
            Observation(iteration=1, signature="s2", state_hash="SAME"),
            Observation(iteration=2, signature="s3", state_hash="SAME"),
        ]
        verdict, evidence = detect_loop(observations)
        assert verdict == "loops"
        assert evidence["kind"] == "degenerate"
        assert evidence["pair"] == [1, 2]

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
