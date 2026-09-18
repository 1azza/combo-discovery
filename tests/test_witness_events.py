"""Live witness narration: schema, streaming, kinds, and determinism.

The harness is faked and the narration is streamed through the real store, so
these tests exercise the event -> row mapping and the live-read contract without
touching a server.
"""

from __future__ import annotations

import json
import sqlite3

from combo_discovery.generated import forge_env_pb2 as pb
from combo_discovery.store import ExperimentStore
from combo_discovery.witness import (
    NARRATION_KINDS,
    LinkPlan,
    WitnessPolicy,
    build_scenario,
    narrate_game_event,
    persist_witness,
    run_witness,
    start_witness_recording,
)

KIKI = "Kiki-Jiki, Mirror Breaker"
DECEIVER = "Deceiver Exarch"

#: One Kiki-Jiki loop turn exactly as the harness broadcasts it.
KIKI_SCRIPT = [
    ("CardTapped", KIKI, "", "tapped=true"),
    (
        "SpellCast",
        KIKI,
        f"A activated {KIKI} targeting [{DECEIVER} (126)]",
        f"sa={{T}}: Create a token that's a copy of target nonlegendary creature "
        f"you control.;targets=[{DECEIVER} (126)];stack=0",
    ),
    ("PermanentEntered", DECEIVER, "", "from=null;to=Battlefield"),
    (
        "SpellResolved",
        KIKI,
        f"{KIKI} (125) - A creates a token that's a copy of {DECEIVER} (126), "
        "except it has haste. Sacrifice it at the beginning of the next end step.",
        f"stack={KIKI} (125) - A creates a token that's a copy of {DECEIVER} "
        "(126), except it has haste.;fizzled=false",
    ),
    (
        "SpellCast",
        DECEIVER,
        f"A triggered {DECEIVER}",
        "sa=When Deceiver Exarch enters, untap target permanent you control. "
        f"(Targeting: {KIKI} (125));stack=0",
    ),
    ("CardTapped", KIKI, "", "tapped=false"),
]


def _state(mana: int = 0, hash_: str = "H") -> pb.FullState:
    state = pb.FullState(
        game_id=1, turn=1, phase="MAIN1", active_player=0, state_hash=hash_
    )
    state.life.extend([20, 20])
    state.typed_mana_pools.add(colorless=mana)
    state.battlefield_cards.append(pb.Permanent(id=0, card_name=KIKI))
    state.battlefield.add().permanents.append(0)
    return state


class FakeKikiClient:
    """A deterministic Kiki-Jiki loop: Kiki activation plus the event stream."""

    def __init__(self):
        self.game_id = 77
        self.mana = 0
        self.stopped = None
        self._decisions = 0
        self._polls = 0
        self._emitted = 0

    def connect(self):
        return None

    def close(self):
        return None

    def start_game(self, decks, seed, player_types=None, **kwargs):
        return self.game_id

    def setup_scenario(self, game_id, scenario):
        return "H0", 3

    def get_decision(self, game_id):
        self._decisions += 1
        return pb.DecisionRequest(
            game_id=game_id,
            decision_id=self._decisions,
            player=0,
            turn=1,
            phase="MAIN1",
            decision_type=pb.DECISION_TYPE_PRIORITY,
            options=[pb.Option(id=0, kind="activate", card_name=KIKI)],
        )

    def submit_decision(self, game_id, decision_id, answer):
        self.mana += 1
        return pb.StepResult()

    def is_game_over(self, game_id):
        return pb.GameOver(over=False)

    def get_state(self, game_id, view_as_player=0):
        return _state(mana=self.mana, hash_=f"H{self.mana}")

    def poll_events(self, game_id, cursor=0):
        self._polls += 1
        if self._polls == 1:  # cursor-establishment poll: nothing yet
            return pb.EventBatch(next_cursor=cursor)
        events = []
        while self._emitted < len(KIKI_SCRIPT):
            etype, card, detail, extra = KIKI_SCRIPT[self._emitted]
            self._emitted += 1
            events.append(
                pb.GameEvent(
                    seq=self._emitted,
                    game_id=game_id,
                    type=etype,
                    player=0,
                    turn=1,
                    phase="MAIN1",
                    card_name=card,
                    detail_raw=detail,
                    extra=extra,
                )
            )
        return pb.EventBatch(events=events, next_cursor=len(KIKI_SCRIPT))

    def stop_game(self, game_id):
        self.stopped = game_id


class FakeRefutedClient:
    """A non-looping, eventless game that ends before any recurrence."""

    def __init__(self):
        self.game_id = 9
        self._decisions = 0
        self._submits = 0
        self._states = 0
        self.stopped = None

    def start_game(self, decks, seed, player_types=None, **kwargs):
        return self.game_id

    def setup_scenario(self, game_id, scenario):
        return "H0", 3

    def get_decision(self, game_id):
        self._decisions += 1
        return pb.DecisionRequest(
            game_id=game_id,
            decision_id=self._decisions,
            player=0,
            turn=1,
            phase="MAIN1",
            decision_type=pb.DECISION_TYPE_PRIORITY,
            options=[pb.Option(id=0, kind="activate", card_name="Altar")],
        )

    def submit_decision(self, game_id, decision_id, answer):
        self._submits += 1
        return pb.StepResult()

    def is_game_over(self, game_id):
        # Two answered loop decisions are enough for a signature recurrence.
        return pb.GameOver(over=self._submits >= 2)

    def get_state(self, game_id, view_as_player=0):
        self._states += 1
        return _state(mana=0, hash_=f"S{self._states}")

    def poll_events(self, game_id, cursor=0):
        return pb.EventBatch(next_cursor=cursor)

    def stop_game(self, game_id):
        self.stopped = game_id


def _kiki_policy() -> WitnessPolicy:
    return WitnessPolicy(links=[LinkPlan(KIKI, DECEIVER)], player=0)


def _persisted_run(db_path, client, *, max_iterations=2, max_decisions=4):
    store = ExperimentStore(db_path)
    scenario = build_scenario((KIKI, DECEIVER))
    run_id, recorder = start_witness_recording(
        store,
        scenario=scenario,
        seeds=[1],
        candidate_key="1048622",
        card_names=(KIKI, DECEIVER),
    )
    result = run_witness(
        client,
        scenario,
        _kiki_policy(),
        seeds=[1],
        max_iterations=max_iterations,
        max_decisions=max_decisions,
        recorder=recorder,
        candidate_key="1048622",
        card_names=(KIKI, DECEIVER),
    )
    persist_witness(store, result, run_id=run_id)
    return store, run_id, result


# ---------------------------------------------------------------------------
# Pure event -> narration mapping
# ---------------------------------------------------------------------------


class TestNarrationMapping:
    def test_copy_row_names_source_and_target(self):
        event = pb.GameEvent(
            type="SpellResolved",
            card_name=KIKI,
            turn=1,
            phase="MAIN1",
            detail_raw=(
                f"{KIKI} (125) - A creates a token that's a copy of {DECEIVER} (126)."
            ),
        )
        row = narrate_game_event(event)
        assert row is not None
        assert row.kind == "copy"
        assert row.text == f"{KIKI} copies {DECEIVER}"
        assert (row.actor, row.target) == (KIKI, DECEIVER)

    def test_untap_row(self):
        event = pb.GameEvent(
            type="CardTapped", card_name=KIKI, turn=1, phase="MAIN1",
            extra="tapped=false",
        )
        row = narrate_game_event(event)
        assert row is not None and row.kind == "untap"
        assert KIKI in row.text

    def test_token_row_is_the_etb_sentence(self):
        event = pb.GameEvent(
            type="PermanentEntered", card_name=DECEIVER, turn=1, phase="MAIN1",
            extra="from=null;to=Battlefield",
        )
        row = narrate_game_event(event)
        assert row is not None
        assert row.kind == "token"
        assert row.text == f"{DECEIVER} enters the battlefield"

    def test_trigger_and_activate_carry_targets(self):
        trigger = pb.GameEvent(
            type="SpellCast", card_name=DECEIVER, turn=1, phase="MAIN1",
            detail_raw=f"A triggered {DECEIVER}",
            extra=(
                "sa=When Deceiver Exarch enters, untap target permanent you control. "
                f"(Targeting: {KIKI} (125));stack=0"
            ),
        )
        row = narrate_game_event(trigger)
        assert row is not None and row.kind == "trigger"
        assert row.target == KIKI

        activate = pb.GameEvent(
            type="SpellCast", card_name=KIKI, turn=1, phase="MAIN1",
            detail_raw=f"A activated {KIKI} targeting [{DECEIVER} (126)]",
        )
        row = narrate_game_event(activate)
        assert row is not None and row.kind == "activate"
        assert row.target == DECEIVER

    def test_silent_events_are_not_narrated(self):
        for etype in ("CardDrawn", "Phase", "TurnStarted", "Shuffled", "ManaProduced"):
            assert narrate_game_event(pb.GameEvent(type=etype)) is None

    def test_every_mapped_kind_is_in_the_vocabulary(self):
        for etype, card, detail, extra in KIKI_SCRIPT:
            row = narrate_game_event(
                pb.GameEvent(
                    type=etype, card_name=card, turn=1, phase="MAIN1",
                    detail_raw=detail, extra=extra,
                )
            )
            if row is not None:
                assert row.kind in NARRATION_KINDS


# ---------------------------------------------------------------------------
# Persisted live runs
# ---------------------------------------------------------------------------


class TestLiveNarration:
    def test_looping_pair_streams_copy_and_untap(self, tmp_path):
        store, run_id, result = _persisted_run(
            tmp_path / "w.sqlite", FakeKikiClient()
        )
        rows = store.witness_events(run_id)
        runs = store.witness_runs()
        store.close()

        seqs = [r["seq"] for r in rows]
        assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))
        kinds = [r["kind"] for r in rows]
        assert "copy" in kinds
        assert "untap" in kinds
        assert set(kinds) <= NARRATION_KINDS
        # Cards named in text/actor/target, not internal vocabulary.
        copy_rows = [r for r in rows if r["kind"] == "copy"]
        assert any(
            KIKI in (r["text"] or "") and DECEIVER in (r["text"] or "")
            for r in copy_rows
        )
        assert any(r["actor"] == KIKI for r in rows)
        assert any(r["target"] == DECEIVER for r in rows)
        # Pair is identifiable at run start, before any result row exists.
        assert runs[0]["candidate_key"] == "1048622"
        assert json.loads(runs[0]["card_names_json"]) == [KIKI, DECEIVER]
        # Live rows were written by the recorder, so the end-of-run backfill
        # must not duplicate them.
        assert result.narrations
        assert len(rows) == len(result.narrations)

    def test_refuted_pair_has_iteration_and_verdict_but_no_copy(self, tmp_path):
        store = ExperimentStore(tmp_path / "w.sqlite")
        scenario = build_scenario(("Altar", "Ghost"))
        # A link that the fake always offers, so policy iterations complete.
        policy = WitnessPolicy(links=[LinkPlan("Altar", "Ghost")], player=0)
        run_id, recorder = start_witness_recording(
            store, scenario=scenario, seeds=[1], candidate_key="refuted"
        )
        result = run_witness(
            FakeRefutedClient(), scenario, policy, seeds=[1],
            max_iterations=3, max_decisions=3, recorder=recorder,
            candidate_key="refuted",
        )
        persist_witness(store, result, run_id=run_id)
        rows = store.witness_events(run_id)
        store.close()

        assert result.verdict == "refuted"
        kinds = [r["kind"] for r in rows]
        assert "iteration" in kinds
        assert "verdict" in kinds
        assert "copy" not in kinds

    def test_non_persisted_run_writes_no_event_rows(self, tmp_path):
        store = ExperimentStore(tmp_path / "w.sqlite")
        store.start_witness_run(seeds=[1], candidate_key="1048622")
        scenario = build_scenario((KIKI, DECEIVER))
        result = run_witness(
            FakeKikiClient(), scenario, _kiki_policy(), seeds=[1],
            max_iterations=2, max_decisions=4,
        )
        # Narration is collected in memory but nothing is written without a
        # recorder (the non --persist path).
        assert result.narrations
        assert store.witness_events() == []
        store.close()

    def test_narration_kind_sequence_is_deterministic(self, tmp_path):
        def kinds_for(name: str) -> list[str]:
            store, run_id, _ = _persisted_run(
                tmp_path / f"{name}.sqlite", FakeKikiClient()
            )
            kinds = [r["kind"] for r in store.witness_events(run_id)]
            store.close()
            return kinds

        assert kinds_for("a") == kinds_for("b")


class TestReadOnlyLive:
    def test_readonly_connection_queries_while_writer_open(self, tmp_path):
        path = tmp_path / "w.sqlite"
        store = ExperimentStore(path)
        run_id = store.start_witness_run(
            seeds=[1], candidate_key="1048622", card_names=(KIKI, DECEIVER)
        )
        store.record_witness_event(run_id, "cast", f"{KIKI} is cast")

        def read() -> list[tuple]:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                conn.execute("PRAGMA query_only=ON")
                return conn.execute(
                    "SELECT kind, text FROM witness_events WHERE run_id = ? ORDER BY seq",
                    (run_id,),
                ).fetchall()
            finally:
                conn.close()

        # The writer is still open (its connection has not been closed) and has
        # committed the first row: a read-only reader sees it mid-run.
        first = read()
        store.record_witness_event(run_id, "verdict", "Verdict: loops")
        second = read()
        store.close()

        assert first == [("cast", f"{KIKI} is cast")]
        assert [kind for kind, _ in second] == ["cast", "verdict"]
