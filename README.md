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
  runner.py                  run_game / run_games / determinism_check
  pool.py                    WorkerPool — round-robin over harness servers
  store.py                   ExperimentStore — append-only SQLite persistence
  research_config.py         research.toml loader
  config.py                  worker-pool defaults
  generated/                 protobuf stubs (do not edit)
research.toml                research metadata + batch defaults
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

## Recording experiments

Trajectories are persisted to an append-only SQLite database (see
`PLUMBING_SPEC.md` section 4 for the schema and `research.toml` for the
recorded engine/model metadata):

```python
from combo_discovery.research_config import load_config
from combo_discovery.store import ExperimentStore
from combo_discovery.runner import run_games

store = ExperimentStore("experiments.sqlite")
run_id = store.start_experiment(**load_config().experiment_meta(), config={"max_turns": 20})

# run_game/run_games and pool.map_games all accept store=/run_id=
run_games(client, decks=[("combo", "combo.dck"), ("goldfish", "goldfish.dck")],
          seeds=[1, 2, 3], store=store, run_id=run_id)

store.export_jsonl("events", "events.jsonl")
store.export_jsonl("games", "games.jsonl")
store.close()
```

`store=` requires a `run_id` from `start_experiment`; each completed game
records a `games` row, every drained event, and the accepted decisions from the
trajectory. The store serializes writes with a process-wide lock, so the worker
pool can record from multiple threads into one database.

## License

GPL-3.0 — the Java harness links GPL Forge code.
