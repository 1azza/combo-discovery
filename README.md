# combo-discovery

MTG combo discovery using the Forge rules engine as a programmable environment.

A Python research codebase that drives a headless [Forge](https://github.com/Card-Forge/forge)
harness over gRPC to search for, validate, and adjudicate Magic: The Gathering
card combos. Part of a research project on engine-grounded combo discovery —
blog + open artifact.

## Architecture

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the design rationale and the
protocol changes required before research-scale search. See
[`STATUS.md`](STATUS.md) for the current implementation state and
[`PROJECT_TODO.md`](PROJECT_TODO.md) for the full project checklist.

- `forge-harness` (Java, separate repo/fork of Forge): embeds `forge-game`, exposes a steppable game environment via gRPC (contract: `proto/forge_env.proto`).
- this repo (Python): environment client, worker pool, search policies, ontology, candidate generation, evaluation.

## Layout

```
proto/forge_env.proto        frozen gRPC contract (shared with Java harness)
src/combo_discovery/
  env.py                     ForgeEnvClient — typed sync gRPC wrapper
  goldfish.py                placeholder deterministic policy (combo validation)
  runner.py                  run_game / run_games / determinism_check
  pool.py                    WorkerPool — round-robin over harness servers
  config.py                  defaults
  generated/                 protobuf stubs (do not edit)
scripts/gen_stubs.sh         regenerate stubs from proto
scripts/run_smoke.py         connectivity check against localhost:50051
```

## Usage

```bash
uv sync                                    # install
uv run python scripts/run_smoke.py         # after starting the Java harness
uv run pytest                              # unit tests (no live server needed)
```

Start worker servers once the harness jar exists:

```python
from combo_discovery.pool import WorkerPool
pool = WorkerPool.spawn_servers("java -jar forge-harness.jar --port {port}", n=8, base_port=50060)
results = pool.map_games((("combo", "combo.dck"), ("goldfish", "goldfish.dck")), seeds=range(20))
```

## License

GPL-3.0 — the Java harness links GPL Forge code.
