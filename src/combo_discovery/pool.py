"""Worker pool: round-robin game distribution over harness servers.

Workers are normally pre-running harness servers (constructor path). When the
pool was created via spawn_servers it also owns the server processes and can
respawn a dead or wedged one.

Scheduling semantics (P1):
  - Successful seeds never consume a failure budget.
  - A seed is tried across the current healthy workers in rotation. If every
    healthy worker fails it within one round, the seed is requeued for another
    round. It fails permanently only after MAX_SEED_ROUNDS full rounds on the
    healthy set (see ``map_games``).
  - A generous global guard over genuinely-consumed worker attempts
    (``_run_job`` invocations) exists only to stop a runaway infinite loop; it
    is not the primary mechanism and never trips a healthy run.

Recovery semantics (P2 + Q1-Q3):
  - A worker left with an active game (crashed client, leftover game) fails
    StartGame with FAILED_PRECONDITION; ``_run_job`` retries once with
    force_stop_active=True (one forced stop) and logs it.
  - Any ForgeEnvError a job can surface (connection death, timeout, stale
    decision, wrong lifecycle state) is treated as a job failure on that
    worker: the worker takes a health note and the seed moves on to the next
    healthy worker. ``InvalidRequestError`` (a malformed/buggy request) is NOT
    in JOB_FAILURES and propagates — a policy bug must fail loudly.
  - After CONSECUTIVE_FAILURES_BEFORE_EXCLUDE consecutive failures a worker is
    excluded from rotation regardless of whether an immediate revive ping
    succeeded (a pingable-but-broken worker must still be excludable).
  - Re-admission policy (Q2): an excluded worker is probed with a cheap bounded
    connect() and may be re-admitted AT MOST ONCE per map_games call. If it
    fails a job again after that re-admission it is excluded for the rest of
    the call (``_probe_readmit_used``/``_readmitted``); a pingable-but-broken
    worker therefore cannot cycle in and out of rotation.
  - Probe cost (Q3): at most ONE excluded worker is probed per scheduling
    iteration, rotating through them, with a small timeout
    (PROBE_TIMEOUT). When every worker is excluded, map_games performs a
    bounded readiness wait (up to _startup_timeout) probing excluded workers
    in rotation before raising "all workers unhealthy" — a just-respawned
    harness needs time to boot.
  - ``_probe``/``_revive`` treat any ForgeEnvError (connection death OR
    timeout) as "did not come back"; ProtocolMismatchError is a configuration
    failure and deliberately propagates, as do unexpected non-ForgeEnv
    exceptions.
  - A worker whose harness process is alive but wedged is killed and respawned
    after WEDGED_REVIVE_FAILURES consecutive failed revives. Spawned harnesses
    run in their own session/process group and are killed as a group.
  - The immediate revive after a job failure is SHORT (PROBE_TIMEOUT, one
    connect) so a dead external worker cannot stall scheduling for
    _startup_timeout; only the all-excluded readiness wait is bounded by
    _startup_timeout (a just-respawned harness needs boot time).
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from collections import deque
from collections.abc import Sequence

from .config import DEFAULT_CONFIG, Config
from .env import (
    ForgeEnvClient,
    ForgeEnvError,
    GameNotActiveError,
    HarnessConnectionError,
    HarnessTimeoutError,
    ProtocolMismatchError,
    StaleDecisionError,
)
from .runner import GameResult, run_game

logger = logging.getLogger(__name__)

# Consecutive job failures after which a worker is excluded from rotation.
CONSECUTIVE_FAILURES_BEFORE_EXCLUDE = 2
# A seed fails permanently after this many full rounds on the healthy set.
MAX_SEED_ROUNDS = 2
# Global anti-infinite-loop guard: max _run_job invocations, sized as
# GLOBAL_ATTEMPT_BOUND_FACTOR * n_workers per seed with a floor. This is a
# backstop, not the primary per-seed mechanism: a healthy run consumes at most
# one attempt per seed and can never hit it.
GLOBAL_ATTEMPT_BOUND_FACTOR = 2
MIN_GLOBAL_ATTEMPTS = 10
# Consecutive failed revives after which an alive-but-wedged owned process is
# killed and respawned (poll() is not the only wedge mode).
WEDGED_REVIVE_FAILURES = 2
# Cheap liveness probe timeout for re-admitting excluded workers (Q3: small).
PROBE_TIMEOUT = 2.0
# Pause between readiness probes while every worker is excluded (Q3).
READINESS_PROBE_INTERVAL_S = 0.5

# Errors that a single job can surface and that should be treated as a worker
# job failure (health note + try the next worker), rather than propagating out
# of map_games. InvalidRequestError (malformed/buggy request) and
# ProtocolMismatchError (configuration fault) deliberately propagate.
JOB_FAILURES = (
    HarnessConnectionError,
    HarnessTimeoutError,
    StaleDecisionError,
    GameNotActiveError,
)


class WorkerPool:
    def __init__(
        self,
        n_workers: int | None = None,
        base_port: int | None = None,
        host: str = "localhost",
        config: Config = DEFAULT_CONFIG,
    ):
        self.n_workers = n_workers if n_workers is not None else config.n_workers
        self.base_port = base_port if base_port is not None else config.base_port
        self.host = host
        self._clients: list[ForgeEnvClient] = []
        self._procs: list[subprocess.Popen] = []
        self._healthy: list[bool] = []
        self._fail_counts: list[int] = []
        self._revive_fail_counts: list[int] = []
        # Q2/Q3 re-admission state (reset at the start of each map_games call).
        self._probe_readmit_used: list[bool] = []
        self._readmitted: list[bool] = []
        self._probe_cursor = 0
        self._attempts_consumed = 0
        self._rr = 0
        for i in range(self.n_workers):
            client = ForgeEnvClient(host=self.host, port=self.base_port + i)
            client.connect()
            self._clients.append(client)
            self._healthy.append(True)
            self._fail_counts.append(0)
            self._revive_fail_counts.append(0)
            self._probe_readmit_used.append(False)
            self._readmitted.append(False)
        # Set by spawn_servers; enables real process respawn on recovery.
        self._cmd_template: str | None = None
        self._startup_timeout: float = 120.0

    @classmethod
    def spawn_servers(
        cls,
        cmd_template: str,
        n: int,
        base_port: int,
        host: str = "localhost",
        startup_timeout: float = 120.0,
    ) -> "WorkerPool":
        procs = [cls._spawn(cmd_template, base_port + i) for i in range(n)]
        deadline = time.monotonic() + startup_timeout
        for i in range(n):
            port = base_port + i
            client = ForgeEnvClient(host=host, port=port)
            while True:
                try:
                    client.connect()
                    break
                except HarnessConnectionError:
                    if time.monotonic() > deadline:
                        for p in procs:
                            cls._kill_proc(p)
                        raise HarnessConnectionError(f"harness on port {port} never came up")
                    time.sleep(0.5)
            client.close()
        pool = cls(n_workers=n, base_port=base_port, host=host)
        pool._procs = procs
        pool._cmd_template = cmd_template
        pool._startup_timeout = startup_timeout
        return pool

    @staticmethod
    def _spawn(cmd_template: str, port: int) -> subprocess.Popen:
        return subprocess.Popen(
            cmd_template.format(port=port),
            shell=True,
            # Own session/process group: with shell=True the harness is a
            # grandchild, so killing only the shell would orphan it.
            start_new_session=True,
        )

    @staticmethod
    def _kill_proc(proc: subprocess.Popen, sig: int = signal.SIGKILL) -> None:
        """Kill a spawned harness and its whole process group."""
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except Exception:  # best effort
                pass

    def map_games(
        self,
        deck_pair: tuple[tuple[str, str], tuple[str, str]],
        seeds: Sequence[int],
        policy=None,
        max_turns: int = 0,
        timeout_seconds: int = 0,
    ) -> list[GameResult]:
        decks = list(deck_pair)
        seeds = list(seeds)
        if not seeds:
            return []
        results: dict[int, GameResult] = {}
        pending: deque[int] = deque(seeds)
        # Per-seed accounting: how many full rounds of the healthy set have
        # failed this seed, and which workers it has already been tried on in
        # the current round.
        seed_rounds: dict[int, int] = {s: 0 for s in seeds}
        seed_tried: dict[int, set[int]] = {s: set() for s in seeds}
        self._attempts_consumed = 0
        # Q2: each worker gets at most one probe re-admission per call.
        self._probe_readmit_used = [False] * self.n_workers
        self._readmitted = [False] * self.n_workers
        self._probe_cursor = 0
        attempt_guard = max(
            len(seeds) * self.n_workers * GLOBAL_ATTEMPT_BOUND_FACTOR,
            MIN_GLOBAL_ATTEMPTS,
        )
        while pending:
            # Q3: at most one excluded worker is probed per iteration.
            self._reprobe_excluded()
            healthy = [i for i in range(self.n_workers) if self._healthy[i]]
            if not healthy:
                # Every worker is excluded: wait (bounded) for one to become
                # ready — a just-respawned harness needs time to boot — then
                # re-read the healthy set.
                self._wait_for_any_worker(pending)
                healthy = [i for i in range(self.n_workers) if self._healthy[i]]
            seed = pending.popleft()
            candidates = [i for i in healthy if i not in seed_tried[seed]]
            if not candidates:
                # Every currently healthy worker failed this seed in this
                # round. Count the round; only after MAX_SEED_ROUNDS full
                # rounds is the seed a permanent failure.
                seed_rounds[seed] += 1
                if seed_rounds[seed] >= MAX_SEED_ROUNDS:
                    raise HarnessConnectionError(
                        f"seed {seed} failed on every healthy worker in "
                        f"{seed_rounds[seed]} full rounds (workers={healthy}); "
                        f"failing seeds: {list(pending)}"
                    )
                seed_tried[seed] = set()
                pending.append(seed)
                continue
            if self._attempts_consumed >= attempt_guard:
                raise HarnessConnectionError(
                    f"global attempt guard exceeded ({self._attempts_consumed}/"
                    f"{attempt_guard} worker attempts); failing seeds: {list(pending)}"
                )
            result = self._run_seed(
                candidates, decks, seed, policy, max_turns, timeout_seconds
            )
            seed_tried[seed].update(candidates)
            if result is not None:
                results[seed] = result
            else:
                pending.append(seed)
        return [results[s] for s in seeds]

    def _run_seed(
        self,
        workers: list[int],
        decks: list[tuple[str, str]],
        seed: int,
        policy,
        max_turns: int,
        timeout_seconds: int,
    ) -> GameResult | None:
        """Try one seed on the given workers in rotation. Returns the
        GameResult on success, or None if every worker failed this round."""
        start = self._rr % len(workers)
        ordered = workers[start:] + workers[:start]
        self._rr = (self._rr + 1) % len(workers)
        for idx in ordered:
            try:
                result = self._run_job(
                    self._clients[idx], decks, seed, policy, max_turns, timeout_seconds
                )
            except JOB_FAILURES as e:
                logger.warning(
                    "worker %d (port %d) failed seed %d: %s: %s",
                    idx,
                    self.base_port + idx,
                    seed,
                    type(e).__name__,
                    e,
                )
                self._note_failure(idx)
            else:
                self._fail_counts[idx] = 0
                return result
        return None

    def _run_job(
        self,
        client: ForgeEnvClient,
        decks: list[tuple[str, str]],
        seed: int,
        policy,
        max_turns: int,
        timeout_seconds: int,
    ) -> GameResult:
        # One consumed worker attempt. Counted by the global anti-loop guard.
        self._attempts_consumed += 1
        try:
            return run_game(
                client, decks, seed, policy,
                max_turns=max_turns, timeout_seconds=timeout_seconds,
            )
        except GameNotActiveError:
            # A worker left with an active game (crashed client, leftover
            # game) rejects StartGame with FAILED_PRECONDITION. One forced
            # stop, then retry once.
            logger.warning(
                "worker at %s has a leftover active game; retrying seed %d "
                "with force_stop_active=True",
                getattr(client, "_target", "?"), seed,
            )
            return run_game(
                client, decks, seed, policy,
                max_turns=max_turns, timeout_seconds=timeout_seconds,
                force_stop_active=True,
            )

    def _note_failure(self, idx: int) -> None:
        """Record a job failure on a worker and attempt recovery. Exclusion
        happens after CONSECUTIVE_FAILURES_BEFORE_EXCLUDE consecutive failures
        regardless of whether the immediate revive ping succeeded (a
        pingable-but-broken worker must still be excludable). Q2: a worker that
        was already re-admitted once this call and fails again is excluded
        immediately (no second re-admission)."""
        self._fail_counts[idx] += 1
        self._revive(idx)  # best-effort immediate recovery
        if (
            self._readmitted[idx]
            or self._fail_counts[idx] >= CONSECUTIVE_FAILURES_BEFORE_EXCLUDE
        ):
            if self._healthy[idx]:
                logger.warning(
                    "excluding worker %d (port %d) from rotation after %d "
                    "failure(s)%s",
                    idx,
                    self.base_port + idx,
                    self._fail_counts[idx],
                    " (failed after its one probe re-admission)" if self._readmitted[idx] else "",
                )
            self._healthy[idx] = False

    def _next_excluded_to_probe(self) -> int | None:
        """Rotate through excluded workers so each scheduling iteration probes
        at most one (Q3)."""
        excluded = [i for i in range(self.n_workers) if not self._healthy[i]]
        if not excluded:
            return None
        idx = excluded[self._probe_cursor % len(excluded)]
        self._probe_cursor += 1
        return idx

    def _try_readmit(self, idx: int) -> bool:
        """Re-admit an excluded worker at most ONCE per map_games call (Q2).
        A successful probe on an already-spent worker leaves it excluded."""
        if self._probe_readmit_used[idx]:
            logger.info(
                "worker %d (port %d) probes OK but its one re-admission for "
                "this run is spent; staying excluded",
                idx,
                self.base_port + idx,
            )
            return False
        self._probe_readmit_used[idx] = True
        self._readmitted[idx] = True
        self._healthy[idx] = True
        self._fail_counts[idx] = 0
        logger.info(
            "worker %d (port %d) re-admitted after probe (one re-admission "
            "per run)",
            idx,
            self.base_port + idx,
        )
        return True

    def _reprobe_excluded(self) -> None:
        """Probe at most one excluded worker per iteration (Q3), re-admitting
        it only if its one-per-call re-admission is available (Q2)."""
        idx = self._next_excluded_to_probe()
        if idx is None:
            return
        if self._probe(idx):
            self._try_readmit(idx)

    def _wait_for_any_worker(self, pending) -> None:
        """All workers are excluded: wait up to _startup_timeout for one to
        become ready, probing excluded workers in rotation (a just-respawned
        harness needs time to boot, Q3). Raises if none recovers in time."""
        deadline = time.monotonic() + self._startup_timeout
        logger.warning(
            "all %d workers excluded; waiting up to %.0fs for one to recover",
            self.n_workers,
            self._startup_timeout,
        )
        while True:
            idx = self._next_excluded_to_probe()
            if idx is not None and self._probe(idx) and self._try_readmit(idx):
                return
            if time.monotonic() >= deadline:
                raise HarnessConnectionError(
                    f"all {self.n_workers} workers unhealthy after a "
                    f"{self._startup_timeout:.0f}s readiness wait; "
                    f"failing seeds: {list(pending)}"
                )
            time.sleep(READINESS_PROBE_INTERVAL_S)

    def _probe(self, idx: int) -> bool:
        port = self.base_port + idx
        try:
            client = ForgeEnvClient(host=self.host, port=port, timeout=PROBE_TIMEOUT)
            client.connect()
        except ProtocolMismatchError:
            # Q1/M3: an incompatible jar is a configuration failure, not a
            # health state — never mask it as "did not come back".
            raise
        except ForgeEnvError:
            # Q1: connection death OR timeout both mean "did not come back".
            # Unexpected non-ForgeEnv exceptions deliberately propagate.
            return False
        old = self._clients[idx]
        if old is not None and old is not client:
            old.close()
        self._clients[idx] = client
        return True

    def _owns_process(self, idx: int) -> bool:
        return self._cmd_template is not None and idx < len(self._procs)

    def _revive(self, idx: int) -> bool:
        """Immediate best-effort revive after a job failure: close the broken
        channel, respawn a dead owned process, and connect ONCE with the short
        PROBE_TIMEOUT so a dead external worker cannot stall scheduling for
        _startup_timeout (M2). The _startup_timeout-bounded wait lives only in
        _wait_for_any_worker. If the owned process is alive but revive keeps
        failing, treat it as wedged: kill and respawn (its replacement may not
        be listening yet, which is fine — the probe rotation/readiness wait
        covers the boot window). ProtocolMismatchError propagates. Returns True
        if a live harness was reached."""
        port = self.base_port + idx
        cmd_template = self._cmd_template
        old = self._clients[idx]
        if old is not None:
            old.close()
        if self._owns_process(idx) and self._procs[idx].poll() is not None:
            logger.warning("harness process on port %d is dead; respawning", port)
            assert cmd_template is not None  # implied by _owns_process
            self._procs[idx] = self._spawn(cmd_template, port)
            self._revive_fail_counts[idx] = 0
        client = ForgeEnvClient(host=self.host, port=port, timeout=PROBE_TIMEOUT)
        # Adopt the short-timeout client immediately, even if the connect below
        # fails: while a worker is unhealthy, subsequent job attempts must fail
        # fast on PROBE_TIMEOUT instead of stalling on the default 30s RPC
        # deadline (M2).
        self._clients[idx] = client
        try:
            client.connect()
        except ProtocolMismatchError:
            raise
        except ForgeEnvError:
            # Q1: connection death OR timeout both mean "did not come back".
            # Unexpected non-ForgeEnv exceptions deliberately propagate.
            self._revive_fail_counts[idx] += 1
            logger.warning(
                "worker %d (port %d) did not come back (revive failure %d)",
                idx, port, self._revive_fail_counts[idx],
            )
            # poll() is not the only wedge mode: an alive process can still be
            # unreachable. After repeated failed revives, replace it.
            if (
                self._owns_process(idx)
                and self._revive_fail_counts[idx] >= WEDGED_REVIVE_FAILURES
                and self._procs[idx].poll() is None
            ):
                logger.warning(
                    "harness process on port %d is alive but wedged; killing + "
                    "respawning", port,
                )
                assert cmd_template is not None  # implied by _owns_process
                self._kill_proc(self._procs[idx])
                self._procs[idx] = self._spawn(cmd_template, port)
                self._revive_fail_counts[idx] = 0
            return False
        self._revive_fail_counts[idx] = 0
        return True

    def close(self) -> None:
        for client in self._clients:
            client.close()
        for proc in self._procs:
            self._kill_proc(proc, signal.SIGTERM)
