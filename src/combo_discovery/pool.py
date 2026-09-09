"""Worker pool: round-robin game distribution over pre-running harness servers."""

from __future__ import annotations

import subprocess
import time
from collections.abc import Sequence

from .config import DEFAULT_CONFIG, Config
from .env import ForgeEnvClient, HarnessConnectionError
from .runner import GameResult, run_game


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
        for i in range(self.n_workers):
            client = ForgeEnvClient(host=self.host, port=self.base_port + i)
            client.connect()
            self._clients.append(client)

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
        failures: dict[int, int] = {}
        max_attempts = len(self._clients) + 1
        rr = 0
        while pending:
            seed = pending.pop(0)
            progressed = False
            for offset in range(len(self._clients)):
                client = self._clients[(rr + offset) % len(self._clients)]
                try:
                    results[seed] = run_game(
                        client,
                        decks,
                        seed,
                        policy,
                        max_turns=max_turns,
                        timeout_seconds=timeout_seconds,
                    )
                    progressed = True
                    rr = (rr + offset + 1) % len(self._clients)
                    break
                except HarnessConnectionError:
                    self._restart(client)
            if not progressed:
                failures[seed] = failures.get(seed, 0) + 1
                if failures[seed] >= max_attempts:
                    raise HarnessConnectionError(f"seed {seed} failed on all workers")
                pending.append(seed)
        return [results[s] for s in seeds]

    def _restart(self, dead: ForgeEnvClient) -> None:
        idx = self._clients.index(dead)
        port = self.base_port + idx
        dead.close()
        client = ForgeEnvClient(host=self.host, port=port)
        client.connect()
        self._clients[idx] = client

    def close(self) -> None:
        for client in self._clients:
            client.close()
        for proc in self._procs:
            proc.terminate()
