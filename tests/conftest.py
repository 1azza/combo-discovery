"""Shared fixtures for the TUI card-loop tests."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

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
