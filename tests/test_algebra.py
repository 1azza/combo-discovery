"""Interaction-algebra acceptance tests.

Hermetic: a mini corpus is built from the vendored real Forge scripts in
``tests/fixtures/ontology_cards`` (falling back to the live Forge checkout when
present), then signatures / links / cycles are exercised directly.  No network,
no full corpus, no writes to ``research.db``.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from combo_discovery.corpus.importer import import_corpus, normalize_name
from combo_discovery.corpus.names import (
    is_non_vintage_printing,
    is_only_unset_printing,
    is_unset,
)
from combo_discovery.corpus.spellbook import (
    DEFAULT_FORGE_EDITIONS,
    DEFAULT_VINTAGE_FORMAT,
    VintageLegality,
    load_forge_editions,
)
from combo_discovery.ontology.budget import (
    BudgetExceeded,
    SearchBudget,
    preflight,
)
from combo_discovery.ontology.builder import _combo_cards_legal
from combo_discovery.ontology.cycles import (
    ComboList,
    _evidence_factor,
    _gate_preconditions,
    _motif_factor,
    build_graph,
    find_combos,
    load_motif_weights,
    tight_pool,
    vintage_pool,
)
from combo_discovery.ontology.extractor import CardContext, load_contexts
from combo_discovery.ontology.links import (
    _COPY_RE_TRIGGER_KINDS,
    _RE_TRIGGER_LISTENER_KINDS,
    LinkOptions,
    copy_accepts,
    link_enables,
    link_hostile,
    link_re_trigger,
    ports_enable,
)
from combo_discovery.ontology.ports import AbilitySig, Port, build_signatures
from combo_discovery.ontology.queries import (
    QUERIES,
    ComboContext,
    get_query,
    iter_queries,
)
from combo_discovery.ontology.restrictions import parse_restriction
from combo_discovery.store import ExperimentStore

FIXTURES = Path(__file__).parent / "fixtures" / "ontology_cards"
FORGE_CARDSFOLDER = Path("/home/lza/Work/forge/forge-gui/res/cardsfolder")


# ---------------------------------------------------------------------------
# Mini-corpus fixture
# ---------------------------------------------------------------------------


def _seed_cardsfolder(root: Path) -> Path:
    folder = root / "forge-gui" / "res" / "cardsfolder"
    for fixture in sorted(FIXTURES.glob("*.txt")):
        real = FORGE_CARDSFOLDER / fixture.name[0] / fixture.name
        source = real if real.is_file() else fixture
        target = folder / fixture.name[0] / fixture.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    return root


@pytest.fixture(scope="module")
def algebra(tmp_path_factory) -> dict:
    root = _seed_cardsfolder(tmp_path_factory.mktemp("algebra"))
    store = ExperimentStore(root / "research.db")
    try:
        import_corpus(store, root)
        import_id = store._conn.execute(
            "SELECT import_id FROM import_runs ORDER BY rowid DESC LIMIT 1"
        ).fetchone()[0]
        contexts, effects = load_contexts(store._conn, import_id)
        sigs = build_signatures(contexts, effects)
        weights = load_motif_weights(store._conn)
    finally:
        store.close()
    by_name: dict[str, list[AbilitySig]] = {}
    for sig in sigs:
        by_name.setdefault(sig.card_name, []).append(sig)
    return {"sigs": sigs, "by_name": by_name, "weights": weights}


def _combos_for(sigs, a: str, b: str, *, max_len: int = 4, weights=None):
    out = []
    for combo in find_combos(sigs, max_len=max_len, weights=weights):
        if set(combo.cards) == {a, b}:
            out.append(combo)
    return out


# ---------------------------------------------------------------------------
# Acceptance: real Scripts
# ---------------------------------------------------------------------------


class TestRealScripts:
    def test_kiki_exarch_is_an_infinite_etb_loop(self, algebra):
        combos = _combos_for(algebra["sigs"], "Kiki-Jiki, Mirror Breaker", "Deceiver Exarch")
        assert combos, "Kiki-Jiki + Deceiver Exarch was not detected"
        combo = combos[0]
        assert "infinite_etb_loop" in combo.patterns
        assert combo.preconditions == ()
        assert combo.infinite is True
        assert any(link.kind == "re_trigger" and link.subkind == "copy"
                   for link in combo.links)

    def test_kiki_bumi_land_untapper_is_not_a_link(self, algebra):
        by_name = algebra["by_name"]
        kiki = by_name["Kiki-Jiki, Mirror Breaker"]
        bumi = by_name["Bumi, Unleashed"]
        # Bumi's untap is on a combat-damage trigger over lands, so it neither
        # re-triggers nor can untap Kiki (a creature).
        assert all(link_re_trigger(e, b) is None for e in kiki for b in bumi)
        assert all(link_enables(b, e) is None for e in kiki for b in bumi)
        assert _combos_for(algebra["sigs"], "Kiki-Jiki, Mirror Breaker", "Bumi, Unleashed") == []

    def test_kiki_fomo_is_a_combat_loop_with_delirium_precondition(self, algebra):
        combos = _combos_for(algebra["sigs"], "Kiki-Jiki, Mirror Breaker",
                             "Fear of Missing Out")
        assert combos, "Kiki-Jiki + Fear of Missing Out was not detected"
        combo = combos[0]
        assert "combat_loop" in combo.patterns
        assert "infinite_etb_loop" not in combo.patterns
        assert any("delirium" in pre for pre in combo.preconditions)
        # FirstAttack is discharged by the copy engine (a fresh token each loop),
        # so it must NOT be carried as a precondition.
        assert not any("first_attack" in pre for pre in combo.preconditions)

    def test_legendary_copy_restriction(self, algebra):
        by_name = algebra["by_name"]
        kiki = by_name["Kiki-Jiki, Mirror Breaker"]
        derevi = by_name["Derevi, Empyrial Tactician"]
        twin = by_name["Splinter Twin"]
        # Kiki's copy restriction is nonLegendary; Derevi is legendary.
        assert all(link_re_trigger(e, d) is None for e in kiki for d in derevi)
        assert _combos_for(algebra["sigs"], "Kiki-Jiki, Mirror Breaker",
                           "Derevi, Empyrial Tactician") == []
        # Splinter Twin copies its host (Defined Self), which only requires a creature.
        links = [link_re_trigger(t, d) for t in twin for d in derevi]
        assert any(link is not None for link in links)

    def test_kiki_grizzly_no_cycle(self, algebra):
        assert _combos_for(algebra["sigs"], "Kiki-Jiki, Mirror Breaker", "Grizzly Bears") == []

    def test_kiki_known_partners_form_cycles(self, algebra):
        for partner in ("Deceiver Exarch", "Pestermite", "Corridor Monitor",
                        "Zealous Conscripts"):
            combos = _combos_for(algebra["sigs"], "Kiki-Jiki, Mirror Breaker", partner)
            assert combos, f"Kiki-Jiki + {partner} was not detected"
            assert "infinite_etb_loop" in combos[0].patterns


# ---------------------------------------------------------------------------
# Synthetic helpers
# ---------------------------------------------------------------------------


def _context(card_id: int, name: str, type_line: str, mana: str = "1") -> CardContext:
    return CardContext(card_id, name, normalize_name(name), mana, type_line, "", "", ())


def _sig(
    card_id: int,
    name: str,
    ref: str,
    *,
    type_line: str = "Creature",
    kind: str = "ability",
    trigger: Port | None = None,
    consumes: tuple[Port, ...] = (),
    produces: tuple[Port, ...] = (),
    gates: tuple[Port, ...] = (),
) -> AbilitySig:
    return AbilitySig(
        card_id=card_id,
        card_name=name,
        ability_ref=ref,
        ability_kind=kind,
        triggers_on=trigger,
        consumes=consumes,
        produces=produces,
        gates=gates,
        raw={"context": _context(card_id, name, type_line)},
    )


def _copy_port(restriction: str | None = None, defined: str = "") -> Port:
    spec = parse_restriction(restriction).to_dict() if restriction else parse_restriction("").to_dict()
    return Port("copy_permanent", {
        "restriction": spec, "defined": defined, "predicate": "COPIES_CREATURE",
    })


def _untap_port(restriction: str) -> Port:
    return Port("untap", {
        "restriction": parse_restriction(restriction).to_dict(),
        "predicate": "UNTAPS",
    })


def _token_port(types: tuple[str, ...] = ("CREATURE",)) -> Port:
    return Port("token", {"types": types, "predicate": "CREATES_TOKEN"})


def _mana_port(colors: tuple[str, ...]) -> Port:
    return Port("mana", {"colors": colors, "predicate": "PRODUCES_MANA"})


def _three_card_cycle() -> list[AbilitySig]:
    maker = _sig(
        1, "Token Maker", "line:0:1:T:ChangesZone", kind="trigger",
        trigger=Port("enters_battlefield", {"self": True, "predicate": "ETB_TRIGGER"}),
        produces=(_token_port(),),
    )
    altar = _sig(2, "Altar", "line:0:2:A:Sacrifice", type_line="Artifact",
                 consumes=(Port("sacrifice", {
                     "self": False,
                     "restriction": parse_restriction("Creature").to_dict(),
                     "predicate": "SACRIFICE_OUTLET",
                 }),),
                 produces=(_mana_port(("B",)),))
    reanimator = _sig(
        3, "Reanimator", "line:0:3:A:ChangeZone",
        consumes=(Port("mana", {"colors": ("B",), "predicate": "PRODUCES_MANA"}),),
        produces=(Port("zone_move", {"from": "Graveyard", "to": "Battlefield",
                                     "predicate": "RECURS_FROM_GRAVEYARD"}),),
    )
    return [maker, altar, reanimator]


# ---------------------------------------------------------------------------
# Unit: structural matching
# ---------------------------------------------------------------------------


class TestStructuralMatching:
    def test_mana_superset(self):
        producer = _sig(1, "Producer", "a", produces=(_mana_port(("U", "R")),))
        consumer = _sig(2, "Consumer", "b",
                        consumes=(Port("mana", {"colors": ("U",), "predicate": "PRODUCES_MANA"}),))
        assert ports_enable(producer.produces[0], consumer.consumes[0], consumer) is True
        wrong = _sig(3, "Wrong", "c",
                     consumes=(Port("mana", {"colors": ("G",), "predicate": "PRODUCES_MANA"}),))
        assert ports_enable(producer.produces[0], wrong.consumes[0], wrong) is False
        # A producer that makes X can pay any coloured cost.
        any_producer = _sig(4, "Any", "d",
                            produces=(Port("mana", {"colors": ("W",), "x": True,
                                                    "predicate": "PRODUCES_MANA"}),))
        assert ports_enable(any_producer.produces[0], wrong.consumes[0], wrong) is True

    def test_controller_and_legendary_compatibility(self):
        engine = _sig(1, "Engine", "a", type_line="Creature",
                      produces=(_copy_port("Creature.nonLegendary+YouCtrl"),))
        plain = _sig(2, "Plain Buddy", "b", type_line="Creature Human")
        legendary = _sig(3, "Legend Buddy", "c", type_line="Legendary Creature Human")
        assert copy_accepts(engine.produces[0], plain) is True
        assert copy_accepts(engine.produces[0], legendary) is False
        # Defined Self only requires a creature.
        assert copy_accepts(_copy_port(defined="Self"), plain) is True
        assert copy_accepts(_copy_port(defined="Self"), legendary) is True
        # An opponent-controller restriction is not satisfiable same-player.
        assert ports_enable(
            Port("untap", {"restriction": parse_restriction("Creature.OppCtrl").to_dict(),
                           "predicate": "UNTAPS"}),
            Port("tap", {"self": True, "predicate": "TAPS_COST"}),
            plain,
        ) is False

    def test_gate_propagation_first_attack_vs_delirium(self):
        engine = _sig(1, "Engine", "a", type_line="Legendary Creature",
                      consumes=(Port("tap", {"self": True, "predicate": "TAPS_COST"}),),
                      produces=(_copy_port("Creature.nonLegendary+YouCtrl"),))
        holder = _sig(
            2, "Attacker", "b", type_line="Creature",
            kind="trigger",
            trigger=Port("attacks", {"self": True, "first": True, "predicate": "ATTACKS"}),
            produces=(_untap_port("Creature"),),
            gates=(Port("first_attack", {"raw": "True", "predicate": "FIRST_ATTACK"}),
                   Port("delirium", {"raw": "True", "predicate": "DELIRIUM"})),
        )
        # Fresh token satisfies FirstAttack; nothing external discharges delirium.
        assert _gate_preconditions([engine, holder]) == ("Attacker:delirium",)

    def test_hostile_suppression(self):
        cycle = _three_card_cycle()
        assert link_hostile(cycle[0], cycle[1], None) is None
        hostile = link_hostile(
            cycle[0], cycle[1], {"CREATES_TOKEN~SACRIFICE_OUTLET": 0.1}
        )
        assert hostile is not None and hostile.kind == "hostile"
        # The cycle survives neutral weights but is suppressed when hostile.
        assert find_combos(cycle, max_len=3)
        assert find_combos(cycle, max_len=3,
                           weights={"CREATES_TOKEN~SACRIFICE_OUTLET": 0.1}) == []

    def test_enrichment_weights_change_the_score(self):
        cycle = _three_card_cycle()
        base = find_combos(cycle, max_len=3)[0]
        boosted = find_combos(cycle, max_len=3, weights={"CREATES_TOKEN~SACRIFICE_OUTLET": 2.0})[0]
        penalised = find_combos(cycle, max_len=3,
                                weights={"CREATES_TOKEN~SACRIFICE_OUTLET": 0.8})[0]
        # Score is monotone in the enrichment weight.  It is now a bounded
        # (count-normalized) motif factor rather than a raw product, so the
        # exact old ratio no longer holds, but the ordering must.
        assert penalised.score < base.score < boosted.score
        assert 0.0 < penalised.score <= 1.0
        assert 0.0 < boosted.score <= 1.0
        assert base.motifs and "CREATES_TOKEN~SACRIFICE_OUTLET" in base.motifs


# ---------------------------------------------------------------------------
# Unit: cycles, queries, determinism
# ---------------------------------------------------------------------------


class TestCyclesAndQueries:
    def test_three_card_cycle_needs_max_len(self):
        cycle = _three_card_cycle()
        assert find_combos(cycle, max_len=2) == []
        combos = find_combos(cycle, max_len=3)
        assert combos, "3-card cycle was not detected"
        assert set(combos[0].cards) == {"Token Maker", "Altar", "Reanimator"}
        assert any(link.kind == "re_trigger" for link in combos[0].links)

    def test_queries_are_listable_and_named(self):
        names = [q.name for q in iter_queries()]
        assert names == [
            "infinite_etb_loop", "combat_loop", "sacrifice_loop",
            "mana_loop", "any_cycle",
        ]
        for query in QUERIES:
            assert query.description and isinstance(query.rule, dict)
            assert get_query(query.name) is query

    def test_deterministic_output(self):
        cycle = _three_card_cycle()
        first = find_combos(cycle, max_len=3)
        second = find_combos(cycle, max_len=3)
        assert [c.as_dict() for c in first] == [c.as_dict() for c in second]
        # build_graph link ordering is deterministic too.
        g1 = build_graph(cycle)
        g2 = build_graph(cycle)
        assert [l.key() for l in g1.links] == [l.key() for l in g2.links]

    def test_graph_is_card_level_with_ability_links(self, algebra):
        graph = build_graph(algebra["sigs"])
        assert graph.nodes
        assert graph.re_triggers
        for link in graph.links:
            assert link.src.card_id in graph.by_card
            assert link.dst.card_id in graph.by_card

    def test_load_motif_weights_without_run_is_empty(self, tmp_path):
        db = tmp_path / "empty.db"
        ExperimentStore(db).close()
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        try:
            assert load_motif_weights(conn) == {}
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Hard budgets + cost pre-flight
# ---------------------------------------------------------------------------


def _retrigger_cluster(engines: int = 40, listeners: int = 40) -> list[AbilitySig]:
    """A synthetic graph whose re-trigger fan-out is large enough to need a cap."""
    sigs: list[AbilitySig] = []
    tap = Port("tap", {"self": True, "predicate": "TAPS_COST"})
    etb = Port("enters_battlefield", {"self": True, "predicate": "ETB_TRIGGER"})
    for i in range(engines):
        sigs.append(_sig(1000 + i, f"Engine {i}", f"e{i}",
                         consumes=(tap,), produces=(_copy_port("Creature"),)))
    for j in range(listeners):
        sigs.append(_sig(2000 + j, f"Listener {j}", f"l{j}", kind="trigger",
                         trigger=etb, produces=(_untap_port("Creature"),)))
    return sigs


class TestBudgetsAndPreflight:
    def test_preflight_clamps_closure_depth(self):
        with pytest.warns(RuntimeWarning, match="clamped"):
            depth, messages = preflight(100, 5)
        assert depth == 2
        assert messages

    def test_preflight_refuses_large_pool_unless_overridden(self):
        with pytest.raises(BudgetExceeded):
            preflight(9_000, 2)
        with pytest.warns(RuntimeWarning, match="safe limit"):
            depth, messages = preflight(9_000, 2, allow_over_budget=True)
        assert depth == 2
        assert messages

    def test_safe_link_options_are_tight(self):
        options = LinkOptions.safe()
        assert options.scope == "retrigger"
        assert options.closure_depth == 2
        assert options.max_enables is not None

    def test_pathological_config_truncates_instead_of_hanging(self):
        sigs = _retrigger_cluster()
        combos = find_combos(sigs, max_len=3, options=LinkOptions.safe(),
                             max_seconds=10, max_steps=50)
        assert isinstance(combos, ComboList)
        assert combos.truncated is True
        assert combos.truncation_reason is not None
        assert "max_steps" in combos.truncation_reason

    def test_build_graph_reports_partial_on_budget(self):
        sigs = _retrigger_cluster()
        graph = build_graph(sigs, options=LinkOptions.safe(),
                            max_seconds=10, max_steps=50)
        assert graph.truncated is True
        assert "max_steps" in (graph.truncation_reason or "")
        assert graph.budget_report and graph.budget_report["truncated"] is True

    def test_default_budget_completes_small_graph(self):
        combos = find_combos(_three_card_cycle(), max_len=3)
        assert combos
        assert combos.truncated is False
        assert combos.truncation_reason is None


# ---------------------------------------------------------------------------
# Tuning round: the four measured defects
# ---------------------------------------------------------------------------


class TestNonVintagePoolExclusion:
    """Fix 1: Alchemy (A-*) / Un-set names never enter the candidate pool."""

    def test_name_guards(self):
        assert is_non_vintage_printing("A-Goldspan Dragon") is True
        assert is_non_vintage_printing("A-Esika's Chariot") is True
        assert is_unset('"Name Sticker" Goblin') is True
        # A real card whose name merely starts with "A " is not Alchemy.
        assert is_non_vintage_printing("A Display of My Dark Power") is False
        assert is_non_vintage_printing("Nimble Birdsticker") is False

    def test_vintage_legality_excludes_alchemy_and_unset(self):
        legality = VintageLegality.permissive()
        assert legality.is_legal("A-Goldspan Dragon") is False
        assert legality.is_legal('"Name Sticker" Goblin') is False
        assert legality.is_legal("Grizzly Bears") is True

    def test_vintage_pool_drops_alchemy_ids(self):
        names = {1: "A-Goldspan Dragon", 2: "Grizzly Bears"}
        pool = vintage_pool([1, 2], names, VintageLegality.permissive())
        assert pool == (2,)

    def test_tight_pool_excludes_alchemy_loop_machinery(self):
        alchemy = _sig(1, "A-Goldspan Dragon", "a",
                       produces=(_copy_port("Creature"),))
        real = _sig(2, "Kiki-Jiki, Mirror Breaker", "b",
                    produces=(_copy_port("Creature"),))
        names = {1: "A-Goldspan Dragon", 2: "Kiki-Jiki, Mirror Breaker"}
        assert tight_pool([alchemy, real], names,
                          VintageLegality.permissive()) == (2,)

    # -- Un-set / novelty set codes (Fix 2) ---------------------------------

    def test_only_unset_printing_guard(self):
        assert is_only_unset_printing({"UST"}, {"UST", "UGL"}) is True
        # A legal reprint rescues the card.
        assert is_only_unset_printing({"UST", "LEA"}, {"UST", "UGL"}) is False
        # No known printing is never treated as "only unset".
        assert is_only_unset_printing(set(), {"UST"}) is False

    def test_vintage_legality_excludes_unset_only_cards(self):
        legality = VintageLegality(
            card_sets={"eager beaver": frozenset({"UST"})},
            unset_sets=frozenset({"UST"}),
        )
        # Eager Beaver has no name marker and is not in the banned list; the
        # set-code signal is the only thing that can see it.
        assert legality.is_legal("Eager Beaver") is False
        reprinted = VintageLegality(
            card_sets={"blacker lotus": frozenset({"UGL", "LEA"})},
            unset_sets=frozenset({"UGL"}),
        )
        assert reprinted.is_legal("Blacker Lotus") is True

    def test_vintage_pool_drops_unset_only_ids(self):
        legality = VintageLegality(
            card_sets={"eager beaver": frozenset({"UST"})},
            unset_sets=frozenset({"UST"}),
        )
        names = {1: "Eager Beaver", 2: "Grizzly Bears"}
        assert vintage_pool([1, 2], names, legality) == (2,)

    def test_tight_pool_excludes_unset_loop_machinery(self):
        eager = _sig(1, "Eager Beaver", "a",
                     produces=(_untap_port("Permanent"),))
        real = _sig(2, "Kiki-Jiki, Mirror Breaker", "b",
                    produces=(_copy_port("Creature"),))
        names = {1: "Eager Beaver", 2: "Kiki-Jiki, Mirror Breaker"}
        legality = VintageLegality(
            card_sets={"eager beaver": frozenset({"UST"})},
            unset_sets=frozenset({"UST"}),
        )
        assert tight_pool([eager, real], names, legality) == (2,)

    @pytest.mark.skipif(
        not DEFAULT_VINTAGE_FORMAT.is_file() or not DEFAULT_FORGE_EDITIONS.is_dir(),
        reason="Forge format/editions not present",
    )
    def test_real_forge_data_excludes_eager_beaver(self):
        legality = VintageLegality.from_forge_format(DEFAULT_VINTAGE_FORMAT)
        assert legality.is_legal("Eager Beaver") is False
        # A normal Vintage staple is untouched.
        assert legality.is_legal("Grizzly Bears") is True
        # The set-code map really was loaded from the editions tree.
        assert legality.card_sets
        assert "UST" in legality.unset_sets


class TestFinalizationGuard:
    """Fix: a proposed combo is dropped at finalization if any card is illegal.

    This is the algebra path's choke point (``_write_algebra``): the guard runs
    over the combo's card ids against the same ``VintageLegality`` the Spellbook
    resolver uses, so an explicit ``pool`` that bypassed the pool-level filter
    still cannot leak a non-Vintage card into ``combo_hypotheses``.
    """

    @staticmethod
    def _legality() -> VintageLegality:
        return VintageLegality(
            card_sets={
                "eager beaver": frozenset({"UST"}),
                "blacker lotus": frozenset({"UGL", "LEA"}),
                "grizzly bears": frozenset({"LEA"}),
            },
            unset_sets=frozenset({"UST", "UGL"}),
        )

    def test_rejects_unset_only_card(self):
        # (a) Eager Beaver has no name marker and is not banned; only the
        # set-code signal can see that its every printing is Unstable.
        names = {1: "Eager Beaver", 2: "Kiki-Jiki, Mirror Breaker"}
        assert _combo_cards_legal([1, 2], names, self._legality()) is False

    def test_accepts_normal_vintage_card(self):
        # (b) an ordinary Vintage-legal pair is untouched.
        names = {1: "Grizzly Bears", 2: "Kiki-Jiki, Mirror Breaker"}
        assert _combo_cards_legal([1, 2], names, self._legality()) is True

    def test_accepts_card_with_a_normal_reprint(self):
        # (c) Blacker Lotus is printed in UGL *and* LEA, so it is rescued.
        names = {1: "Blacker Lotus", 2: "Grizzly Bears"}
        assert _combo_cards_legal([1, 2], names, self._legality()) is True

    def test_name_level_guard_applies(self):
        names = {1: "A-Goldspan Dragon", 2: "Grizzly Bears"}
        assert _combo_cards_legal([1, 2], names, self._legality()) is False

    def test_none_legality_is_permissive(self):
        names = {1: "Eager Beaver", 2: "Grizzly Bears"}
        assert _combo_cards_legal([1, 2], names, None) is True


class TestScoreDiscrimination:
    """Fix 2: bounded score with real spread + evidence tie-breakers."""

    def test_score_is_bounded_and_structural_factors_are_interior(self):
        combo = find_combos(_three_card_cycle(), max_len=3)[0]
        assert 0.0 < combo.score <= 1.0
        fact = _motif_factor(combo.links, None)
        ev = _evidence_factor(combo.links, 0)
        assert 0.0 < fact <= 1.0
        assert 0.0 < ev < 1.0
        # A gate lowers the structural support.
        assert _evidence_factor(combo.links, 2) < ev

    def test_motif_factor_is_count_normalized(self):
        cycle = find_combos(_three_card_cycle(), max_len=3)[0]
        one = cycle.links[:1]
        assert _motif_factor(one, None) == _motif_factor(cycle.links, None)

    def test_scores_are_not_all_saturated(self):
        # Across a small synthetic cluster not every cycle should score 1.0.
        combos = find_combos(_retrigger_cluster(engines=6, listeners=6), max_len=2)
        assert combos
        assert any(combo.score < 1.0 for combo in combos)


class TestManaLoopTightening:
    """Fix 3: mana_loop requires a real closed mana link, not incidental mana."""

    def test_requires_closed_mana_link(self):
        producer = _sig(1, "Mana Rock", "a", type_line="Artifact",
                        produces=(_mana_port(("U",)),))
        sink = _sig(2, "Mana Sink", "b",
                    consumes=(Port("mana", {"colors": ("U",),
                                            "predicate": "PRODUCES_MANA"}),),
                    produces=(_untap_port("Creature"),))
        query = get_query("mana_loop")
        # Just "produces mana and consumes mana" no longer matches...
        assert query.matches(ComboContext(abilities=(producer, sink), links=())) is False
        # ...an explicit mana->mana enables link does.
        link = link_enables(producer, sink)
        assert link is not None
        assert query.matches(
            ComboContext(abilities=(producer, sink), links=(link,))
        ) is True


class TestPhaseTriggerHandling:
    """Fix 4: phase triggers are deliberately not copy re-trigger kinds."""

    def _engine(self):
        return _sig(
            1, "Kiki-Jiki, Mirror Breaker", "a",
            type_line="Legendary Creature Goblin",
            consumes=(Port("tap", {"self": True, "predicate": "TAPS_COST"}),),
            produces=(_copy_port("Creature.nonLegendary+YouCtrl"),),
        )

    def test_phase_is_not_a_copy_retrigger_kind(self):
        assert "phase" not in _COPY_RE_TRIGGER_KINDS
        assert "phase" not in _RE_TRIGGER_LISTENER_KINDS

    def test_phase_untapper_does_not_close_a_copy_loop(self):
        engine = self._engine()
        adventurer = _sig(
            2, "White Plume Adventurer", "b", kind="trigger",
            trigger=Port("phase", {"phase": "Upkeep", "predicate": "PHASE"}),
            produces=(_untap_port("Creature"),),
        )
        assert link_re_trigger(engine, adventurer) is None
        assert find_combos([engine, adventurer], max_len=3) == []

    def test_etb_untapper_still_closes_the_loop(self):
        engine = self._engine()
        exarch = _sig(
            2, "Deceiver Exarch", "b", kind="trigger",
            trigger=Port("enters_battlefield", {"self": True,
                                                "predicate": "ETB_TRIGGER"}),
            produces=(_untap_port("Creature"),),
        )
        assert link_re_trigger(engine, exarch) is not None
        combos = find_combos([engine, exarch], max_len=3)
        assert combos
