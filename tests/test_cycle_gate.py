"""Unit + hermetic acceptance tests for the static cycle gate.

The synthetic tests exercise the cycle logic directly (no corpus): closure,
cap gates, resource leaks, and the additive fresh-token closure rule.  The
hermetic corpus tests replay the gate on the vendored real Forge scripts, so the
documented near-miss (a "once each turn" limit) and the Kiki/Splinter-Twin shape
are checked against real card text.  No network, no writes to ``research.db``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from combo_discovery.corpus.importer import import_corpus, normalize_name
from combo_discovery.ontology.cycle_gate import (
    CAP_GATES,
    COPY_REFRESH,
    SIGNAL_CAPPED,
    SIGNAL_NONE,
    SIGNAL_UNBOUNDED,
    VerdictRow,
    _auc,
    cap_gates,
    check_card_ids,
    check_signatures,
    enumerate_cycles,
    refresh_links,
    resolve_card_id,
    resource_leaks,
    validate,
)
from combo_discovery.ontology.extractor import CardContext, load_contexts
from combo_discovery.ontology.links import LinkOptions, build_links
from combo_discovery.ontology.ports import AbilitySig, Port
from combo_discovery.ontology.restrictions import parse_restriction
from combo_discovery.store import ExperimentStore

FIXTURES = Path(__file__).parent / "fixtures" / "ontology_cards"
FORGE_CARDSFOLDER = Path("/home/lza/Work/forge/forge-gui/res/cardsfolder")


# ---------------------------------------------------------------------------
# Synthetic builders
# ---------------------------------------------------------------------------


def _context(card_id: int, name: str, type_line: str = "Creature") -> CardContext:
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
    gates: tuple[Port, ...] = (),
) -> AbilitySig:
    return AbilitySig(
        card_id=card_id,
        card_name=name,
        ability_ref=f"line:0:{card_id}:A:X",
        ability_kind=kind,
        triggers_on=trigger,
        consumes=consumes,
        produces=produces,
        gates=gates,
        raw={"context": _context(card_id, name, type_line)},
    )


def _mana(colors: tuple[str, ...] = ("B",)) -> Port:
    return Port("mana", {"colors": colors, "predicate": "PRODUCES_MANA"})


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


def _gate(kind: str) -> Port:
    return Port(kind, {"raw": True, "predicate": kind.upper()})


def _mana_cycle() -> list[AbilitySig]:
    """Two abilities that each produce the mana the other consumes."""
    return [
        _sig(1, "Well", consumes=(_mana(),), produces=(_mana(),)),
        _sig(2, "Spout", consumes=(_mana(),), produces=(_mana(),)),
    ]


# ---------------------------------------------------------------------------
# Synthetic cycle logic
# ---------------------------------------------------------------------------


class TestSyntheticCycles:
    def test_mana_two_cycle_is_unbounded(self):
        evidence = check_signatures(_mana_cycle(), max_len=2)
        assert evidence.closed is True
        assert evidence.unbounded is True
        assert evidence.signal == SIGNAL_UNBOUNDED
        assert {step.src for step in evidence.path} == {"Well", "Spout"}
        assert len(evidence.path) == 2

    def test_turn_restriction_cap_blocks_unbounded(self):
        """The documented near-miss: a closed cycle limited to once each turn."""
        sigs = _mana_cycle()
        sigs[0] = _sig(1, "Well", consumes=(_mana(),), produces=(_mana(),),
                       gates=(_gate("turn_restriction"),))
        evidence = check_signatures(sigs, max_len=2)
        assert evidence.closed is True
        assert evidence.unbounded is False
        assert evidence.signal == SIGNAL_CAPPED
        assert "turn_restriction" in evidence.caps

    def test_uncovered_consume_is_not_closed(self):
        sigs = [
            _sig(1, "Well", consumes=(_mana(), Port("life_loss", {"amount": 2})),
                 produces=(_mana(),)),
            _sig(2, "Spout", consumes=(_mana(),), produces=(_mana(),)),
        ]
        evidence = check_signatures(sigs, max_len=2)
        assert evidence.closed is False
        assert evidence.unbounded is False
        assert evidence.signal == SIGNAL_NONE
        assert "Well:life_loss" in evidence.leaks
        # A structural cycle is still reported, so the near-miss is inspectable.
        assert evidence.path

    def test_no_cycle_returns_signal_none(self):
        sigs = [
            _sig(1, "Well", produces=(_mana(),)),
            _sig(2, "Idle", produces=(_mana(),)),
        ]
        evidence = check_signatures(sigs, max_len=2)
        assert evidence.closed is False
        assert evidence.signal == SIGNAL_NONE
        assert evidence.path == ()

    def test_copy_refresh_closes_activated_engine(self):
        """Kiki/Jelpie shape: the fresh token's *activated* untap closes the loop."""
        engine = _sig(1, "Engine", consumes=(_tap(),), produces=(_copy(),))
        untapper = _sig(2, "Untapper", consumes=(_tap(),), produces=(_untap(),))
        evidence = check_signatures([engine, untapper], max_len=2)
        assert evidence.closed is True
        assert evidence.unbounded is True
        assert any(step.kind == COPY_REFRESH for step in evidence.path)

    def test_triggered_engine_is_not_refreshed_by_default(self):
        engine = _sig(1, "Engine", kind="trigger",
                      trigger=Port("attacks", {"self": True, "predicate": "ATTACKS"}),
                      consumes=(_tap(),), produces=(_copy(),))
        untapper = _sig(2, "Untapper", consumes=(_tap(),), produces=(_untap(),))
        assert refresh_links([engine, untapper]) == []
        evidence = check_signatures([engine, untapper], max_len=2)
        assert evidence.closed is False
        # ...but the caller can opt in to triggered engines.
        evidence = check_signatures(
            [engine, untapper], max_len=2, include_triggered=True
        )
        assert evidence.closed is True

    def test_cap_gates_lists_only_cap_kinds(self):
        sigs = [
            _sig(1, "Well", consumes=(_mana(),), produces=(_mana(),),
                 gates=(_gate("delirium"), _gate("turn_restriction"))),
        ]
        assert cap_gates(sigs) == ("turn_restriction",)
        assert "delirium" not in CAP_GATES

    def test_enumerate_cycles_dedups_and_bounds(self):
        sigs = [
            _sig(1, "A", consumes=(_mana(),), produces=(_mana(),)),
            _sig(2, "B", consumes=(_mana(),), produces=(_mana(),)),
            _sig(3, "C", consumes=(_mana(),), produces=(_mana(),)),
        ]
        links = build_links(sigs, options=LinkOptions())
        two = enumerate_cycles(links, max_len=2)
        three = enumerate_cycles(links, max_len=3)
        assert len(two) == 3  # one per unordered pair, not per rotation
        assert len(three) == 4  # the three pairs plus the A-B-C triple

    def test_resource_leaks_reports_uncovered_consumes(self):
        sigs = [
            _sig(1, "A", consumes=(_mana(),), produces=(_mana(),)),
            _sig(2, "B", consumes=(_mana(), Port("discard", {"cost": True})),
                 produces=(_mana(),)),
        ]
        leaks = resource_leaks(sigs)
        assert "B:discard" in leaks


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


class TestValidationHelpers:
    def _row(self, verdict: str, signal: float, score: float) -> VerdictRow:
        from combo_discovery.ontology.cycle_gate import CycleEvidence

        evidence = CycleEvidence(
            cards=("A", "B"),
            closed=signal >= SIGNAL_CAPPED,
            unbounded=signal >= SIGNAL_UNBOUNDED,
            signal=signal,
        )
        return VerdictRow(
            run_id=1, verdict=verdict, cards=("A", "B"), card_ids=(1, 2),
            engine_class="activated", evidence=evidence, score=score,
            existing_score=score,
        )

    def test_validate_excludes_inconclusive(self):
        rows = [
            self._row("loops", SIGNAL_UNBOUNDED, 0.8),
            self._row("no_loop", SIGNAL_UNBOUNDED, 0.0),
            self._row("loops", SIGNAL_NONE, 0.0),
            self._row("inconclusive", SIGNAL_UNBOUNDED, 0.9),
        ]
        result = validate(rows)
        assert result["inconclusive_excluded"] == 1
        assert result["scored"] == 3
        static = result["overall"]["static_unbounded"]
        assert static["tp"] == 1 and static["fp"] == 1
        assert static["fn"] == 1 and static["tn"] == 0

    def test_auc_perfect_and_tied(self):
        assert _auc([1.0, 1.0, 0.0, 0.0], [True, True, False, False]) == 1.0
        assert _auc([0.5, 0.5], [True, False]) == 0.5


# ---------------------------------------------------------------------------
# Hermetic real-corpus replay
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
def corpus(tmp_path_factory) -> dict:
    root = _seed_cardsfolder(tmp_path_factory.mktemp("cycle_gate"))
    store = ExperimentStore(root / "research.db")
    try:
        import_corpus(store, root)
        import_id = store._conn.execute(
            "SELECT import_id FROM import_runs ORDER BY rowid DESC LIMIT 1"
        ).fetchone()[0]
        contexts, effects = load_contexts(store._conn, import_id)
    finally:
        store.close()
    return {"contexts": contexts, "effects": effects}


class TestHermeticCorpus:
    def _evidence(self, corpus, *names):
        ids = [resolve_card_id(corpus["contexts"], name) for name in names]
        assert all(cid is not None for cid in ids), f"missing fixture cards: {names}"
        return check_card_ids(
            corpus["contexts"], corpus["effects"], ids, max_len=2
        )

    def test_kiki_exarch_is_an_unbounded_cycle(self, corpus):
        evidence = self._evidence(
            corpus, "Kiki-Jiki, Mirror Breaker", "Deceiver Exarch"
        )
        assert evidence.unbounded is True
        assert evidence.signal == SIGNAL_UNBOUNDED

    def test_kiki_fear_of_missing_out_is_capped(self, corpus):
        """A real 'once each turn' near-miss: the untap is FirstAttack-gated."""
        evidence = self._evidence(
            corpus, "Kiki-Jiki, Mirror Breaker", "Fear of Missing Out"
        )
        assert evidence.closed is True
        assert evidence.unbounded is False
        assert "first_attack" in evidence.caps

    def test_splinter_twin_corridor_monitor_is_a_known_limit(self, corpus):
        """Honest limit: Splinter Twin is an Aura, so the untap cannot target the
        engine card and the graph does not close the loop (the real combo loops
        on the enchanted creature, which this ability-scoped graph cannot model).
        """
        evidence = self._evidence(
            corpus, "Splinter Twin", "Corridor Monitor"
        )
        assert evidence.unbounded is False

    def test_mana_cost_leak_is_not_closed(self, corpus):
        """Janjeet Sentry's untap costs mana the cycle does not refund."""
        evidence = self._evidence(
            corpus, "Kiki-Jiki, Mirror Breaker", "Janjeet Sentry"
        )
        assert evidence.closed is False
        assert any("mana" in leak for leak in evidence.leaks)
