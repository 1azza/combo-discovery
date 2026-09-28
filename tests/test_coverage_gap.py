"""Unit tests for the coverage-gap classification logic (no database).

The module's pure functions are exercised with synthetic card metadata and
combo lists: mapping priority, bucket assignment, capability tagging, pair-set
construction, and the precision view.
"""

from __future__ import annotations

from combo_discovery.ontology.coverage import (
    EDGE_EXISTS_FILTERED,
    NET_PRESENT,
    NO_GRAPH_EDGE,
    CardMeta,
    KnownCombo,
    analyze_denominator,
    capability_tags,
    classify_pair,
    combo_pairs,
    map_card,
    ordered_combo_pairs,
    pool_gap_view,
    precision_view,
    primary_capability,
)


def _meta(
    card_id: int,
    name: str,
    *,
    type_line: str = "Creature",
    oracle_text: str = "",
) -> CardMeta:
    return CardMeta(
        card_id=card_id,
        name=name,
        normalized_name=name.lower().replace(",", ""),
        type_line=type_line,
        oracle_text=oracle_text,
    )


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


class TestMapCard:
    def test_oracle_wins_over_name(self):
        cid, via = map_card(
            raw_name="Whatever",
            normalized_name="whatever",
            oracle_id="o-1",
            by_oracle={"o-1": 7},
            by_norm={"whatever": 9},
        )
        assert (cid, via) == (7, "oracle")

    def test_name_used_when_no_oracle(self):
        cid, via = map_card(
            raw_name="Kiki-Jiki, Mirror Breaker",
            normalized_name="kiki jiki mirror breaker",
            oracle_id=None,
            by_oracle={},
            by_norm={"kiki jiki mirror breaker": 5},
        )
        assert (cid, via) == (5, "name")

    def test_raw_front_face_fallback(self):
        cid, via = map_card(
            raw_name="Lunarch Veteran // Luminous Phantom",
            normalized_name="not in corpus",
            oracle_id=None,
            by_oracle={},
            by_norm={"lunarch veteran": 3},
        )
        assert (cid, via) == (3, "raw_front")

    def test_unmatched(self):
        cid, via = map_card(
            raw_name="Nope",
            normalized_name="nope",
            oracle_id=None,
            by_oracle={},
            by_norm={},
        )
        assert cid is None and via == "unmatched"


# ---------------------------------------------------------------------------
# Buckets
# ---------------------------------------------------------------------------


class TestClassifyPair:
    def test_present(self):
        pair = frozenset((1, 2))
        assert classify_pair(
            pair, present_pairs={pair}, edge_pairs={pair}
        ) == NET_PRESENT

    def test_edge_but_not_a_pair_proposal(self):
        pair = frozenset((1, 2))
        assert classify_pair(
            pair, present_pairs=set(), edge_pairs={pair}
        ) == EDGE_EXISTS_FILTERED

    def test_no_edge(self):
        assert classify_pair(
            frozenset((1, 2)), present_pairs=set(), edge_pairs=set()
        ) == NO_GRAPH_EDGE


# ---------------------------------------------------------------------------
# Capability tags
# ---------------------------------------------------------------------------


class TestCapabilityTags:
    def test_aura_equipment_beats_others(self):
        aura = _meta(1, "Curiosity", type_line="Enchantment Aura",
                     oracle_text="Enchant creature")
        torch = _meta(2, "Human Torch",
                      oracle_text="Whenever you draw a card, deal 1 damage.")
        tags = capability_tags(aura, torch)
        assert primary_capability(tags) == "aura_equipment"

    def test_draw_damage_loop(self):
        a = _meta(1, "Niv-Mizzet", oracle_text="Whenever you draw a card, deal damage.")
        b = _meta(2, "Curiosity", type_line="Enchantment Aura",
                  oracle_text="Whenever enchanted creature deals damage, draw a card.")
        # The Aura still takes priority, but the loop tag is present.
        assert "draw_damage_loop" in capability_tags(a, b)

    def test_triggered_vs_activated_copy(self):
        activated = _meta(1, "Kiki", oracle_text="{T}: Create a token that's a copy.")
        triggered = _meta(
            2, "Splinter",
            oracle_text="Whenever this attacks, create a token that's a copy.",
        )
        assert "activated_copy_engine" in capability_tags(activated, activated)
        assert primary_capability(capability_tags(triggered, triggered)) == "triggered_engine"

    def test_one_shot(self):
        a = _meta(1, "Tainted Strike", type_line="Instant",
                  oracle_text="Target creature gets +1/+0.")
        b = _meta(2, "Kediss")
        assert primary_capability(capability_tags(a, b)) == "one_shot"

    def test_counters_synergy(self):
        a = _meta(1, "Cathars' Crusade",
                  oracle_text="Whenever a creature enters, put a +1/+1 counter on each creature.")
        b = _meta(2, "Aerie Ouphes", oracle_text="Persist.")
        assert primary_capability(capability_tags(a, b)) == "counters_synergy"

    def test_fallback_is_other_synergy(self):
        a = _meta(1, "A", oracle_text="Nothing special.")
        b = _meta(2, "B", oracle_text="Also nothing.")
        assert capability_tags(a, b) == ("other_synergy",)


# ---------------------------------------------------------------------------
# Pair sets
# ---------------------------------------------------------------------------


class TestPairSets:
    def test_combo_pairs_dedup_and_size_filter(self):
        combos = [
            KnownCombo(1, (1, 2), ("A", "B")),
            KnownCombo(2, (1, 2, 3), ("A", "B", "C")),
        ]
        assert combo_pairs(combos, sizes=(2,)) == {frozenset((1, 2))}
        assert combo_pairs(combos, sizes=(2, 3)) == {
            frozenset((1, 2)), frozenset((1, 3)), frozenset((2, 3))
        }

    def test_unmapped_combo_excluded(self):
        combos = [KnownCombo(1, (1, None), ("A", "?"))]
        assert combo_pairs(combos) == set()

    def test_same_card_pair_skipped(self):
        combos = [KnownCombo(1, (1, 1), ("A", "A"))]
        assert combo_pairs(combos) == set()

    def test_ordered_variant(self):
        combos = [KnownCombo(1, (1, 2), ("A", "B"))]
        assert ordered_combo_pairs(combos) == {(1, 2), (2, 1)}


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


class TestReports:
    def _metas(self):
        return {
            1: _meta(1, "Engine", oracle_text="{T}: Create a token that's a copy."),
            2: _meta(2, "Untapper", oracle_text="Untap target permanent."),
            3: _meta(3, "Aura", type_line="Enchantment Aura", oracle_text="Enchant creature"),
        }

    def test_analyze_buckets_and_examples(self):
        metas = self._metas()
        pairs = [frozenset((1, 2)), frozenset((1, 3))]
        report = analyze_denominator(
            "test", pairs,
            present_pairs={frozenset((1, 2))},
            edge_pairs=set(),
            metas=metas,
        )
        assert report.buckets[NET_PRESENT] == 1
        assert report.buckets[NO_GRAPH_EDGE] == 1
        assert report.capabilities == {"aura_equipment": 1}
        assert report.capability_examples["aura_equipment"] == [("Engine", "Aura")]
        assert report.present_examples == [("Engine", "Untapper")]

    def test_precision_view(self):
        view = precision_view({frozenset((1, 2)), frozenset((1, 3))},
                              {frozenset((1, 2))})
        assert view["candidate_pairs"] == 2
        assert view["known_pairs"] == 1
        assert view["fraction"] == 0.5

    def test_pool_gap_view(self):
        pairs = [frozenset((1, 2)), frozenset((3, 4)), frozenset((1, 3))]
        view = pool_gap_view(pairs, pool={1, 2})
        assert view == {"neither_in_pool": 1, "one_in_pool": 1, "both_in_pool": 1}
