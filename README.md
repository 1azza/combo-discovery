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

## Protocol (v6)

The frozen contract is `proto/forge_env.proto`; regenerate stubs with
`bash scripts/gen_stubs.sh`.

- **Typed decisions.** `GetDecision` returns a `DecisionType` plus a per-type
  payload and `SubmitDecision` echoes a typed answer. All 13 types are covered:
  `PRIORITY`, `MULLIGAN_KEEP`/`MULLIGAN_TUCK`, `DECLARE_ATTACKERS`/
  `DECLARE_BLOCKERS`/`ASSIGN_COMBAT_DAMAGE`/`ORDER_BLOCKERS`,
  `CHOOSE_CARDS`, `ANNOUNCE`, `SCRY_ARRANGE`, `CHOOSE_TARGETS`,
  `CHOOSE_MODE`, `OPTIONAL_COSTS`.
- **Normalized events.** The event stream is the fixed 28-type research
  vocabulary (`runner.EVENT_VOCABULARY` — `LandPlayed`, `CardDrawn`,
  `LifeChanged`, …), with structured `card_id`/`old_value`/`new_value`/`extra`
  fields; raw log text is preserved in `detail_raw`.
- **Snapshot/restore.** `Snapshot` (requires an outstanding decision) returns
  an opaque token + state hash; `Restore` re-seeds the engine RNG and appends a
  deterministic `SnapshotRestored` event, so an identical action suffix replays
  byte-identically.

## Layout

```
proto/forge_env.proto        frozen gRPC contract (shared with Java harness)
src/combo_discovery/
  env.py                     ForgeEnvClient — typed sync gRPC wrapper
  runner.py                  run_game / run_games / determinism_check; default_policy
  goldfish.py                GoldfishPolicy — deterministic goldfish driver for REMOTE games
  pool.py                    WorkerPool — round-robin over harness servers
  store.py                   ExperimentStore — append-only SQLite persistence
  research_config.py         research.toml loader
  config.py                  worker-pool defaults
  tui/                       Textual research console (`combo-tui`)
    app.py                   app shell, tabs, header/statusline, run wiring
    data.py                  read-only store binding, run config, worker probing
    views/                   Corpus · Experiments · Candidates · Card Lab · Activity
    widgets.py               shared presentation widgets
    screens.py               key-map help modal
    app.tcss                 "omarchy" stylesheet
  generated/                 protobuf stubs (do not edit)
research.toml                research metadata + batch defaults
scripts/gen_stubs.sh         regenerate stubs from proto
scripts/run_smoke.py         end-to-end check against a live harness
scripts/check.sh             local run: gen_stubs + pytest + optional Java compile
.github/workflows/ci.yml     CI: Python lane + path-filtered Java harness lane
```

## Usage

```bash
uv sync                                    # install
bash scripts/gen_stubs.sh                  # regenerate protobuf/gRPC stubs

# Boot the Java harness (from the Forge fork). --assets points Forge at its
# data/asset root so deck paths resolve regardless of the working directory.
java -Djava.awt.headless=true -jar forge-harness-*.jar \
  --port 50051 --assets /path/to/forge/forge-gui-desktop

uv run python scripts/run_smoke.py         # end-to-end smoke (needs the harness)
uv run pytest                              # unit tests (no live server needed)
bash scripts/check.sh                      # gen_stubs + pytest + optional Java compile
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

## Research console (TUI)

`combo-tui` is a keyboard-first [Textual](https://textual.textualize.io/) console
for running experiments interactively. It drives the same library as the
snippets above — the append-only store, `WorkerPool.map_games(..., store=,
run_id=)`, and the real policies — from five views:

- **Corpus** — search the card store, inspect type line / mana / oracle text /
  extracted effect count. From a highlighted card: `i` opens its top
  interactions (hypotheses with the mechanism and partner cards, with a jump to
  the partner), `d` adds it to the research scratch deck, and `D` views/clears
  that deck.
- **Experiments** — pick deck pair, policy, seed range, worker count and port;
  launch a batch and watch live progress, worker health and the results table.
  `research_scratch.dck` is offered as a deck and `n` creates an empty one.
- **Candidates** — the live `combo_hypotheses` graph: filter by pattern or card
  name, sort by score, read the mechanism, jump to a card, and cycle its verdict
  with `v` (an append-only adjudication through the store). Each row carries a
  ground-truth badge: `known` (exact Spellbook pair), `contained` (inside a
  larger known combo) or `candidate` — never "novel", which needs a second source.
- **Card Lab** — ground-truth tuning for one card at a time (starts on
  Kiki-Jiki, `/` picks another). Known Spellbook combos on the left (`f` toggles
  exact 2-card only), our proposals on the right with verdict badges, a **Missed**
  list of known combos we fail to propose, the card + aggregate + per-pattern
  metrics, and false-positive / miss diagnostics. `r` re-runs the evaluation and
  persists it through the store; `enter` jumps to a partner card; `d` adds the
  card to the scratch deck; `g` shows the proposal's pattern module.
- **Activity** — a live tail of experiment events plus worker health.

```bash
uv run combo-tui                              # research.db, research.toml, ./decks
uv run python -m combo_discovery.tui          # same thing, module form
uv run combo-tui --db runs.sqlite --config research.toml --decks-dir decks
```

| key | action |
| --- | --- |
| `1` `2` `3` `4` `5` | Corpus / Experiments / Candidates / Card Lab / Activity |
| `[` `]` | previous / next view |
| `tab` `shift+tab` | move focus between panes |
| `j` `k` | move down / up in a list |
| `enter` | open / inspect the highlighted row (or a card link) |
| `/` | focus the search box / pick a card (Card Lab) |
| `i` | interactions for the selected card (Corpus) |
| `d` `D` | add card to the research scratch deck / view-clear it |
| `v` | cycle a hypothesis verdict (Candidates) |
| `f` | known filter: exact 2-card only / all combos (Card Lab) |
| `r` | reload the current view; re-run + persist the evaluation (Card Lab) |
| `g` | show the proposal's pattern module (Card Lab) |
| `n` | create an empty `research_scratch.dck` (Experiments) |
| `ctrl+r` `ctrl+k` `ctrl+t` | start run / cancel run / re-check harness |
| `?` `f1` | key map |
| `ctrl+p` | command palette |
| `q` | quit (press twice while a run is active) |

**The card loop.** Corpus → interactions (`i`) → scratch deck (`d`) →
Experiments offers `research_scratch` as a deck. Card and partner links jump
back into Corpus. Hypothesis verdicts are the only database writes from the
console: `v` appends a row to `adjudications` through the store's write lock.
Until `combo_hypotheses` is mirrored into `candidates`, that row's
`candidate_id` holds the hypothesis id (documented on
`ExperimentStore.record_adjudication`); status reads prefer the latest
adjudication over `combo_hypotheses.status`, which stays append-only.

**Empty and absent data are first-class states.** The SQLite store is created on
demand, so the console starts cleanly against a fresh database: Corpus and
Candidates show quiet empty states, and the Experiments form still configures.
Every read goes through a separate read-only (`PRAGMA query_only`) connection,
so polling the UI can never interfere with the worker thread recording games.

**No live harness?** The Experiments view probes the configured worker ports with
a short connect and, when none answer, shows the exact `java -jar
forge-harness-*.jar --port …` command to start one. A run is only launched over
the longest contiguous run of reachable ports, so absent ports never stall the
UI. Corpus, recorded experiments and candidates remain fully browsable offline.

Long-running work (probing and the game batch) runs in Textual worker threads,
never on the UI thread; progress is polled from the store and the pool's health
flags. The visual style is a single restrained omarchy blue on a deep warm
near-black ground, with no motion.

## License

GPL-3.0 — the Java harness links GPL Forge code.
