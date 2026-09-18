"""Shared fixtures for the TUI card-loop tests."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from combo_discovery.corpus.names import normalize_card_name, pair_hash
from combo_discovery.store import ExperimentStore

# Deterministic ids and shapes used by the assertions in the TUI tests.
KIKI_ID = 1
PESTERMITE_ID = 2
SPLINTER_TWIN_ID = 3
BRAINSTORM_ID = 4
DECEIVER_ID = 5

# (id, pattern, card_ids, score, status, mechanism)
HYPOTHESES = [
    (
        1, 1, [KIKI_ID, PESTERMITE_ID], 0.97, "proposed",
        "Kiki-Jiki copies Pestermite; the token's ETB untaps Kiki-Jiki and "
        "creates an arbitrarily large number of hasty fliers.",
    ),
    (
        2, 1, [SPLINTER_TWIN_ID, PESTERMITE_ID], 0.90, "proposed",
        "Splinter Twin enchants Pestermite; each copy untaps the enchanted "
        "creature for an unbounded number of tokens.",
    ),
    (
        3, 2, [BRAINSTORM_ID, DECEIVER_ID], 0.50, "proposed",
        "Cheap spells plus a cost reducer feed the storm count.",
    ),
    (
        4, 1, [KIKI_ID, BRAINSTORM_ID], 0.80, "refuted",
        "Refuted pairing: no untap, so the loop does not close.",
    ),
]


def seed_ontology(db_path: Path) -> Path:
    """Create the store schema and fill a small, deterministic ontology."""
    ExperimentStore(db_path).close()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO import_runs (import_id, started_at, scryfall_source)"
            " VALUES ('imp1', '2026-01-01T00:00:00+00:00', 'forge_script')"
        )
        cards = [
            (KIKI_ID, "Kiki-Jiki, Mirror Breaker", "Legendary Creature — Goblin",
             "{2}{R}{R}{R}", "Haste. {T}: Create a token that's a copy of target "
             "nonlegendary creature you control."),
            (PESTERMITE_ID, "Pestermite", "Creature — Faerie Wizard",
             "{2}{U}", "Flash. Flying. When Pestermite enters, tap or untap "
             "target permanent."),
            (SPLINTER_TWIN_ID, "Splinter Twin", "Enchantment — Aura",
             "{2}{R}{R}", "Enchanted creature has '{T}: Create a token that's a "
             "copy of this creature.'"),
            (BRAINSTORM_ID, "Brainstorm", "Instant", "{U}",
             "Draw three cards, then put two cards from your hand on top."),
            (DECEIVER_ID, "Deceiver Exarch", "Creature — Cleric", "{2}{U}",
             "Flash. When Deceiver Exarch enters, untap target permanent."),
        ]
        conn.executemany(
            "INSERT INTO cards (id, import_id, file_sha256, name, normalized_name,"
            " mana_cost, type_line, oracle_text, effect_count, set_code, rarity)"
            " VALUES (?, 'imp1', 'sha', ?, ?, ?, ?, ?, 2, 'TST', 'rare')",
            [(cid, name, name.lower(), mana, type_line, oracle)
             for cid, name, type_line, mana, oracle in cards],
        )
        conn.executemany(
            "INSERT INTO patterns (id, name, description, pattern_json, version)"
            " VALUES (?, ?, ?, '{}', 1)",
            [
                (1, "infinite_etb_loop", "Tap-cost engine plus an ETB untapper."),
                (2, "storm_engine", "Storm payoff plus a cheap-spell enabler."),
            ],
        )
        conn.executemany(
            "INSERT INTO combo_hypotheses (id, import_id, pattern_id, card_ids_json,"
            " mechanism, score, status, created_at)"
            " VALUES (?, 'imp1', ?, ?, ?, ?, ?, '2026-01-02T00:00:00+00:00')",
            [(hid, pattern, json.dumps(cards), mechanism, score, status)
             for hid, pattern, cards, score, status, mechanism in HYPOTHESES],
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


@pytest.fixture
def ontology_db(tmp_path: Path) -> Path:
    """A temp ``research.db`` with two patterns and four hypotheses."""
    return seed_ontology(tmp_path / "research.db")


# ---------------------------------------------------------------------------
# Card Lab fixture: a tiny ground truth + proposals + missing combo
# ---------------------------------------------------------------------------

LAB_KIKI, LAB_PESTERMITE, LAB_SPLINTER, LAB_DECEIVER, LAB_FOMO = 1, 2, 3, 4, 5


def _evidence(predicates: list[str]) -> str:
    items = [
        {"card_id": LAB_KIKI, "lines": [], "name": "Kiki-Jiki, Mirror Breaker",
         "params": {}, "predicate": predicate}
        for predicate in predicates
    ]
    items.append(
        {
            "type_verified": True, "copy_verified": True, "ability_linked": True,
            "kind": "compatibility", "link_kind": "etb_chain", "detail": "ok", "via": [],
        }
    )
    return json.dumps(items)


def seed_lab(path: Path) -> Path:
    """Cards, one pattern, three proposals and three known combos.

    Verdicts: proposal #1 ``known_pair``, #2 ``unmatched``, #3
    ``contained_in_known``; known combo Kiki + Fear of Missing Out is missed.
    """
    ExperimentStore(path).close()
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO import_runs (import_id, started_at, scryfall_source)"
            " VALUES ('imp1', '2026-01-01T00:00:00+00:00', 'forge_script')"
        )
        conn.execute(
            "INSERT INTO import_runs (import_id, started_at, scryfall_source)"
            " VALUES ('sb1', '2026-01-01T00:00:00+00:00', 'spellbook')"
        )
        cards = [
            (LAB_KIKI, "Kiki-Jiki, Mirror Breaker", "Legendary Creature — Goblin",
             "{2}{R}{R}{R}", "Haste. {T}: Create a token that's a copy of target creature."),
            (LAB_PESTERMITE, "Pestermite", "Creature — Faerie", "{2}{U}",
             "Flash. When Pestermite enters, tap or untap target permanent."),
            (LAB_SPLINTER, "Splinter Twin", "Enchantment — Aura", "{2}{R}{R}",
             "Enchanted creature has '{T}: Create a token copy'."),
            (LAB_DECEIVER, "Deceiver Exarch", "Creature — Cleric", "{2}{U}",
             "Flash. When Deceiver Exarch enters, untap target permanent."),
            (LAB_FOMO, "Fear of Missing Out", "Creature — Horror", "{1}{R}",
             "When this enters, untap target creature you control."),
        ]
        conn.executemany(
            "INSERT INTO cards (id, import_id, file_sha256, name, normalized_name,"
            " mana_cost, type_line, oracle_text, effect_count, set_code, rarity)"
            " VALUES (?, 'imp1', 'sha', ?, ?, ?, ?, ?, 2, 'TST', 'rare')",
            [(cid, name, normalize_card_name(name), mana, type_line, oracle)
             for cid, name, type_line, mana, oracle in cards],
        )
        conn.execute(
            "INSERT INTO patterns (id, name, description, pattern_json, version)"
            " VALUES (1, 'infinite_etb_loop', 'Tap-cost engine plus ETB untapper.',"
            " '{}', 1)"
        )
        interactions = [
            (
                1, 1, LAB_KIKI, LAB_PESTERMITE, 0.97,
                ["TAPS_COST", "COPIES_CREATURE", "ETB_TRIGGER", "UNTAPS"],
            ),
            (2, 1, LAB_KIKI, LAB_SPLINTER, 0.90, ["TAPS_COST", "COPIES_CREATURE"]),
            (3, 1, LAB_KIKI, LAB_DECEIVER, 0.50, ["TAPS_COST", "PRODUCES_MANA"]),
        ]
        conn.executemany(
            "INSERT INTO interactions (id, import_id, source_card_id, target_card_id,"
            " pattern_id, direction, mechanism, score, evidence_json, created_at)"
            " VALUES (?, 'imp1', ?, ?, ?, 'mutual', 'loop', ?, ?, '2026-01-02T00:00:00+00:00')",
            [(iid, src, tgt, pat, score, _evidence(preds))
             for iid, pat, src, tgt, score, preds in interactions],
        )
        conn.executemany(
            "INSERT INTO combo_hypotheses (id, import_id, pattern_id, card_ids_json,"
            " mechanism, score, status, created_at)"
            " VALUES (?, 'imp1', 1, ?, 'loop', ?, 'proposed', '2026-01-02T00:00:00+00:00')",
            [
                (1, json.dumps([LAB_KIKI, LAB_PESTERMITE]), 0.97),
                (2, json.dumps([LAB_KIKI, LAB_SPLINTER]), 0.90),
                (3, json.dumps([LAB_KIKI, LAB_DECEIVER]), 0.50),
            ],
        )
        known = [
            # (id, source_id, n_uses, n_requires, description, produces_feature, popularity)
            (101, "1-1", 2, 0, "Tap Kiki to copy Pestermite; untap; repeat.",
             "Infinite creature tokens with haste", 500),
            (102, "1-2", 2, 0, "Kiki copies Fear of Missing Out for an extra combat.",
             "Infinite combat phases", 42),
            (103, "1-3", 3, 0, "Kiki + Deceiver + a third piece loops.",
             "Infinite creature ETB", 7),
        ]
        conn.executemany(
            "INSERT INTO known_combos (id, source, source_id, source_version,"
            " fetched_at, bracket_tag, popularity, easy_prereqs, notable_prereqs,"
            " description, legalities_json, produces_json, requires_json,"
            " n_uses, n_requires, n_produces, import_id)"
            " VALUES (?, 'commander_spellbook', ?, '6.4.0', '2026-01-01T00:00:00+00:00',"
            " 'R', ?, '', '', ?, ?, ?, '[]', ?, ?, 1, 'sb1')",
            [
                (
                    kid, source_id, popularity, description,
                    json.dumps({"vintage": True, "commander": True, "modern": False}),
                    json.dumps([{"feature": {"name": feature}, "quantity": 1}]),
                    n_uses, n_requires,
                )
                for kid, source_id, n_uses, n_requires, description, feature, popularity in known
            ],
        )
        pieces = [
            (101, "Kiki-Jiki, Mirror Breaker"), (101, "Pestermite"),
            (102, "Kiki-Jiki, Mirror Breaker"), (102, "Fear of Missing Out"),
            (103, "Kiki-Jiki, Mirror Breaker"), (103, "Deceiver Exarch"),
            (103, "Third Piece"),
        ]
        conn.executemany(
            "INSERT INTO known_combo_cards (combo_id, role, raw_name, normalized_name,"
            " quantity, zone_locations, must_be_commander, import_id)"
            " VALUES (?, 'use', ?, ?, 1, '[\"B\"]', 0, 'sb1')",
            [(cid, name, normalize_card_name(name)) for cid, name in pieces],
        )
        pair_rows: list[tuple[int, str, int]] = []
        for combo_id, names in (
            (101, ["Kiki-Jiki, Mirror Breaker", "Pestermite"]),
            (102, ["Kiki-Jiki, Mirror Breaker", "Fear of Missing Out"]),
            (103, ["Kiki-Jiki, Mirror Breaker", "Deceiver Exarch", "Third Piece"]),
        ):
            norm = [normalize_card_name(n) for n in names]
            for i in range(len(norm)):
                for j in range(i + 1, len(norm)):
                    full = 1 if len(norm) == 2 else 0
                    pair_rows.append((combo_id, pair_hash(norm[i], norm[j]), full))
        conn.executemany(
            "INSERT INTO known_combo_pairs (combo_id, pair_hash, is_full_variant,"
            " source, import_id) VALUES (?, ?, ?, 'commander_spellbook', 'sb1')",
            pair_rows,
        )
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture
def lab_db(tmp_path: Path) -> Path:
    """A temp DB with proposals + a tiny Commander Spellbook ground truth."""
    return seed_lab(tmp_path / "lab.db")
