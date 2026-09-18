"""Persistence helpers for witness runs.

Split out of ``combo_discovery.witness``; behaviour is unchanged.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any

from ..env import PROTOCOL_VERSION
from .driver import WitnessResult
from .loop import Observation
from .narration import Narration
from .policy import WITNESS_POLICY_VERSION
from .scenario import Scenario

logger = logging.getLogger("combo_discovery.witness")



# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def start_witness_recording(
    store: Any,
    *,
    scenario: Scenario,
    seeds: Sequence[int],
    params: dict[str, Any] | None = None,
    engine_commit: str = "",
    proto_version: int = PROTOCOL_VERSION,
    policy_version: str = WITNESS_POLICY_VERSION,
    notes: str = "",
    candidate_key: str = "",
    card_names: Sequence[str] = (),
) -> tuple[int, WitnessLiveRecorder]:
    """Start a ``witness_runs`` row and return ``(run_id, live_recorder)``.

    Shared by ``combo-witness --persist`` and the batch instruments: the run row
    is opened *before* the run so observations and narration can stream to a UI,
    and the returned recorder is meant to be passed to :func:`run_witness` as
    ``recorder=``.  ``candidate_key``/``card_names`` are recorded on the run row
    up front (schema v8) so an in-progress run is identifiable.  Finish with
    ``persist_witness(store, result, run_id=run_id, ...)``.  This is the single
    place that builds the pre-run row, so every writer records the same fields.
    """
    run_id = int(
        store.start_witness_run(
            engine_commit=engine_commit,
            proto_version=int(proto_version),
            policy_version=policy_version,
            scenario_json=scenario.canonical_json(),
            seeds=list(seeds),
            params=params or {},
            notes=notes,
            candidate_key=candidate_key,
            card_names=list(card_names),
        )
    )
    return run_id, WitnessLiveRecorder(store, run_id)


def observation_recorder(store: Any, run_id: int) -> Callable[[Observation], None]:
    """Build a live ``recorder`` that appends each observation immediately.

    Pass the result to :func:`run_witness` (``recorder=...``) together with a
    ``witness_runs`` row started *before* the run: every captured observation is
    appended to ``witness_observations`` as it happens, which is what lets a UI
    watch a run in progress.  Each written observation is marked so
    :func:`persist_observations` will not append it a second time at end of run.
    """

    def record(observation: Observation) -> None:
        store.record_witness_observation(
            run_id,
            observation.iteration,
            turn=observation.turn,
            phase=observation.phase,
            signature=observation.signature,
            resources=observation.resources,
            event_seq=observation.event_seq,
        )
        try:
            observation._persisted = True
        except Exception:  # pragma: no cover - dataclasses are mutable
            logger.debug("could not mark observation persisted", exc_info=True)

    return record


class WitnessLiveRecorder:
    """Live recorder for observations *and* card-level narration.

    Callable with an :class:`Observation` (the existing ``recorder=`` contract)
    and exposes ``record_event``, which the driver uses to stream narration as
    each game event happens.  Both write immediately, so a UI can poll the DB
    while the run is still in flight.
    """

    def __init__(self, store: Any, run_id: int):
        self._store = store
        self._run_id = int(run_id)
        self._record_observation = observation_recorder(store, self._run_id)

    def __call__(self, observation: Observation) -> None:
        self._record_observation(observation)

    def record_event(self, narration: Narration) -> None:
        self._store.record_witness_event(
            self._run_id,
            narration.kind,
            narration.text,
            turn=narration.turn,
            phase=narration.phase,
            actor=narration.actor,
            target=narration.target,
            detail=narration.detail or None,
        )
        try:
            narration._persisted = True
        except Exception:  # pragma: no cover - dataclasses are mutable
            logger.debug("could not mark narration persisted", exc_info=True)



def persist_observations(store: Any, run_id: int, result: WitnessResult) -> int:
    """Append ``result``'s observations that a live recorder has not written.

    A recorder built by :func:`observation_recorder` marks each observation it
    appends; those are skipped here, so calling this after a live run is a safe
    no-op and calling it on a result captured without a recorder persists the
    whole per-step trace.  Returns the number of rows appended.
    """
    written = 0
    for observation in result.observations:
        if getattr(observation, "_persisted", False):
            continue
        store.record_witness_observation(
            run_id,
            observation.iteration,
            turn=observation.turn,
            phase=observation.phase,
            signature=observation.signature,
            resources=observation.resources,
            event_seq=observation.event_seq,
        )
        try:
            observation._persisted = True
        except Exception:  # pragma: no cover - dataclasses are mutable
            logger.debug("could not mark observation persisted", exc_info=True)
        written += 1
    return written


def persist_narrations(store: Any, run_id: int, result: WitnessResult) -> int:
    """Append ``result``'s narration rows that a live recorder has not written.

    Mirrors :func:`persist_observations`: rows streamed live are marked and
    skipped, so this safely backfills a run captured without a live recorder
    (and is a no-op after one).  Returns the number of rows appended.
    """
    written = 0
    for narration in result.narrations:
        if getattr(narration, "_persisted", False):
            continue
        store.record_witness_event(
            run_id,
            narration.kind,
            narration.text,
            turn=narration.turn,
            phase=narration.phase,
            actor=narration.actor,
            target=narration.target,
            detail=narration.detail or None,
        )
        try:
            narration._persisted = True
        except Exception:  # pragma: no cover - dataclasses are mutable
            logger.debug("could not mark narration persisted", exc_info=True)
        written += 1
    return written


def persist_witness(
    store: Any,
    result: WitnessResult,
    *,
    engine_commit: str = "",
    proto_version: int = PROTOCOL_VERSION,
    policy_version: str = WITNESS_POLICY_VERSION,
    params: dict[str, Any] | None = None,
    notes: str = "",
    run_id: int | None = None,
) -> tuple[int, int]:
    """Append a witness run + result to the store; return ``(run_id, result_id)``.

    ``run_id`` may be passed when the ``witness_runs`` row was already started
    before the run (the live ``combo-witness --persist`` path); otherwise a new
    run row is appended (recording the pair up front).  Per-step observations
    and narration not already written live are appended via
    :func:`persist_observations`/:func:`persist_narrations`, and
    ``result.evidence`` (plus its ``diagnostics`` sub-dict) is stored on the
    result row.
    """
    if run_id is None:
        active_id = int(
            store.start_witness_run(
                engine_commit=engine_commit,
                proto_version=int(proto_version),
                policy_version=policy_version,
                scenario_json=result.scenario.canonical_json(),
                seeds=result.seeds or [result.seed],
                params=params or {},
                notes=notes,
                candidate_key=result.candidate_key,
                card_names=list(result.card_names),
            )
        )
    else:
        active_id = int(run_id)
    persist_observations(store, active_id, result)
    persist_narrations(store, active_id, result)
    diagnostics: Any = None
    if isinstance(result.evidence, dict):
        diagnostics = result.evidence.get("diagnostics")
    result_id = store.record_witness_result(
        active_id,
        candidate_kind=result.candidate_kind,
        candidate_key=result.candidate_key,
        card_names=result.card_names,
        verdict=result.verdict,
        infinite=result.infinite,
        iterations=result.iterations,
        signature=result.signatures,
        resource_deltas=result.resource_deltas,
        state_hash_before=result.state_hash_before,
        state_hash_after=result.state_hash_after,
        event_start_seq=result.event_start_seq,
        event_end_seq=result.event_end_seq,
        trace=result.decision_trace,
        evidence=result.evidence if result.evidence else None,
        diagnostics=diagnostics,
    )
    return active_id, result_id


def persist_error_result(
    store: Any,
    run_id: int,
    *,
    scenario: Scenario,
    error: Any,
    candidate_kind: str = "",
    candidate_key: str = "",
    card_names: Sequence[str] = (),
    seed: int = 0,
    seeds: Sequence[int] | None = None,
) -> int:
    """Persist an ``error`` verdict for a run that raised before returning one.

    Guarantees a ``witness_runs`` row started by :func:`start_witness_recording`
    is never left without a result.  Reuses :func:`persist_witness`, so any
    observations already streamed live are kept and the error reason lands in
    ``evidence_json``.  Returns the result id.
    """
    result = WitnessResult(
        verdict="error",
        scenario=scenario,
        iterations=0,
        seed=int(seed),
        seeds=[int(s) for s in (seeds if seeds is not None else [seed])],
        evidence={"error": f"{type(error).__name__}: {error}"},
        error=str(error),
        candidate_kind=candidate_kind,
        candidate_key=candidate_key,
        card_names=tuple(card_names),
    )
    _, result_id = persist_witness(store, result, run_id=int(run_id))
    return result_id
