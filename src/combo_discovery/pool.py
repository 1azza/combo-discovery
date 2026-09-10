"""Worker pool: round-robin game distribution over harness servers.

Workers are normally pre-running harness servers (constructor path). When the
pool was created via spawn_servers it also owns the server processes and can
respawn a dead one. Recovery behavior:

  - A worker left with an active game (crashed client, leftover game) fails
    StartGame with FAILED_PRECONDITION; the pool retries the job once with
    force_stop_active=True (one forced stop, then retry) and logs it.
  - A worker that repeatedly fails jobs is marked unhealthy and excluded from
    rotation instead of crashing the whole run; a successfully revived worker
    is re-admitted.
  - A failed job's seed is retried on another healthy worker, bounded by
    MAX_TOTAL_ATTEMPTS = 2x the worker count for the whole map call.
"""

from __future__ import annotations

import logging
import subprocess
import time
from collections.abc import Sequence

from .config import DEFAULT_CONFIG, Config
from .env import ForgeEnvClient, GameNotActiveError, HarnessConnectionError
from .runner import GameResult, run_game

logger = logging.getLogger(__name__)

# Consecutive job failures after which a worker is excluded from rotation.
CONSECUTIVE_FAILURES_BEFORE_EXCLUDE = 2
# Whole-map bound: total job attempts <= 2x the worker count.
MAX_TOTAL_ATTEMPTS_FACTOR = 2


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
        self._rr = 0
        for i in range(self.n_workers):
            client = ForgeEnvClient(host=self.host, port=self.base_port + i)
            client.connect()
            self._clients.append(client)
            self._healthy.append(True)
            self._fail_counts.append(0)
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
        procs = []
        for i in range(n):
            port = base_port + i
            procs.append(subprocess.Popen(cmd_template.format(port=port), shell=True))
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
                            p.kill()
                        raise HarnessConnectionError(f"harness on port {port} never came up")
                    time.sleep(0.5)
            client.close()
        pool = cls(n_workers=n, base_port=base_port, host=host)
        pool._procs = procs
        pool._cmd_template = cmd_template
        pool._startup_timeout = startup_timeout
        return pool

    def map_games(
        self,
        deck_pair: tuple[tuple[str, str], tuple[str, str]],
        seeds: Sequence[int],
        policy=None,
        max_turns: int = 0,
        timeout_seconds: int = 0,
    ) -> list[GameResult]:
        decks = list(deck_pair)
        results: dict[int, GameResult] = {}
        pending = list(seeds)
        max_attempts = MAX_TOTAL_ATTEMPTS_FACTOR * self.n_workers
        attempts = 0
        while pending:
            healthy = [i for i in range(self.n_workers) if self._healthy[i]]
            if not healthy:
                raise HarnessConnectionError(
                    f"all {self.n_workers} workers unhealthy; failing seeds: {pending}"
                )
            if attempts >= max_attempts:
                raise HarnessConnectionError(
                    f"jobs still failing after {attempts} attempts (bound "
                    f"{max_attempts} = 2x workers); failing seeds: {pending}"
                )
            seed = pending.pop(0)
            attempts += 1
            result = self._run_seed(healthy, decks, seed, policy, max_turns, timeout_seconds)
            if result is not None:
                results[seed] = result
            else:
                pending.append(seed)  # retried on other workers, bounded above
        return [results[s] for s in seeds]

    def _run_seed(
        self,
        healthy: list[int],
        decks: list[tuple[str, str]],
        seed: int,
        policy,
        max_turns: int,
        timeout_seconds: int,
    ) -> GameResult | None:
        """Try one seed on the healthy workers in round-robin order. Returns
        the GameResult on success, None if every candidate worker failed this
        round (the seed is retried later, bounded by the total-attempt limit)."""
        start = self._rr % len(healthy)
        ordered = healthy[start:] + healthy[:start]
        self._rr = (self._rr + 1) % len(healthy)
        for idx in ordered:
            client = self._clients[idx]
            try:
                result = self._run_job(client, decks, seed, policy, max_turns, timeout_seconds)
            except GameNotActiveError as e:
                # _run_job already retried once with force_stop_active; this
                # worker keeps misbehaving.
                logger.warning(
                    "worker %d (port %d) still failing after forced stop: %s",
                    idx, self.base_port + idx, e,
                )
                self._note_failure(idx)
            except HarnessConnectionError as e:
                logger.warning(
                    "worker %d (port %d) connection failure: %s",
                    idx, self.base_port + idx, e,
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
        """Record a job failure on a worker and attempt recovery. After
        CONSECUTIVE_FAILURES_BEFORE_EXCLUDE consecutive failures the worker is
        excluded from rotation; a successfully revived worker is re-admitted
        (its next job attempt decides whether it stays)."""
        self._fail_counts[idx] += 1
        revived = self._revive(idx)
        if revived:
            if not self._healthy[idx]:
                logger.info("worker %d (port %d) re-admitted after recovery", idx, self.base_port + idx)
            self._healthy[idx] = True
        elif self._fail_counts[idx] >= CONSECUTIVE_FAILURES_BEFORE_EXCLUDE:
            self._healthy[idx] = False
            logger.warning(
                "excluding worker %d (port %d) from rotation after %d consecutive failures",
                idx, self.base_port + idx, self._fail_counts[idx],
            )

    def _revive(self, idx: int) -> bool:
        """Try to bring a worker back: close the broken channel, respawn the
        server process if the pool owns it, reconnect. Returns True if a
        channel to a live harness was established."""
        port = self.base_port + idx
        old = self._clients[idx]
        if old is not None:
            old.close()
        if (
            self._cmd_template is not None
            and self._procs
            and idx < len(self._procs)
            and self._procs[idx].poll() is not None
        ):
            logger.warning("respawning harness process on port %d", port)
            self._procs[idx].kill()
            self._procs[idx] = subprocess.Popen(
                self._cmd_template.format(port=port), shell=True
            )
        try:
            client = ForgeEnvClient(host=self.host, port=port)
            client.connect()
        except HarnessConnectionError:
            logger.warning("worker %d (port %d) did not come back", idx, port)
            return False
        self._clients[idx] = client
        return True

    def close(self) -> None:
        for client in self._clients:
            client.close()
        for proc in self._procs:
            proc.terminate()
