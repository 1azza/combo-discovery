"""Parallel witness batch runner over N harness servers.

Sweeps used to be serial: one game at a time against a single harness.  This
module fans a list of ``(engine, partner)`` card pairs across a small pool of
worker clients, each bound to its own harness port (``base_port + index``), so a
sweep can use the headless servers ``WorkerPool.spawn_servers`` already knows how
to start.

Concurrency
-----------
A ``ThreadPoolExecutor(max_workers=workers)`` runs one pair per task.  Clients
are leased from a ``queue.Queue``: a task takes a client, runs the witness, and
returns it, so **no two threads ever use the same client at the same time**.
The returned list is always in the input pair order regardless of completion
order (results are stored by input index), and each pair's result is independent
of scheduling.

Persistence
-----------
Persistence happens in the worker that owns the run (option (b) of the design
note).  ``ExperimentStore`` already serializes every read/write with a
process-wide lock and opens the connection with ``check_same_thread=False``, so
one shared store across worker threads is safe; the module-level lock is the
"guard" and ``PRAGMA busy_timeout`` (set by the store) covers any external
writer.  Live streaming **is preserved**: the recorder built by
``start_witness_recording`` appends each observation from the worker thread as it
is captured, and the result/evidence row is appended when the run finishes (or
an ``error`` row if it raises).  Persisting in the parent instead would have
been simpler but would have degraded to end-of-run persistence.
"""

from __future__ import annotations

import logging
import queue
import sqlite3
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from . import witness as W
from .env import ForgeEnvClient
from .pool import WorkerPool
from .research_config import load_config
from .store import ExperimentStore
from .witness import run_witness  # module-level seam (tests monkeypatch this)

logger = logging.getLogger(__name__)

#: Default command template for ``spawn=True``.  ``WorkerPool._spawn`` formats
#: only ``{port}``, so the path must not contain any other braces.  Override
#: with ``spawn_cmd`` (or use pre-running harnesses with ``spawn=False``).
DEFAULT_SPAWN_CMD = (
    "cd /home/lza/Work/forge/forge-gui-desktop && "
    "java -Djava.awt.headless=true -jar "
    "../forge-harness/target/forge-harness-2.0.15-SNAPSHOT.jar "
    "--port {port}"
)

#: Progress callback: ``progress(index, record)`` in completion order.
ProgressFn = Callable[[int, dict[str, Any]], None]

#: Optional per-pair candidate override: ``(engine, partner) -> W.Candidate``.
CandidateBuilder = Callable[[str, str], W.Candidate]


def _load_card_meta(db: str | Path | None) -> dict[str, tuple[str, str]]:
    """Map exact card name -> (type_line, lower-cased oracle_text) from ``db``.

    First printing wins (the corpus can carry several).  With ``db=None`` the
    map is empty, so candidates are still runnable but with no type/oracle hints.
    """
    if db is None:
        return {}
    meta: dict[str, tuple[str, str]] = {}
    conn = sqlite3.connect(str(db))
    try:
        for name, type_line, oracle in conn.execute(
            "select name, type_line, oracle_text from cards"
        ):
            key = str(name or "")
            if key and key not in meta:
                meta[key] = (str(type_line or ""), str(oracle or "").lower())
    finally:
        conn.close()
    return meta


def _candidate(engine: str, partner: str, meta: dict[str, tuple[str, str]]) -> W.Candidate:
    engine_type, engine_oracle = meta.get(engine, ("", ""))
    partner_type, partner_oracle = meta.get(partner, ("", ""))
    return W.Candidate(
        cards=(engine, partner),
        type_lines=(engine_type, partner_type),
        oracle_texts=(engine_oracle, partner_oracle),
        kind="pair",
        key=f"{engine}+{partner}",
        pattern="batch",
    )


def _build_clients(n: int, host: str, base_port: int) -> list[ForgeEnvClient]:
    clients: list[ForgeEnvClient] = []
    for index in range(n):
        client = ForgeEnvClient(host=host, port=base_port + index)
        client.connect()
        clients.append(client)
    return clients


def _seed_list(seeds: int | Sequence[int]) -> list[int]:
    return [int(seeds)] if isinstance(seeds, int) else [int(s) for s in seeds]


def _run_one(
    client: Any,
    engine: str,
    partner: str,
    *,
    meta: dict[str, tuple[str, str]],
    make_candidate: CandidateBuilder,
    store: Any,
    decks: Sequence[tuple[str, str]],
    seeds: list[int],
    max_iterations: int,
    max_decisions: int,
    engine_commit: str,
    proto_version: int,
) -> dict[str, Any]:
    """Run one pair on ``client`` and return its record (persisting if asked)."""
    combo = make_candidate(engine, partner)
    scenario = W.build_scenario(combo)
    policy = W.WitnessPolicy(combo)
    run_id: int | None = None
    recorder = None
    persisted: W.WitnessResult | None = None
    try:
        if store is not None:
            run_id, recorder = W.start_witness_recording(
                store,
                scenario=scenario,
                seeds=seeds,
                params={
                    "max_iterations": int(max_iterations),
                    "max_decisions": int(max_decisions),
                },
                engine_commit=engine_commit,
                proto_version=int(proto_version),
            )
        res = run_witness(
            client,
            scenario,
            policy,
            seeds=seeds,
            max_iterations=int(max_iterations),
            max_decisions=int(max_decisions),
            decks=decks,
            candidate_kind=combo.kind,
            candidate_key=combo.key,
            card_names=combo.cards,
            infinite=combo.infinite,
            recorder=recorder,
        )
        persisted = res
        diag = policy.diagnostics()
        evidence = res.evidence or {}
        record: dict[str, Any] = {
            "engine": engine,
            "partner": partner,
            "candidate_kind": combo.kind,
            "candidate_key": combo.key,
            "verdict": res.verdict,
            "iterations": int(res.iterations),
            "executed": int(diag.get("executed_actions", 0)),
            "reason": evidence.get("kind") or evidence.get("reason"),
            "error": res.error,
        }
    except Exception as exc:  # noqa: BLE001 - never lose a started run
        logger.warning("batch run %s + %s failed: %s", engine, partner, exc)
        record = {
            "engine": engine,
            "partner": partner,
            "candidate_kind": combo.kind,
            "candidate_key": combo.key,
            "verdict": "error",
            "iterations": 0,
            "executed": 0,
            "reason": str(exc),
            "error": str(exc),
        }
        if store is not None and run_id is not None:
            W.persist_error_result(
                store,
                run_id,
                scenario=scenario,
                error=exc,
                candidate_kind=combo.kind,
                candidate_key=combo.key,
                card_names=combo.cards,
                seeds=seeds,
            )
            run_id = None
    if store is not None and run_id is not None and persisted is not None:
        W.persist_witness(store, persisted, run_id=run_id)
    return record


def run_batch(
    pairs: Iterable[tuple[str, str]],
    *,
    workers: int = 1,
    host: str = "localhost",
    base_port: int = 50051,
    spawn: bool = False,
    spawn_cmd: str | None = None,
    db: str | Path | None = None,
    persist: bool = False,
    decks: Sequence[tuple[str, str]],
    seeds: int | Sequence[int] = 1,
    max_iterations: int = 6,
    max_decisions: int = 256,
    progress: ProgressFn | None = None,
    candidate_builder: CandidateBuilder | None = None,
) -> list[dict[str, Any]]:
    """Run every ``(engine, partner)`` pair, returning records in input order.

    ``workers`` clients are leased from a queue (never shared concurrently) on
    ports ``base_port + index``.  With ``spawn=True`` the harness servers are
    started first via :meth:`WorkerPool.spawn_servers` and stopped in a
    ``finally``.  With ``persist=True`` each worker streams observations into
    ``witness_observations`` and appends its result/evidence row; a raised run
    still gets an ``error`` result.

    ``candidate_builder`` overrides how a pair becomes a ``W.Candidate`` (the
    default reads ``type_lines``/``oracle_texts`` from ``db``).  Callers use it
    to keep the parallel path semantically identical to their serial path.
    """
    pair_list = [(str(engine), str(partner)) for engine, partner in pairs]
    if not pair_list:
        return []
    width = max(1, min(int(workers), len(pair_list)))
    seed_list = _seed_list(seeds)
    cfg = load_config()
    meta = _load_card_meta(db)

    def default_candidate(engine: str, partner: str) -> W.Candidate:
        return _candidate(engine, partner, meta)

    make_candidate = candidate_builder or default_candidate

    pool: WorkerPool | None = None
    clients: list[ForgeEnvClient] = []
    store: ExperimentStore | None = None
    try:
        if spawn:
            pool = WorkerPool.spawn_servers(
                spawn_cmd or DEFAULT_SPAWN_CMD, width, base_port, host=host
            )
        clients = _build_clients(width, host, base_port)
        if persist:
            if db is None:
                raise ValueError("persist=True requires db=<sqlite path>")
            store = ExperimentStore(Path(db))

        leases: queue.Queue[Any] = queue.Queue()
        for client in clients:
            leases.put(client)

        def task(index: int) -> dict[str, Any]:
            engine, partner = pair_list[index]
            client = leases.get()
            try:
                return _run_one(
                    client,
                    engine,
                    partner,
                    meta=meta,
                    make_candidate=make_candidate,
                    store=store,
                    decks=decks,
                    seeds=seed_list,
                    max_iterations=int(max_iterations),
                    max_decisions=int(max_decisions),
                    engine_commit=cfg.engine_commit,
                    proto_version=cfg.proto_version,
                )
            finally:
                leases.put(client)

        results: list[dict[str, Any] | None] = [None] * len(pair_list)
        with ThreadPoolExecutor(max_workers=width) as executor:
            futures = {
                executor.submit(task, index): index
                for index in range(len(pair_list))
            }
            for future in as_completed(futures):
                index = futures[future]
                record = future.result()
                results[index] = record
                if progress is not None:
                    progress(index, record)
        return [record for record in results if record is not None]
    finally:
        for client in clients:
            try:
                client.close()
            except Exception:  # noqa: BLE001 - best-effort teardown
                logger.debug("batch client close failed", exc_info=True)
        if store is not None:
            store.close()
        if pool is not None:
            pool.close()


__all__ = ["CandidateBuilder", "DEFAULT_SPAWN_CMD", "ProgressFn", "run_batch"]
