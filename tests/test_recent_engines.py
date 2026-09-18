"""Pure selection logic for ``scripts/recent_engines.py``.

The DB/harness wiring is exercised through the real ``main`` by
``test_instrument_persist``; here the engine/partner filters and the pair
cross-product are unit-tested on a tiny synthetic corpus.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from combo_discovery.corpus.names import normalize_card_name, pair_hash

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str):
    """Import a ``scripts/<name>.py`` module by path (not an installed package)."""
    path = _REPO_ROOT / "scripts" / f"{name}.py"
    synth = f"combo_script_{name}"
    spec = importlib.util.spec_from_file_location(synth, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # ``@dataclass`` resolves string annotations through ``sys.modules`` during
    # class creation, so the module must be registered before ``exec_module``.
    sys.modules[synth] = module
    spec.loader.exec_module(module)
    return module


re_mod = _load_script("recent_engines")

SINCE = 2024


def _card(name: str, type_line: str, oracle: str):
    return re_mod.Card(
        normalized_name=normalize_card_name(name),
        name=name,
        type_line=type_line,
        oracle_text=oracle.lower(),
    )


#: A recent activated copy engine (the Kiki/Twin shape).
KIKI = _card(
    "Test Kiki", "Legendary Creature Goblin",
    "{T}: Create a token that's a copy of target creature you control.",
)
#: The functional ETB-untapper partner shape.
UNTAPPER = _card(
    "Test Untapper", "Creature Wizard",
    "When Test Untapper enters, untap target creature you control.",
)
#: A triggered (attack) copy engine: the policy cannot drive it.
ATTACK_ENGINE = _card(
    "Attack Copier", "Creature Dragon",
    "Whenever Attack Copier attacks, create a token that's a copy of "
    "target creature you control.",
)
#: A triggered (ETB) copy engine: also undriveable.
ETB_ENGINE = _card(
    "Etb Copier", "Creature Angel",
    "When Etb Copier enters, create a token that's a copy of target creature "
    "you control.",
)
#: An activated copy engine from an old set.
OLD_ENGINE = _card(
    "Old Kiki", "Legendary Creature Goblin",
    "{T}: Create a token that's a copy of target creature you control.",
)

# -- tap-class fixtures -----------------------------------------------------

#: A recent free {T}: engine (non-copy, non-mana) — the Kiki/Pestermite shape.
TAP_ENGINE = _card(
    "Test Tap Engine", "Artifact",
    "{T}: Create a 1/1 green Elf creature token.",
)
#: A pure mana ability: ramp, not a loop engine.
MANA_DORK = _card("Mana Dork", "Creature Elf Druid", "{T}: Add {G}.")
#: A {T} engine with a per-turn activation limit: cannot loop.
LIMITED_ENGINE = _card(
    "Limited Engine", "Artifact",
    "{T}: Draw a card. Activate only once each turn.",
)
#: A multi-ability mana land: every {T} ability is mana, so it is ramp, not an
#: engine, even though it is a ``Land`` with two ``{T}:`` abilities.
MANA_LAND = _card(
    "Test Mana Land", "Land",
    "{T}: Add {C}. {T}: Add {R} or {W}.",
)
#: A pure-mana artifact whose mana clause carries a rider (the Talisman shape);
#: it must not count as an engine just because "deals 1 damage" looks like a verb.
MANA_ARTIFACT = _card(
    "Test Mana Artifact", "Artifact",
    "{T}: Add {C}. {T}: Add {W} or {U}. Test Mana Artifact deals 1 damage to you.",
)
#: An activated (non-ETB) untapper: any-shape partner for the tap class.
ACTIVATED_UNTAPPER = _card(
    "Test Activated Untapper", "Artifact", "{1}: Untap target creature."
)
#: An attack-triggered untapper: also an any-shape partner.
ATTACK_UNTAPPER = _card(
    "Test Attack Untapper", "Creature Bird",
    "Flying\nWhenever Test Attack Untapper attacks, untap target creature.",
)

ALL = [KIKI, UNTAPPER, ATTACK_ENGINE, ETB_ENGINE, OLD_ENGINE]
RELEASES = {
    KIKI.normalized_name: "2025-02-01",
    UNTAPPER.normalized_name: "2012-05-01",
    ATTACK_ENGINE.normalized_name: "2025-03-01",
    ETB_ENGINE.normalized_name: "2025-03-01",
    OLD_ENGINE.normalized_name: "2011-01-01",
}

TAP_ALL = [
    TAP_ENGINE, MANA_DORK, LIMITED_ENGINE, MANA_LAND, MANA_ARTIFACT,
    ACTIVATED_UNTAPPER,
    ATTACK_UNTAPPER, UNTAPPER,
]
TAP_RELEASES = {card.normalized_name: "2025-01-01" for card in TAP_ALL}


def _legal(name: str) -> bool:
    return True


def test_recent_engine_pairs_with_untapper():
    engines = re_mod.select_recent_engines(ALL, RELEASES, since=SINCE, is_legal=_legal)
    assert [c.name for c in engines] == ["Test Kiki"]

    partners = re_mod.select_partners(ALL, is_legal=_legal, known_engines=())
    assert partners == ["Test Untapper"]

    pairs = re_mod.build_pairs(
        [c.name for c in engines], partners, set(), max_pairs=60
    )
    assert pairs == [("Test Kiki", "Test Untapper")]


def test_triggered_engines_are_excluded():
    assert not re_mod.is_activated_copy_engine(ATTACK_ENGINE.oracle_text)
    assert not re_mod.is_activated_copy_engine(ETB_ENGINE.oracle_text)
    recent = {
        c.name
        for c in re_mod.select_recent_engines(ALL, RELEASES, since=SINCE, is_legal=_legal)
    }
    assert recent == {"Test Kiki"}


def test_catalogued_pair_is_excluded():
    known = {pair_hash(KIKI.normalized_name, UNTAPPER.normalized_name)}
    pairs = re_mod.build_pairs([KIKI.name], [UNTAPPER.name], known, max_pairs=60)
    assert pairs == []


def test_non_recent_engine_excluded_when_since_is_late():
    recent = {
        c.name
        for c in re_mod.select_recent_engines(ALL, RELEASES, since=SINCE, is_legal=_legal)
    }
    assert "Old Kiki" not in recent
    # Lowering ``since`` below its release date lets the old engine in.
    older = {
        c.name
        for c in re_mod.select_recent_engines(ALL, RELEASES, since=2010, is_legal=_legal)
    }
    assert "Old Kiki" in older


def test_known_engines_are_appended_to_partners():
    partners = re_mod.select_partners(
        ALL, is_legal=_legal, known_engines=["Kiki-Jiki, Mirror Breaker"]
    )
    assert partners == ["Test Untapper", "Kiki-Jiki, Mirror Breaker"]


# -- tap engine class -------------------------------------------------------


def test_tap_engine_pairs_with_untapper_in_tap_mode():
    engines = re_mod.select_recent_engines(
        TAP_ALL, TAP_RELEASES, since=SINCE, is_legal=_legal, engine_class="tap"
    )
    assert [c.name for c in engines] == ["Test Tap Engine"]

    partners = re_mod.select_partners(TAP_ALL, is_legal=_legal, engine_class="tap")
    # Untappers of any shape: activated and attack-triggered included.
    assert "Test Untapper" in partners
    assert "Test Activated Untapper" in partners
    assert "Test Attack Untapper" in partners

    pairs = re_mod.build_pairs(
        [c.name for c in engines], partners, set(), max_pairs=60
    )
    assert ("Test Tap Engine", "Test Untapper") in pairs
    assert ("Test Tap Engine", "Test Activated Untapper") in pairs


def test_tap_class_excludes_pure_mana_ability():
    assert not re_mod.is_tap_engine(MANA_DORK.oracle_text)
    # A pure-mana artifact with a rider ("deals 1 damage to you") is not an engine.
    assert not re_mod.is_tap_engine(MANA_ARTIFACT.oracle_text, MANA_ARTIFACT.type_line)
    # A mana land with several mana abilities is not an engine either.
    assert not re_mod.is_tap_engine(MANA_LAND.oracle_text, MANA_LAND.type_line)
    names = {
        c.name
        for c in re_mod.select_recent_engines(
            TAP_ALL, TAP_RELEASES, since=SINCE, is_legal=_legal, engine_class="tap"
        )
    }
    assert "Mana Dork" not in names


def test_tap_class_excludes_multi_ability_mana_land():
    # Both {T} abilities add mana, so the land is ramp — not a tap engine.
    assert not re_mod.is_tap_engine(MANA_LAND.oracle_text, MANA_LAND.type_line)
    names = {
        c.name
        for c in re_mod.select_recent_engines(
            TAP_ALL, TAP_RELEASES, since=SINCE, is_legal=_legal, engine_class="tap"
        )
    }
    assert "Test Mana Land" not in names

    # A {T}: ... engine with a real effect still qualifies (the Land guard only
    # fires when every {T} ability is a mana ability).
    assert re_mod.is_tap_engine(TAP_ENGINE.oracle_text, TAP_ENGINE.type_line)
    real_land = _card("Test Utility Land", "Land", "{T}: Draw a card.")
    assert re_mod.is_tap_engine(real_land.oracle_text, real_land.type_line)


def test_pair_order_interleaves_engines_and_partners():
    engines = ["E0", "E1", "E2"]
    partners = ["P0", "P1", "P2", "P3"]

    capped = re_mod.build_pairs(engines, partners, set(), max_pairs=3)
    assert [engine for engine, _ in capped] == ["E0", "E1", "E2"]
    assert len({partner for _, partner in capped}) == 3

    # The uncapped product still covers every engine x partner combination.
    full = re_mod.build_pairs(engines, partners, set(), max_pairs=None)
    assert len(full) == len(engines) * len(partners)
    assert {(e, p) for e, p in full} == {
        (e, p) for e in engines for p in partners
    }


def test_tap_class_excludes_limited_ability():
    assert not re_mod.is_tap_engine(LIMITED_ENGINE.oracle_text)
    names = {
        c.name
        for c in re_mod.select_recent_engines(
            TAP_ALL, TAP_RELEASES, since=SINCE, is_legal=_legal, engine_class="tap"
        )
    }
    assert "Limited Engine" not in names


def test_copy_mode_is_still_the_default():
    # The default class is unchanged: only the copy engine is selected, and the
    # copy engine is not re-selected by the tap class (it is a copy ability).
    engines_copy = re_mod.select_recent_engines(ALL, RELEASES, since=SINCE, is_legal=_legal)
    assert [c.name for c in engines_copy] == ["Test Kiki"]

    engines_tap = re_mod.select_recent_engines(
        ALL, RELEASES, since=SINCE, is_legal=_legal, engine_class="tap"
    )
    assert "Test Kiki" not in {c.name for c in engines_tap}
