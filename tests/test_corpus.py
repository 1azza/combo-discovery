"""Card-corpus parser + importer tests (no network, no Forge checkout needed).

The three canonical parser samples are embedded verbatim; the importer runs on a
synthetic mini-cardsfolder in ``tmp_path`` so the suite is hermetic.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from combo_discovery.corpus import parser as corpus_parser
from combo_discovery.corpus.importer import (
    ImportReport,
    import_corpus,
    normalize_name,
    resolve_cardsfolder,
)
from combo_discovery.store import ExperimentStore, _migration_2
from combo_discovery.tui.data import StoreBinding

# ---------------------------------------------------------------------------
# Canonical samples (exact Forge script text)
# ---------------------------------------------------------------------------

GRIZZLY_BEARS = (
    "Name:Grizzly Bears\n"
    "ManaCost:1 G\n"
    "Types:Creature Bear\n"
    "PT:2/2\n"
    "Oracle:\n"
)

KIKI_JIKI = (
    "Name:Kiki-Jiki, Mirror Breaker\n"
    "ManaCost:2 R R R\n"
    "Types:Legendary Creature Goblin Shaman\n"
    "PT:2/2\n"
    "K:Haste\n"
    "A:AB$ CopyPermanent | Cost$ T | ValidTgts$ Creature.nonLegendary+YouCtrl | "
    "TgtPrompt$ Select target nonlegendary creature you control | "
    "AddKeywords$ Haste | AtEOT$ Sacrifice | "
    "SpellDescription$ Create a token that's a copy of target nonlegendary "
    "creature you control, except it has haste. Sacrifice it at the beginning "
    "of the next end step.\n"
    "SVar:UntapMe:True\n"
    "Oracle:Haste\\n{T}: Create a token that's a copy of target nonlegendary "
    "creature you control, except it has haste. Sacrifice it at the beginning "
    "of the next end step.\n"
)

FIERY_CONFLUENCE = (
    "Name:Fiery Confluence\n"
    "ManaCost:2 R R\n"
    "Types:Sorcery\n"
    "A:SP$ Charm | Choices$ DBDamageCreatures,DBDamageOpponents,DBDestroy | "
    "CharmNum$ 3 | CanRepeatModes$ True\n"
    "SVar:DBDamageCreatures:DB$ DamageAll | NumDmg$ 1 | ValidCards$ Creature | "
    "SpellDescription$ CARDNAME deals 1 damage to each creature.\n"
    "SVar:DBDamageOpponents:DB$ DealDamage | Defined$ Player.Opponent | "
    "NumDmg$ 2 | AILogic$ Good | SpellDescription$ CARDNAME deals 2 damage to "
    "each opponent.\n"
    "SVar:DBDestroy:DB$ Destroy | ValidTgts$ Artifact | "
    "SpellDescription$ Destroy target artifact.\n"
    "Oracle:Choose three. You may choose the same mode more than once.\\n"
    "• Fiery Confluence deals 1 damage to each creature.\\n"
    "• Fiery Confluence deals 2 damage to each opponent.\\n"
    "• Destroy target artifact.\n"
)

MULTIFACE_DFC = (
    "Name:Test Front\n"
    "ManaCost:1 U\n"
    "Types:Creature Human\n"
    "PT:2/2\n"
    "T:Mode$ Phase | Phase$ Upkeep | Execute$ TrigX | "
    "TriggerDescription$ At the beginning of your upkeep, draw a card.\n"
    "SVar:TrigX:DB$ Draw | NumCards$ 1\n"
    "AlternateMode:DoubleFaced\n"
    "Oracle:At the beginning of your upkeep, draw a card.\n"
    "\n"
    "ALTERNATE\n"
    "\n"
    "Name:Test Back\n"
    "ManaCost:no cost\n"
    "Colors:blue\n"
    "Types:Creature Werewolf\n"
    "PT:3/3\n"
    "Oracle:Flying.\n"
)

SPECIALIZE_CARD = (
    "Name:Test Specialize\n"
    "ManaCost:2 G\n"
    "Types:Legendary Creature Elf\n"
    "PT:2/2\n"
    "AlternateMode:Specialize\n"
    "Oracle:Specialize {2}.\n"
    "\n"
    "SPECIALIZE:WHITE\n"
    "\n"
    "Name:Test Specialize White\n"
    "ManaCost:2 W\n"
    "Types:Legendary Creature Elf\n"
    "PT:3/3\n"
    "Oracle:Vigilance.\n"
)

TRIGGER_STATIC = (
    "Name:Test Trigger\n"
    "ManaCost:2 R\n"
    "Types:Enchantment\n"
    "K:Flying\n"
    "T:Mode$ ChangesZone | Origin$ Battlefield | Destination$ Graveyard | "
    "ValidCard$ Creature | TriggerZones$ Battlefield | Execute$ TrigDmg | "
    "TriggerDescription$ When a creature dies, deal 1 damage.\n"
    "SVar:TrigDmg:DB$ DealDamage | Defined$ Player | NumDmg$ 1\n"
    "S:Mode$ Continuous | Affected$ Creature.YouCtrl | AddPower$ 1 | "
    "Description$ Creatures you control get +1/+0.\n"
    "R:Event$ DamageDone | ActiveZones$ Battlefield | ValidTarget$ Player | "
    "ReplaceWith$ DmgReplace | Description$ Prevent damage.\n"
    "SVar:DmgReplace:DB$ ReplaceEffect | VarName$ Prevented | VarValue$ True\n"
)

MALFORMED = (
    "Name:Test Malformed\n"
    "this line has no colon\n"
    "ManaCost:1 G\n"
    "SVar:NoColonName\n"
    "K:\n"
    "A:AB$\n"
    "Types:Creature\n"
    "Oracle:broken\n"
)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


class TestParserSamples:
    def test_grizzly_bears_has_no_effects(self):
        card = corpus_parser.parse_script(GRIZZLY_BEARS)
        assert card.parse_ok
        assert len(card.faces) == 1
        face = card.faces[0]
        assert face.name == "Grizzly Bears"
        assert face.mana_cost == "1 G"
        assert face.types == "Creature Bear"
        assert face.pt == "2/2"
        assert card.effects == []
        assert card.abilities == []
        assert card.svars == {}

    def test_kiki_jiki_copy_permanent(self):
        card = corpus_parser.parse_script(KIKI_JIKI)
        assert card.parse_ok
        assert len(card.effects) == 1
        effect = card.effects[0]
        assert (effect.kind, effect.ability_type, effect.verb) == ("ability", "AB", "CopyPermanent")
        assert effect.params["Cost"] == "T"
        assert effect.params["ValidTgts"] == "Creature.nonLegendary+YouCtrl"
        assert effect.zone == "Battlefield"
        assert not effect.is_optional
        assert [a.keyword for a in card.abilities] == ["Haste"]

    def test_fiery_confluence_charm_with_three_db_svars(self):
        card = corpus_parser.parse_script(FIERY_CONFLUENCE)
        assert card.parse_ok
        assert len(card.effects) == 1
        charm = card.effects[0]
        assert (charm.kind, charm.ability_type, charm.verb) == ("ability", "SP", "Charm")
        assert charm.zone == "Stack"
        assert charm.params["CharmNum"] == "3"
        assert set(charm.params["Choices"].split(",")) == {
            "DBDamageCreatures", "DBDamageOpponents", "DBDestroy"
        }
        assert len(card.svars) == 3
        assert {s.kind for s in card.svars.values()} == {"DB"}
        # 1 Charm + 3 DB SVar abilities.
        assert len(card.all_effects) == 4
        svars = [e.svar_name for e in card.all_effects if e.svar_name]
        assert sorted(svars) == ["DBDamageCreatures", "DBDamageOpponents", "DBDestroy"]

    def test_multiface_splits_on_alternate_marker(self):
        card = corpus_parser.parse_script(MULTIFACE_DFC)
        assert card.parse_ok
        assert card.is_multiface
        assert card.alternate_mode == "DoubleFaced"
        assert [f.name for f in card.faces] == ["Test Front", "Test Back"]
        assert card.faces[0].marker is None
        assert card.faces[1].marker == "ALTERNATE"
        assert len(card.effects) == 1 and card.effects[0].kind == "trigger"

    def test_specialize_marker_begins_a_face(self):
        card = corpus_parser.parse_script(SPECIALIZE_CARD)
        assert card.parse_ok
        assert card.alternate_mode == "Specialize"
        assert [f.marker for f in card.faces] == [None, "SPECIALIZE:WHITE"]
        assert len(card.faces) == 2

    def test_trigger_static_replacement_kinds(self):
        card = corpus_parser.parse_script(TRIGGER_STATIC)
        assert card.parse_ok
        kinds = {e.kind for e in card.effects}
        assert kinds == {"trigger", "static", "replacement"}
        trigger = next(e for e in card.effects if e.kind == "trigger")
        assert trigger.ability_type == "ChangesZone"
        assert trigger.params["Origin"] == "Battlefield"
        static = next(e for e in card.effects if e.kind == "static")
        assert static.params["Affected"] == "Creature.YouCtrl"
        assert trigger.zone == "Battlefield"

    def test_option_targets_detected(self):
        card = corpus_parser.parse_script(
            "Name:X\nTypes:Creature\n"
            "A:AB$ Pump | ValidTgts$ Creature | TargetMin$ 0 | TargetMax$ 1\n"
        )
        assert card.effects[0].is_optional

    def test_malformed_lines_are_collected_not_raised(self):
        card = corpus_parser.parse_script(MALFORMED)
        assert not card.parse_ok
        reasons = [e.reason for e in card.parse_errors]
        assert len(card.parse_errors) >= 4
        assert any("Key:Value" in r for r in reasons)
        assert any("SVar" in r for r in reasons)
        assert any("keyword" in r for r in reasons)
        # Still parsed the good lines and kept the card.
        assert card.faces[0].name == "Test Malformed"
        assert card.faces[0].mana_cost == "1 G"


class TestNameNormalization:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Grizzly Bears", "grizzly bears"),
            ("Kiki-Jiki, Mirror Breaker", "kiki jiki mirror breaker"),
            ("Bind // Liberate", "bind liberate"),
            ("Lim-Dûl's Vault", "lim dul s vault"),
            ("", ""),
        ],
    )
    def test_normalize(self, raw, expected):
        assert normalize_name(raw) == expected


# ---------------------------------------------------------------------------
# Importer
# ---------------------------------------------------------------------------


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def mini_corpus(tmp_path: Path) -> Path:
    """A synthetic Forge checkout layout with 7 scripts (one malformed)."""
    folder = tmp_path / "forge-gui" / "res" / "cardsfolder"
    _write(folder / "g" / "grizzly_bears.txt", GRIZZLY_BEARS)
    _write(folder / "k" / "kiki_jiki_mirror_breaker.txt", KIKI_JIKI)
    _write(folder / "f" / "fiery_confluence.txt", FIERY_CONFLUENCE)
    _write(folder / "m" / "multiface.txt", MULTIFACE_DFC)
    _write(folder / "s" / "specialize.txt", SPECIALIZE_CARD)
    _write(folder / "t" / "trigger_static.txt", TRIGGER_STATIC)
    _write(folder / "x" / "malformed.txt", MALFORMED)
    return tmp_path


def _query(path: Path, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


class TestImporter:
    def test_imports_tables_and_reports(self, mini_corpus):
        db = mini_corpus / "research.db"
        store = ExperimentStore(db)
        report = import_corpus(store, mini_corpus)
        store.close()

        assert isinstance(report, ImportReport)
        assert report.cards_total == 7
        assert report.parse_ok == 6
        assert report.parse_error_cards == 1
        assert report.abilities == 2
        assert report.svars == 7  # 1 kiki + 3 fiery + 1 multiface + 2 trigger/static
        # 6 direct A/T/S/R effects + 6 SVar-defined effects.
        assert report.effects == 12
        assert report.faces == 9
        assert report.aliases >= 7
        assert report.scryfall_source == "forge_script"

        counts = {
            table: _query(db, f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]
            for table in (
                "import_runs", "cards", "card_faces", "card_aliases",
                "card_scripts", "card_effects", "corpus_coverage",
            )
        }
        assert counts["import_runs"] == 1
        assert counts["cards"] == 7
        assert counts["card_faces"] == 9
        assert counts["card_scripts"] == 7
        assert counts["card_effects"] == 12
        assert counts["card_aliases"] == report.aliases
        assert counts["corpus_coverage"] > 6

    def test_effects_rows_are_queryable(self, mini_corpus):
        db = mini_corpus / "research.db"
        store = ExperimentStore(db)
        import_corpus(store, mini_corpus)
        store.close()

        kiki = _query(
            db,
            "SELECT e.effect_kind, e.verb_or_mode, e.ability_type, e.zone, e.params_json "
            "FROM card_effects e JOIN cards c ON c.id = e.card_id "
            "WHERE c.normalized_name = ?",
            ("kiki jiki mirror breaker",),
        )
        assert len(kiki) == 1
        row = kiki[0]
        assert (row["effect_kind"], row["verb_or_mode"], row["ability_type"]) == (
            "ability", "CopyPermanent", "AB"
        )
        assert row["zone"] == "Battlefield"
        assert json.loads(row["params_json"])["Cost"] == "T"

        fiery = _query(
            db,
            "SELECT e.verb_or_mode, e.is_svar, e.svar_name FROM card_effects e "
            "JOIN cards c ON c.id = e.card_id WHERE c.normalized_name = ? "
            "ORDER BY e.is_svar, e.svar_name",
            ("fiery confluence",),
        )
        assert fiery[0]["verb_or_mode"] == "Charm"
        assert [r["svar_name"] for r in fiery[1:]] == [
            "DBDamageCreatures", "DBDamageOpponents", "DBDestroy"
        ]

        trigger = _query(
            db, "SELECT COUNT(*) AS n FROM card_effects WHERE effect_kind = 'trigger'"
        )
        assert trigger[0]["n"] == 2

    def test_aliases_and_coverage(self, mini_corpus):
        db = mini_corpus / "research.db"
        store = ExperimentStore(db)
        report = import_corpus(store, mini_corpus)
        store.close()

        alias_rows = _query(
            db, "SELECT alias, alias_kind FROM card_aliases ORDER BY alias"
        )
        aliases = {(r["alias"], r["alias_kind"]) for r in alias_rows}
        assert ("Kiki-Jiki, Mirror Breaker", "face") in aliases
        assert ("Test Back", "face") in aliases
        assert ("Test Front // Test Back", "combined") in aliases

        coverage = {
            r["metric"]: r["value"]
            for r in _query(db, "SELECT metric, value FROM corpus_coverage")
        }
        assert coverage["total_cards"] == 7
        assert coverage["parse_ok"] == 6
        assert coverage["parse_error_cards"] == 1
        verbs = {
            r["key"]
            for r in _query(
                db, "SELECT key FROM corpus_coverage WHERE metric='effects_by_verb'"
            )
        }
        assert {"CopyPermanent", "Charm"} <= verbs
        modes = {
            r["key"]
            for r in _query(
                db, "SELECT key FROM corpus_coverage WHERE metric='triggers_by_mode'"
            )
        }
        assert "ChangesZone" in modes
        # The report mirrors the persisted coverage totals.
        assert report.coverage["total_cards"] == 7

    def test_resumable_second_import_appends(self, mini_corpus):
        db = mini_corpus / "research.db"
        store = ExperimentStore(db)
        first = import_corpus(store, mini_corpus)
        second = import_corpus(store, mini_corpus)
        store.close()

        assert first.import_id != second.import_id
        assert _query(db, "SELECT COUNT(*) AS n FROM import_runs")[0]["n"] == 2
        assert _query(db, "SELECT COUNT(*) AS n FROM cards")[0]["n"] == 14
        assert _query(db, "SELECT COUNT(DISTINCT import_id) AS n FROM cards")[0]["n"] == 2
        # Every child row points at a real import run (FK holds).
        dangling = _query(
            db,
            "SELECT COUNT(*) AS n FROM card_effects e "
            "LEFT JOIN import_runs r ON r.import_id = e.import_id "
            "WHERE r.import_id IS NULL",
        )[0]["n"]
        assert dangling == 0

    def test_schema_version_is_six(self, mini_corpus):
        db = mini_corpus / "research.db"
        store = ExperimentStore(db)
        versions = [r[0] for r in store._conn.execute("SELECT version FROM schema_version")]
        store.close()
        # Fresh databases step 1 -> 2 -> 3 -> 4 -> 5 -> 6 through the migration registry.
        assert versions == [1, 2, 3, 4, 5, 6]

    def test_migration_function_creates_corpus_tables(self, tmp_path):
        conn = sqlite3.connect(tmp_path / "raw.db")
        _migration_2(conn)
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        conn.close()
        assert {
            "import_runs", "cards", "card_faces", "card_aliases",
            "card_scripts", "card_effects", "corpus_coverage",
        } <= tables

    def test_card_filter_and_limit(self, mini_corpus):
        db = mini_corpus / "research.db"
        store = ExperimentStore(db)
        report = import_corpus(store, mini_corpus, limit=3)
        store.close()
        assert report.cards_total == 3
        assert _query(db, "SELECT COUNT(*) AS n FROM cards")[0]["n"] == 3

        db2 = mini_corpus / "research2.db"
        store2 = ExperimentStore(db2)
        report2 = import_corpus(
            store2, mini_corpus, card_filter=lambda p: p.name == "grizzly_bears.txt"
        )
        store2.close()
        assert report2.cards_total == 1

    def test_export_jsonl_for_new_tables(self, mini_corpus):
        db = mini_corpus / "research.db"
        store = ExperimentStore(db)
        import_corpus(store, mini_corpus)
        cards_out = mini_corpus / "cards.jsonl"
        effects_out = mini_corpus / "effects.jsonl"
        store.export_jsonl("cards", cards_out)
        store.export_jsonl("card_effects", effects_out)
        store.close()
        assert len(cards_out.read_text().splitlines()) == 7
        assert len(effects_out.read_text().splitlines()) == 12

    def test_store_binding_sees_imported_cards(self, mini_corpus):
        db = mini_corpus / "research.db"
        store = ExperimentStore(db)
        import_corpus(store, mini_corpus)
        store.close()

        binding = StoreBinding(db)
        schema = binding.card_schema()
        assert schema is not None and schema.name == "name"
        assert binding.card_count() == 7
        assert binding.card_count("kiki") == 1
        cards = binding.list_cards("fiery")
        assert len(cards) == 1
        assert cards[0]["name"] == "Fiery Confluence"
        # effect_count column feeds the view's effect counter.
        assert cards[0]["effects"] == 1
        detail = binding.card(cards[0]["id"])
        assert detail is not None and detail["mana"] == "2 R R"
        binding.close()

    def test_resolve_cardsfolder(self, mini_corpus):
        assert resolve_cardsfolder(mini_corpus).name == "cardsfolder"
        assert resolve_cardsfolder(mini_corpus / "forge-gui" / "res" / "cardsfolder").name == "cardsfolder"


class _FakeScryfall:
    """Duck-typed stand-in: no network, deterministic enrichment."""

    download_uri = "https://example.test/oracle_cards.json"
    sha256 = "a" * 64

    def load(self) -> bool:
        return True

    def lookup(self, name: str):
        if normalize_name(name) == "kiki jiki mirror breaker":
            return {
                "name": "Kiki-Jiki, Mirror Breaker",
                "oracle_text": "Haste\n{T}: Create a token.",
                "type_line": "Legendary Creature — Goblin Shaman",
                "mana_cost": "{2}{R}{R}{R}",
                "colors": ["R"],
                "legalities": {"vintage": "legal"},
                "set": "chk",
                "collector_number": "172",
                "rarity": "rare",
                "layout": "normal",
                "oracle_id": "oracle-kiki",
                "card_faces": [],
            }
        return None


class TestScryfall:
    def test_optional_enrichment_matches_by_name(self, mini_corpus):
        db = mini_corpus / "research.db"
        store = ExperimentStore(db)
        report = import_corpus(store, mini_corpus, scryfall=_FakeScryfall())
        store.close()

        assert report.scryfall_source == "scryfall"
        assert report.scryfall_sha256 == "a" * 64
        assert report.scryfall_matched == 1
        assert report.scryfall_unmatched == 6

        row = _query(
            db,
            "SELECT set_code, rarity, oracle_text FROM cards WHERE normalized_name = ?",
            ("kiki jiki mirror breaker",),
        )[0]
        assert row["set_code"] == "chk"
        assert row["rarity"] == "rare"
        assert row["oracle_text"] == "Haste\n{T}: Create a token."

    def test_missing_cache_is_offline_safe(self, tmp_path):
        from combo_discovery.corpus.importer import ScryfallSource

        source = ScryfallSource(cache_dir=tmp_path / "nope", auto_download=False)
        assert source.load() is False
        assert source.lookup("anything") is None
