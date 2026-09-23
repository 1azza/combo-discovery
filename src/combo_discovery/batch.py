"""Parallel witness batch runner over N harness servers.

Sweeps used to be serial: one game at a time against a single harness.  This
module fans a list of ``(engine, partner)`` card pairs across a small pool of
worker clients, each bound to its own harness port (``base_port + index``), so a
sweep can use the headless servers ``WorkerPool.spawn_servers`` already knows how
to start.

Concurrency
-----------
A ``ThreadPoolExecutor(max_workers=workers)`` runs one pair per task.  Clients
are leased from a :class:`_LeaseManager`: a task takes a slot, runs the witness,
and releases it, so **no two threads ever use the same client at the same
time**.  The returned list is always in the input pair order regardless of
completion order (results are stored by input index), and each pair's result is
independent of scheduling.

Stale-game recovery
-------------------
A harness can refuse to start a new game because a previous one is still active
or its engine thread did not terminate (FAILED_PRECONDITION /
``GameNotActiveError``).  Left alone, that poisons the port: every later task
leased to it errors, so one stuck game costs dozens of results.  ``run_batch``
therefore mirrors :class:`WorkerPool`'s recovery instead of recording an error:

* attempt 1 runs normally;
* a start refusal is retried once with ``start_game_kwargs={"force_stop_active":
  True}`` (the harness stops the leftover game first) — the common case;
* if that still refuses, the port is explicitly recovered (an owned harness
  process is restarted via :meth:`WorkerPool.restart_worker`; an external one is
  reconnected) and a final attempt is made;
* if the port still refuses, the task is moved to another live port, and the
  bad port is **retired** from the lease pool so it cannot consume the rest of
  the queue.

Retries are bounded (``MAX_TASK_ATTEMPTS`` across ports, ``MAX_ATTEMPTS_PER_PORT``
on one port, ``MAX_PORT_RECOVERY_FAILURES`` before retirement), so a genuinely
dead port fails fast rather than looping forever.  A recovered task is recorded
under the verdict its successful attempt produced — never ``error`` — and the
harness is deterministic, so a retry yields the same verdict a first attempt
would have.  Recovery counts are reported through the optional ``stats``
out-parameter and a warning log, so a sweep that needed many recoveries is not
indistinguishable from a clean one.

Persistence
-----------
Persistence happens in the worker that owns the run.  ``ExperimentStore``
already serializes every read/write with a process-wide lock and opens the
connection with ``check_same_thread=False``, so one shared store across worker
threads is safe.  Live streaming **is preserved**: the recorder built by
``start_witness_recording`` appends each observation from the worker thread as it
is captured, and the result/evidence row is appended when the run finishes (or
an ``error`` row if it raises).  The recording is opened once per task and reused
across recovery attempts, so a recovered task leaves exactly one run row with its
real verdict — never an ``error`` row from the refused first attempt.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, NamedTuple

from . import witness as W
from .env import ForgeEnvClient, ProtocolMismatchError, is_start_refusal
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

# -- recovery policy --------------------------------------------------------

#: Run attempts a task may spend on ONE port: 1 normal, 1 forced stop, then 1
#: after an explicit port recovery.
MAX_ATTEMPTS_PER_PORT = 3
#: Consecutive failed recoveries on one port before it is retired from the
#: lease pool.  A port whose force-stop AND restart/reconnect still refuse a
#: start is not worth leasing again.
MAX_PORT_RECOVERY_FAILURES = 1
#: Hard stop: total run attempts a single task may consume across all ports.
#: Bounds a genuinely dead pool so it cannot loop forever.
MAX_TASK_ATTEMPTS = 8
#: Bounded wait for a recovered external harness to accept a connection (an
#: owned, restarted harness gets the pool's (longer) startup window instead).
RECOVER_WAIT_TIMEOUT = 30.0
#: Pause between reconnect probes while waiting for a recovered harness.
RECOVER_POLL_INTERVAL_S = 0.5


class _NoLiveClients(RuntimeError):
    """Every leased client has been retired; no port can run a task."""


class _LeaseManager:
    """Hand out worker-slot indices, one at a time, and allow retirement.

    A slot is identified by its index (port ``base_port + index``).  A retired
    slot is never handed out again, so a poisoned harness cannot consume the
    rest of the queue; when every slot is retired :meth:`acquire` raises
    :class:`_NoLiveClients`.
    """

    def __init__(self, n: int):
        self._n = int(n)
        self._busy = [False] * self._n
        self._retired = [False] * self._n
        self._cond = threading.Condition()

    def _pick(self, avoid: set[int]) -> int | None:
        for i in range(self._n):
            if not self._retired[i] and not self._busy[i] and i not in avoid:
                return i
        for i in range(self._n):
            if not self._retired[i] and not self._busy[i]:
                return i
        return None

    def acquire(self, avoid: Iterable[int] | None = None) -> int:
        """Block until a live slot is free and return its index.

        ``avoid`` is a hint: a slot outside it is preferred, but one inside it
        is still returned if that is all that is free.
        """
        avoid_set = set(avoid or ())
        with self._cond:
            while True:
                idx = self._pick(avoid_set)
                if idx is not None:
                    self._busy[idx] = True
                    return idx
                if all(self._retired):
                    raise _NoLiveClients(
                        "all worker ports are retired; no harness left to run on"
                    )
                self._cond.wait()

    def release(self, index: int) -> None:
        with self._cond:
            if not self._retired[index]:
                self._busy[index] = False
                self._cond.notify()

    def retire(self, index: int) -> None:
        with self._cond:
            self._retired[index] = True
            self._cond.notify_all()

    def is_retired(self, index: int) -> bool:
        with self._cond:
            return self._retired[index]


class _RecoveryCounters:
    """Thread-safe counters for the recovery summary."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.recovered = 0
        self.recovery_attempts = 0
        self.recovery_failures = 0
        self.unrecovered = 0
        self.retired_ports: list[int] = []

    def bump(self, name: str, delta: int = 1) -> None:
        with self._lock:
            setattr(self, name, getattr(self, name) + delta)

    def retire(self, port: int) -> None:
        with self._lock:
            if port not in self.retired_ports:
                self.retired_ports.append(port)

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "recovered": self.recovered,
                "recovery_attempts": self.recovery_attempts,
                "recovery_failures": self.recovery_failures,
                "unrecovered": self.unrecovered,
                "retired_ports": sorted(self.retired_ports),
            }


class _Attempt(NamedTuple):
    """The outcome of one witness attempt on one client."""

    record: dict[str, Any]
    persisted: W.WitnessResult | None
    error: Exception | None


def _error_record(engine: str, partner: str, combo: W.Candidate, exc: Exception) -> dict[str, Any]:
    return {
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


def _is_refusal_record(record: dict[str, Any]) -> bool:
    """True when ``record`` is an ``error`` caused by a harness start refusal."""
    return record.get("verdict") == "error" and is_start_refusal(
        record.get("error") or record.get("reason")
    )


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


def _run_one_attempt(
    client: Any,
    engine: str,
    partner: str,
    *,
    combo: W.Candidate,
    scenario: Any,
    decks: Sequence[tuple[str, str]],
    seeds: list[int],
    max_iterations: int,
    max_decisions: int,
    force_stop_active: bool,
    recorder: Any,
) -> _Attempt:
    """Run one pair once on ``client`` and return the attempt's outcome.

    Does NOT persist the run's result: the caller owns the recording (opened
    once per task) so a recovered task is persisted exactly once, under the
    verdict of the attempt that actually produced a combo verdict.  ``recorder``
    still streams observations/narration live for every attempt.

    ``force_stop_active`` is passed to ``run_witness`` as
    ``start_game_kwargs={"force_stop_active": True}`` so the harness stops a
    leftover game before starting — a harness recovery, not a change to the
    combo under test.
    """
    try:
        policy = W.WitnessPolicy(combo)
        extra: dict[str, Any] = {}
        if force_stop_active:
            extra["start_game_kwargs"] = {"force_stop_active": True}
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
            **extra,
        )
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
        return _Attempt(record, res, None)
    except Exception as exc:  # noqa: BLE001 - never lose a started run
        logger.warning("batch run %s + %s failed: %s", engine, partner, exc)
        return _Attempt(_error_record(engine, partner, combo, exc), None, exc)


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
    stats: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Run every ``(engine, partner)`` pair, returning records in input order.

    ``workers`` clients are leased from a pool (never shared concurrently) on
    ports ``base_port + index``.  With ``spawn=True`` the harness servers are
    started first via :meth:`WorkerPool.spawn_servers` and stopped in a
    ``finally``.  With ``persist=True`` each worker streams observations into
    ``witness_observations`` and appends its result/evidence row; a raised run
    still gets an ``error`` result.

    A harness *start refusal* (``GameNotActiveError`` / FAILED_PRECONDITION:
    leftover active game or a previous engine thread that did not terminate) is
    recovered and retried instead of being recorded as an ``error``; see the
    module docstring for the policy.  When ``stats`` is given, it is updated
    with ``{"recovered", "recovery_attempts", "recovery_failures",
    "unrecovered", "retired_ports"}`` (also logged at WARNING when non-zero).

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

    counters = _RecoveryCounters()
    leases = _LeaseManager(width)
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

        # Per-port consecutive failed recoveries; a port is retired at
        # MAX_PORT_RECOVERY_FAILURES (guarded because tasks run in parallel).
        port_recovery_failures = [0] * width
        port_lock = threading.Lock()

        def recover(index: int) -> bool:
            """Recover the harness on port ``base_port + index``.

            Closes the batch client, restarts an owned harness process, then
            reconnects with a bounded wait.  Returns True when a live client is
            in place again; False when the harness never came back.  A
            ``ProtocolMismatchError`` (configuration fault) propagates.
            """
            port = base_port + index
            old = clients[index]
            try:
                old.close()
            except Exception:  # noqa: BLE001 - best-effort channel teardown
                logger.debug("closing client %d before recovery failed", index, exc_info=True)
            if pool is not None:
                restart = getattr(pool, "restart_worker", None)
                if callable(restart):
                    try:
                        restart(index)
                    except Exception:  # noqa: BLE001 - reconnect below still tries
                        logger.warning(
                            "restarting harness on port %d failed", port, exc_info=True
                        )
            if pool is not None:
                wait = float(getattr(pool, "_startup_timeout", RECOVER_WAIT_TIMEOUT))
            else:
                wait = RECOVER_WAIT_TIMEOUT
            deadline = time.monotonic() + wait
            while True:
                client = ForgeEnvClient(host=host, port=port)
                try:
                    client.connect()
                except ProtocolMismatchError:
                    client.close()
                    raise
                except Exception:  # noqa: BLE001 - connection death or timeout
                    client.close()
                    if time.monotonic() >= deadline:
                        return False
                    time.sleep(RECOVER_POLL_INTERVAL_S)
                else:
                    clients[index] = client
                    return True

        def task(index: int) -> dict[str, Any]:
            engine, partner = pair_list[index]
            combo = make_candidate(engine, partner)
            scenario = W.build_scenario(combo)
            run_id: int | None = None
            recorder = None
            if store is not None:
                run_id, recorder = W.start_witness_recording(
                    store,
                    scenario=scenario,
                    seeds=seed_list,
                    params={
                        "max_iterations": int(max_iterations),
                        "max_decisions": int(max_decisions),
                    },
                    engine_commit=cfg.engine_commit,
                    proto_version=cfg.proto_version,
                    candidate_key=combo.key,
                    card_names=combo.cards,
                )
            try:
                slot = leases.acquire()
            except _NoLiveClients as exc:
                counters.bump("unrecovered")
                record = _error_record(engine, partner, combo, exc)
                if store is not None and run_id is not None:
                    W.persist_error_result(
                        store, run_id, scenario=scenario, error=exc,
                        candidate_kind=combo.kind, candidate_key=combo.key,
                        card_names=combo.cards, seeds=seed_list,
                    )
                return record
            try:
                attempt = 0
                total = 0
                force = False
                record = _error_record(
                    engine, partner, combo, _NoLiveClients("no attempt ran")
                )
                persisted: W.WitnessResult | None = None
                error: Exception | None = None
                while total < MAX_TASK_ATTEMPTS:
                    total += 1
                    attempt += 1
                    persisted = None
                    error = None
                    record, persisted, error = _run_one_attempt(
                        clients[slot],
                        engine,
                        partner,
                        combo=combo,
                        scenario=scenario,
                        decks=decks,
                        seeds=seed_list,
                        max_iterations=int(max_iterations),
                        max_decisions=int(max_decisions),
                        force_stop_active=force,
                        recorder=recorder,
                    )
                    if not _is_refusal_record(record):
                        if total > 1:
                            counters.bump("recovered")
                        with port_lock:
                            port_recovery_failures[slot] = 0
                        break
                    # Harness refused to start a game on this port.
                    if attempt == 1:
                        # Cheap first recovery: the next attempt force-stops the
                        # leftover game.  Usually enough.
                        force = True
                        continue
                    if attempt < MAX_ATTEMPTS_PER_PORT:
                        # Explicit port recovery (restart/reconnect), then a
                        # final attempt on this port.
                        counters.bump("recovery_attempts")
                        if recover(slot):
                            force = True
                            continue
                        counters.bump("recovery_failures")
                    # The port did not yield a working harness for this task:
                    # count it and retire the port once it keeps failing.
                    with port_lock:
                        port_recovery_failures[slot] += 1
                        exhausted = (
                            port_recovery_failures[slot] >= MAX_PORT_RECOVERY_FAILURES
                        )
                    if exhausted and not leases.is_retired(slot):
                        leases.retire(slot)
                        counters.retire(base_port + slot)
                    # Move the task to a different live port.
                    leases.release(slot)
                    try:
                        slot = leases.acquire(avoid=[slot])
                    except _NoLiveClients:
                        break
                    attempt = 0
                    force = False
                if _is_refusal_record(record):
                    counters.bump("unrecovered")
                if store is not None and run_id is not None:
                    if persisted is not None:
                        W.persist_witness(store, persisted, run_id=run_id)
                    elif error is not None:
                        W.persist_error_result(
                            store, run_id, scenario=scenario, error=error,
                            candidate_kind=combo.kind, candidate_key=combo.key,
                            card_names=combo.cards, seeds=seed_list,
                        )
                return record
            finally:
                leases.release(slot)

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

        summary = counters.as_dict()
        if stats is not None:
            stats.update(summary)
        if summary["recovered"] or summary["recovery_attempts"] or summary["retired_ports"]:
            logger.warning(
                "batch start-refusal recovery: recovered=%d recovery_attempts=%d "
                "recovery_failures=%d retired_ports=%s unrecovered=%d",
                summary["recovered"],
                summary["recovery_attempts"],
                summary["recovery_failures"],
                summary["retired_ports"],
                summary["unrecovered"],
            )
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
