"""Unit tests for the shared card heuristics in ``combo_discovery.cards``.

These exercise the predicates directly (not through a script), so the single
source of truth is covered independently of whichever instrument imports it.
"""

from __future__ import annotations

import gzip
import json
import sqlite3
from datetime import datetime

from combo_discovery import cards

# -- fixtures ---------------------------------------------------------------

#: The Kiki-Jiki / Splinter Twin activated copy shape.
KIKI_OT = "{t}: create a token that's a copy of target creature you control."
#: A triggered (attack) copy engine the policy cannot drive.
ATTACK_OT = (
    "whenever attack copier attacks, create a token that's a copy of "
    "target creature you control."
)
#: A triggered (ETB) copy engine the policy cannot drive.
ETB_OT = (
    "when etb copier enters, create a token that's a copy of target creature "
    "you control."
)

#: A free {T}: engine with a real, non-copy effect.
TAP_ENGINE_OT = "{t}: create a 1/1 green elf creature token."
#: A pure mana dork: ramp, not a loop engine.
MANA_DORK_OT = "{t}: add {g}."
#: A {T} engine with a per-turn limit: cannot loop.
LIMITED_OT = "{t}: draw a card. activate only once each turn."
#: A multi-ability mana land: every {T} ability is mana.
MANA_LAND_OT = "{t}: add {c}. {t}: add {r} or {w}."
#: A pure-mana artifact whose mana clause carries a damage rider (the Talisman
#: shape): the rider must not make it look like an engine.
MANA_ARTIFACT_OT = (
    "{t}: add {c}. {t}: add {w} or {u}. test mana artifact deals 1 damage to you."
)

#: The functional ETB-untapper shape.
ETB_UNTAPPER_OT = "when test untapper enters, untap target creature you control."
#: An activated (non-ETB) untapper.
ACTIVATED_UNTAPPER_OT = "{1}: untap target creature."
#: An attack-triggered untapper.
ATTACK_UNTAPPER_OT = "flying\nwhenever test attack untapper attacks, untap target creature."


# -- copy-engine predicate --------------------------------------------------


def test_activated_copy_engine_matches_only_activated_shapes():
    assert cards.is_activated_copy_engine(KIKI_OT)
    assert not cards.is_activated_copy_engine(ATTACK_OT)
    assert not cards.is_activated_copy_engine(ETB_OT)
    assert not cards.is_activated_copy_engine("")


# -- untap predicates -------------------------------------------------------


def test_is_etb_untapper_requires_creature_and_etb_event():
    assert cards.is_etb_untapper("Creature Wizard", ETB_UNTAPPER_OT)
    # Not a creature.
    assert not cards.is_etb_untapper("Artifact", ETB_UNTAPPER_OT)
    # No enters event.
    assert not cards.is_etb_untapper("Creature Wizard", "untap target creature.")


def test_is_untapper_covers_etb_and_any_shape():
    assert cards.is_untapper("Creature Wizard", ETB_UNTAPPER_OT)
    assert cards.is_untapper("Artifact", ACTIVATED_UNTAPPER_OT)
    assert cards.is_untapper("Creature Bird", ATTACK_UNTAPPER_OT)
    assert not cards.is_untapper("Instant", "draw two cards.")


# -- tap-engine predicate ---------------------------------------------------


def test_tap_engine_accepts_real_effect():
    assert cards.is_tap_engine(TAP_ENGINE_OT, "Artifact")


def test_tap_engine_excludes_pure_mana():
    assert not cards.is_tap_engine(MANA_DORK_OT, "Creature Elf Druid")
    # A multi-ability mana land is ramp.
    assert not cards.is_tap_engine(MANA_LAND_OT, "Land")
    # A mana artifact with a damage rider is still pure mana.
    assert not cards.is_tap_engine(MANA_ARTIFACT_OT, "Artifact")


def test_tap_engine_excludes_limited_ability():
    assert not cards.is_tap_engine(LIMITED_OT, "Artifact")


def test_tap_engine_excludes_activated_copy():
    # Token-copy abilities belong to the copy class.
    assert not cards.is_tap_engine(KIKI_OT, "Legendary Creature Goblin")


# -- corpus helpers ---------------------------------------------------------


def test_load_release_map_filters_and_keeps_newest(tmp_path):
    from combo_discovery.corpus.names import normalize_card_name

    path = tmp_path / "oracle.jsonl.gz"
    rows = [
        {"name": "Test Kiki", "released_at": "2025-02-01"},
        {"name": "Test Kiki", "released_at": "2024-01-01"},
        {"name": "Ignored Card", "released_at": "2020-01-01"},
        {"name": "No Date"},
    ]
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
        handle.write("not json\n")

    corpus = {normalize_card_name("Test Kiki")}
    releases = cards.load_release_map(path, corpus)
    assert releases == {normalize_card_name("Test Kiki"): "2025-02-01"}


def test_known_pair_hashes_reads_spellbook_table(tmp_path):
    db = tmp_path / "known.db"
    conn = sqlite3.connect(db)
    conn.execute("create table known_combo_pairs (pair_hash text)")
    conn.executemany(
        "insert into known_combo_pairs (pair_hash) values (?)",
        [("hash-a",), ("hash-b",)],
    )
    conn.commit()
    assert cards.known_pair_hashes(conn) == {"hash-a", "hash-b"}
    conn.close()


# -- small helpers ----------------------------------------------------------


def test_verdict_rank_orders_strongest_first():
    assert cards.verdict_rank("loops") < cards.verdict_rank("no_loop")
    # Unknown verdicts rank weakest.
    assert cards.verdict_rank("bogus") > cards.verdict_rank("error")


def test_utc_now_is_timezone_aware_isoformat():
    parsed = datetime.fromisoformat(cards.utc_now())
    assert parsed.tzinfo is not None


def test_is_legal_uses_the_cached_checker(monkeypatch):
    class _Permissive:
        def is_legal(self, name: str) -> bool:  # noqa: ARG002 - stub
            return True

    monkeypatch.setattr(cards, "_LEGALITY", _Permissive())
    assert cards.is_legal("Anything At All") is True
