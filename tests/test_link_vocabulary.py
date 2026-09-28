"""Tests for the additive link-vocabulary extensions.

Covers the three graph-construction fixes:
* a fresh copy re-arms the copied card's tap-cost activated ability
  (``copy_activation``) and ``copy_permanent`` covers ``tap``;
* an Aura/Equipment's granted ability is matched against its host
  (``host_context``);
* reanimation/flicker listeners and non-rank-0 resource feeders close loops.

Hermetic: synthetic ability signatures only, no corpus, no network.
"""

from __future__ import annotations

from combo_discovery.corpus.importer import normalize_name
from combo_discovery.ontology.cycles import find_combos
from combo_discovery.ontology.extractor import CardContext
from combo_discovery.ontology.links import (
    LinkOptions,
    _closure_rank,
    build_links,
    host_context,
    link_enables,
    link_re_trigger,
    ports_enable,
)
from combo_discovery.ontology.ports import AbilitySig, Port
from combo_discovery.ontology.restrictions import parse_restriction


def _context(card_id: int, name: str, type_line: str) -> CardContext:
    return CardContext(card_id, name, normalize_name(name), "1", type_line, "", "", ())


def _sig(
    card_id: int,
    name: str,
    *,
    type_line: str = "Creature",
    kind: str = "ability",
    trigger: Port | None = None,
    consumes: tuple[Port, ...] = (),
    produces: tuple[Port, ...] = (),
) -> AbilitySig:
    return AbilitySig(
        card_id=card_id,
        card_name=name,
        ability_ref=f"line:0:{card_id}:A:X",
        ability_kind=kind,
        triggers_on=trigger,
        consumes=consumes,
        produces=produces,
        raw={"context": _context(card_id, name, type_line)},
    )


def _tap() -> Port:
    return Port("tap", {"self": True, "predicate": "TAPS_COST"})


def _untap(restriction: str = "Creature") -> Port:
    return Port("untap", {
        "restriction": parse_restriction(restriction).to_dict(),
        "predicate": "UNTAPS",
    })


def _copy(defined: str = "self") -> Port:
    return Port("copy_permanent", {
        "restriction": parse_restriction("").to_dict(),
        "defined": defined,
        "predicate": "COPIES_CREATURE",
    })


def _attacks() -> Port:
    return Port("attacks", {"self": True, "predicate": "ATTACKS"})


# ---------------------------------------------------------------------------
# E1: a fresh copy re-arms the copied card's activated ability
# ---------------------------------------------------------------------------


class TestCopyReArm:
    def _engine(self, *, triggered: bool = False) -> AbilitySig:
        return _sig(
            1, "Engine", type_line="Creature",
            kind="trigger" if triggered else "ability",
            trigger=_attacks() if triggered else None,
            consumes=(_tap(),), produces=(_copy(),),
        )

    def _untapper(self) -> AbilitySig:
        return _sig(2, "Untapper", consumes=(_tap(),), produces=(_untap(),))

    def test_copy_activation_link_is_emitted(self):
        link = link_re_trigger(self._engine(), self._untapper())
        assert link is not None
        assert link.kind == "re_trigger"
        assert link.subkind == "copy_activation"

    def test_triggered_engine_does_not_re_arm(self):
        assert link_re_trigger(self._engine(triggered=True), self._untapper()) is None

    def test_copy_permanent_covers_tap(self):
        engine = self._engine()
        assert ports_enable(engine.produces[0], _tap(), self._untapper()) is True

    def test_copy_engine_plus_untapper_is_a_cycle(self):
        combos = find_combos(
            [self._engine(), self._untapper()], max_len=2, options=LinkOptions()
        )
        assert combos, "copy engine + untapper did not close a cycle"
        assert any(
            link.subkind == "copy_activation"
            for link in combos[0].links
        )

    def test_copy_engine_cannot_copy_nonmatching_partner(self):
        # Engine copies only lands; the untapper is a creature.
        engine = _sig(
            1, "Engine", type_line="Creature", consumes=(_tap(),),
            produces=(Port("copy_permanent", {
                "restriction": parse_restriction("Land").to_dict(),
                "defined": "", "predicate": "COPIES_CREATURE",
            }),),
        )
        assert link_re_trigger(engine, self._untapper()) is None


# ---------------------------------------------------------------------------
# E2: an attachment's granted ability is matched against its host
# ---------------------------------------------------------------------------


class TestAttachmentHost:
    def _aura_engine(self) -> AbilitySig:
        return _sig(
            1, "Aura Engine", type_line="Enchantment Aura",
            consumes=(_tap(),), produces=(_copy(),),
        )

    def test_host_context_uses_creature_producer(self):
        aura = self._aura_engine()
        creature = _sig(2, "Host", type_line="Creature", produces=(_untap(),))
        host = host_context(creature, aura)
        assert host is not None and host.card_id == 2

    def test_host_context_falls_back_for_non_creature(self):
        aura = self._aura_engine()
        artifact = _sig(2, "Rock", type_line="Artifact", produces=(_untap(),))
        host = host_context(artifact, aura)
        assert host is not None and host.card_id == 1  # the Aura itself

    def test_creature_untapper_enables_aura_engine(self):
        link = link_enables(
            _sig(2, "Host", type_line="Creature", produces=(_untap(),)),
            self._aura_engine(),
        )
        assert link is not None and link.kind == "enables"

    def test_non_creature_untapper_does_not_enable_aura_engine(self):
        link = link_enables(
            _sig(2, "Rock", type_line="Artifact", produces=(_untap(),)),
            self._aura_engine(),
        )
        assert link is None

    def test_aura_engine_plus_host_is_a_cycle(self):
        host = _sig(
            2, "Host", type_line="Creature", consumes=(),
            trigger=Port("enters_battlefield", {"self": True, "predicate": "ETB_TRIGGER"}),
            produces=(_untap(),),
        )
        combos = find_combos([self._aura_engine(), host], max_len=2, options=LinkOptions())
        assert combos, "Aura engine + host did not close a cycle"


# ---------------------------------------------------------------------------
# E3: closure rank + listener indexing
# ---------------------------------------------------------------------------


class TestClosureRank:
    def test_zone_move_to_battlefield_ranks_zero(self):
        reanimate = _sig(1, "Reanimator", produces=(Port("zone_move", {
            "from": "Graveyard", "to": "Battlefield", "predicate": "MOVES_ZONE",
        }),))
        assert _closure_rank(reanimate) == 0

    def test_zone_move_to_exile_ranks_one(self):
        exile = _sig(1, "Exiler", produces=(Port("zone_move", {
            "from": "Battlefield", "to": "Exile", "predicate": "MOVES_ZONE",
        }),))
        assert _closure_rank(exile) == 1

    def test_reanimation_listener_is_indexed_for_sacrifice_engines(self):
        """A zone_move-to-battlefield listener closes a sacrifice engine."""
        engine = _sig(
            1, "Reanimator", type_line="Enchantment Aura",
            trigger=Port("enters_battlefield", {"self": True, "predicate": "ETB_TRIGGER"}),
            consumes=(Port("sacrifice", {"self": False, "predicate": "SACRIFICE_OUTLET"}),),
            produces=(Port("zone_move", {
                "from": "Graveyard", "to": "Battlefield", "predicate": "MOVES_ZONE",
            }),),
        )
        returner = _sig(
            2, "Returner",
            trigger=Port("enters_battlefield",
                         {"self": True, "predicate": "ETB_TRIGGER"}),
            produces=(Port("zone_move", {
                "from": "Exile", "to": "Battlefield", "predicate": "MOVES_ZONE",
            }),),
        )
        links = build_links([engine, returner], options=LinkOptions.safe())
        assert any(
            link.kind == "re_trigger" and link.dst.card_id == 2 for link in links
        ), "reanimation listener was pruned from the sacrifice engine"
