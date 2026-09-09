# Architecture

## Purpose

This project uses Card Forge as a headless Magic: The Gathering rules oracle.
Python owns research logic; Java owns Forge integration. The boundary between
them is the protobuf/gRPC contract in `proto/forge_env.proto`.

```text
Python research process
  candidate generation / search / models / experiments
             |
             | protobuf + gRPC
             v
Java forge-harness
  lifecycle / controllers / state / events / snapshots
             |
             v
Card Forge engine
  forge-core / forge-game / forge-ai / forge-gui
```

The Java harness is in the sibling Forge fork:

```text
/home/lza/Work/forge
```

The Python research repository is:

```text
/home/lza/Work/combo-discovery
```

Forge is pinned locally at commit `4f577da7b2a9074f9f66544aaf99405e38cf5ac3`.

## Why This Boundary

- Python is the right environment for ontology extraction, search, learning,
  experiment management, and GLM-5.3-flash integration.
- Java is required to embed Forge and interact with its internal game objects.
- protobuf makes the research code independent of Forge's unstable Java APIs.
- Separate Java worker processes provide fault isolation and allow parallel
  simulation.

Do not replace this with JPype unless profiling proves the gRPC boundary is the
dominant bottleneck. The schema boundary is valuable for reproducibility and
process isolation.

## Current Components

### Python

- `src/combo_discovery/env.py`: synchronous `ForgeEnvClient`
- `src/combo_discovery/runner.py`: game execution and determinism helper
- `src/combo_discovery/goldfish.py`: deterministic placeholder policy
- `src/combo_discovery/pool.py`: worker pool over pre-running harness servers
- `src/combo_discovery/generated/`: generated protobuf stubs
- `tests/`: client, policy, runner, determinism, and pool tests

### Java

- `forge-harness/pom.xml`: Maven module and shaded jar configuration
- `HarnessMain`: headless Forge boot and gRPC server
- `ForgeEnvService`: protobuf service implementation
- `GameRunner`: Forge game lifecycle, timeout, and event queue
- `RemoteController`: remote priority decisions plus inherited Forge AI defaults
- `LobbyPlayerRemote`: Forge lobby/player adapter

## Current Protocol Limitations

Protocol v2 resolved the M1 identity, event-stream, and lifecycle gaps: every
per-game RPC routes by `game_id`, decisions carry a validated monotonic
`decision_id`, events are independently pollable with per-game sequence
numbers, `max_turns` is enforced, and outcomes are classified. What remains:

1. Only priority spell/ability choices are exposed remotely. Targets, modes,
   mulligans, selections, combat, and other Forge callbacks still use defaults.
   (Gate item 4, next round.)
2. `Snapshot` and `Restore` are declared but return `UNIMPLEMENTED`. (Gate
   item 5.)
3. `FullState` is a partial inspection state, not yet a complete search state.
4. Deck paths are resolved by the Java process and currently depend on valid
   filesystem paths.
5. One active game per harness process. The v2 registry routes by `game_id`
   and rejects stale ids from previous games, but concurrent games are not
   hosted yet.

## Required Gate Before Layer 2

Do not begin MCTS, learned search, or large-scale ontology validation until the
following are complete:

1. [x] Add `game_id`, `decision_id`, and action validation. — done (protocol v2)
2. [x] Add sequence-numbered independent event delivery. — done (`PollEvents`, seq from 1)
3. [x] Enforce `max_turns` and make outcome classification reliable. — done
4. [ ] Implement typed decision callbacks beyond priority.
5. [ ] Implement snapshot/restore and state hashing.
6. [x] Add replay tests proving identical `(deck, seed, policy)` trajectories. —
   done and verified live; this gate caught and fixed a real nondeterminism
   (engine match-tally leakage into GameOver events)

## Launching the Harness

Forge resolves development assets relative to `forge-gui-desktop`. Launch from
that directory, not from the repository root:

```bash
cd /home/lza/Work/forge/forge-gui-desktop
java -Djava.awt.headless=true \
  -jar ../forge-harness/target/forge-harness-2.0.15-SNAPSHOT.jar \
  --port 50051
```

The Python smoke check is:

```bash
cd /home/lza/Work/combo-discovery
uv run python scripts/run_smoke.py
```
