# MTG Combo Discovery — Project Tree

Legend:

- `[x]` complete and verified
- `[~]` started, but incomplete or provisional
- `[ ]` not started
- `[!]` prerequisite or risk that should be addressed before continuing

## 0. Project foundation

- `[x]` Define research goal: discover novel MTG combos using Forge as the rules oracle
- `[x]` Choose Vintage/Legacy-like initial card pool
- `[x]` Choose GLM-5.3-flash as the research model
- `[x]` Choose blog + open artifact as the publication target
- `[x]` Pin Forge source commit: `4f577da7b2a9074f9f66544aaf99405e38cf5ac3`
- `[x]` Create separate Python research repository
- `[x]` Add GPL-3 licensing
- `[ ]` Add a top-level research configuration file
- `[ ]` Record exact Forge, Java, Python, protobuf, and model versions in experiment metadata

## 1. Forge harness — Java / Layer 1

### 1.1 Build and boot

- `[x]` Add `forge-harness` Maven module to the Forge fork
- `[x]` Add shaded executable jar packaging
- `[x]` Add gRPC and protobuf code generation
- `[x]` Boot Forge headlessly with `java.awt.headless=true`
- `[x]` Load Forge's card database
- `[x]` Add `Ping`
- `[x]` Add deterministic seed injection
- `[x]` Add per-game timeout watchdog
- `[!]` Make asset paths independent of the process working directory
- `[ ]` Add a stable harness version/build identifier
- `[ ]` Add structured startup and shutdown logging

### 1.2 Game lifecycle

- `[x]` Start a game from `.dck` deck files
- `[x]` Stop an active game
- `[x]` Run the game on a background thread
- `[x]` Report starting life totals
- `[x]` Report game-over state and winner/draw
- `[x]` Enforce `max_turns` from `StartRequest` — enforced deterministically on the game thread at the first turn boundary beyond N (max_turns=3 → turn 4 in every run); the wall-clock monitor remains only for `timeout_seconds`
- `[x]` Add explicit game/session IDs to every request and response
- `[x]` Prevent stale decisions from one game being submitted to another
- `[x]` Add a lifecycle state machine: `CREATED → RUNNING → OVER → CLOSED`
- `[x]` Clear completed runners and release all scheduled timeout resources
- `[ ]` Add cancellation tests for timeout, client disconnect, and server shutdown

### 1.3 Player control

- `[x]` Support Forge AI players
- `[x]` Support a minimal goldfish player
- `[x]` Support a Python-controlled remote player
- `[x]` Surface priority spell/ability choices
- `[x]` Submit a selected option and continue execution
- `[x]` Replace the current implicit global decision routing with `(game_id, decision_id)` routing (registry keyed by server-assigned `game_id`; decisions are per-game monotonic)
- `[x]` Expose all meaningful decision classes (v3 combat/selection/mulligans + v4 spell-casting path; long-tail callbacks deliberately AI-defaulted):
  - `[x]` targets (`chooseTargetsFor` — server-validated candidates via `canTarget`, `TargetSelection` answer, mandatory/optional)
  - `[x]` modes (`chooseModeForAbility` — option list, repeat rules honored)
  - `[x]` X values and announcements (`ANNOUNCE`, min/max validated)
  - `[x]` optional costs (`chooseOptionalCosts` — kicker selection, empty = no kicker)
  - `[~]` trigger ordering — deliberately AI-defaulted for now (hot path, documented in the proto header)
  - `[x]` mulligans and scry decisions (`MULLIGAN_KEEP`, `MULLIGAN_TUCK`, `SCRY_ARRANGE` incl. surveil)
  - `[x]` combat attackers/blockers (`DECLARE_ATTACKERS`, `DECLARE_BLOCKERS`, `ASSIGN_COMBAT_DAMAGE`, `ORDER_BLOCKERS`; live-verified with a remote player winning by combat)
  - `[x]` discard/sacrifice/selection effects (`CHOOSE_CARDS` generic selection with min/max/optional)
- `[x]` Validate that a submitted option belongs to the outstanding decision
- `[ ]` Add explicit `PASS_PRIORITY` options instead of relying on empty lists
- `[ ]` Add a real land-playing/casting goldfish policy
- `[ ]` Add controller conformance tests for every decision callback used by Forge

### 1.4 Event capture

- `[x]` Capture Forge `GameLog` entries
- `[x]` Deliver events after remote decisions
- `[x]` Include event type, card name, and detail text
- `[x]` Add event sequence numbers and game/turn/phase metadata
- `[x]` Decouple event delivery from `SubmitDecision`
- `[x]` Add `PollEvents(cursor)` (independent event stream; per-game monotonic seq from 1)
- `[x]` Deliver events for Forge-AI-vs-Forge-AI and goldfish-only games
- `[x]` Add a stable event taxonomy for research:
  - `[x]` card drawn
  - `[x]` card cast
  - `[x]` ability activated (SpellCast/TriggerOrdered via structured engine events)
  - `[x]` spell/ability resolved
  - `[x]` permanent entered/left battlefield
  - `[~]` trigger created/resolved (TriggerOrdered exists; trigger lifecycle events not separated)
  - `[x]` mana produced/spent
  - `[x]` life/counter changes
  - `[x]` combat decisions and damage
  - `[x]` game outcome
- `[x]` Preserve raw Forge log text alongside normalized events (`detail_raw`)
- `[x]` Add event-stream replay tests (same `(decks, seed)` → byte-identical event streams, verified live)

### 1.5 State and snapshots

- `[x]` Return basic turn, phase, life, hand, and battlefield state
- `[~]` Define protobuf messages for permanents, zones, counters, attachments, and mana
- `[x]` Implement `Snapshot` and `Restore` before MCTS or Go-Explore work (proto v5: decision-parked snapshots, reusable tokens, RNG re-seed, byte-identical post-restore replay verified live)
- `[x]` Make `FullState` semantically complete and versioned — partially: `state_hash` added; zone completeness is the next round
- `[~]` Add graveyard, exile, stack, command zone, library counts, and revealed-card state (graveyard/exile/command/stack done in v6; revealed-card state pending)
- `[x]` Represent mana as typed colored/colorless quantities rather than a string-keyed map (`ManaPool` message; string map deprecated)
- `[x]` Represent permanents using the defined `Permanent` message rather than only `CardRef` (`FullState.battlefield_cards` with typed counters/damage; `Zone.permanents` indices)
- `[ ]` Add hidden-information policy: observer view versus player view
- `[ ]` Add state hashing for cache keys and deterministic replay
- `[ ]` Verify snapshot/restore equivalence with state hashes and event suffixes

## 2. Python environment and execution layer

### 2.1 Client and protocol

- `[x]` Generate Python protobuf/gRPC stubs
- `[x]` Implement synchronous `ForgeEnvClient`
- `[x]` Add connection/error handling
- `[x]` Add context-manager cleanup
- `[ ]` Regenerate stubs as part of CI rather than relying on checked-in manual edits
- `[x]` Add protocol compatibility/version negotiation (`protocol_version` checked on connect)
- `[ ]` Add async client support for parallel search workloads
- `[ ]` Add typed conversion objects instead of exposing protobuf messages throughout research code

### 2.2 Game runners

- `[x]` Run one game
- `[x]` Run multiple seeds
- `[x]` Return `GameResult`
- `[x]` Add basic determinism-check helper
- `[x]` Observer mode: event collection is a first-class stream (`PollEvents` + `drain_events`)
- `[x]` Make runner aware of player types instead of inferring whether it should drive decisions
- `[ ]` Record complete action/event/state trajectories (events are recorded; state snapshots pending gate item 5)
- `[ ]` Add retry and cleanup behavior after server-side game errors
- `[x]` Add outcome classification: win, loss, draw, timeout, engine error, invalid action (`Outcome` enum + winner/reason on `GameOver`)
- `[ ]` Add per-turn and per-decision timing metrics

### 2.3 Worker pool

- `[x]` Connect to multiple pre-running harness servers
- `[x]` Distribute seeds round-robin
- `[x]` Detect/restart a failed worker once
- `[ ]` Spawn and supervise Java workers robustly
- `[ ]` Use process-specific Forge data directories
- `[ ]` Add bounded queues and backpressure
- `[ ]` Add worker health, throughput, and memory metrics
- `[ ]` Add deterministic job manifests so failed jobs can be replayed
- `[ ]` Benchmark games/minute and decision steps/second

## 3. Experiment and data infrastructure

- `[ ]` Add SQLite schema for cards, candidates, games, decisions, events, outcomes, and adjudications
- `[ ]` Make all experiment records append-only and immutable
- `[ ]` Store exact deck lists, seeds, engine commit, protocol version, policy version, and model version
- `[ ]` Store raw protobuf/event trajectories for replay
- `[ ]` Add migration/versioning for the result database
- `[ ]` Add artifact export to JSONL/Parquet
- `[ ]` Add experiment manifests and run IDs
- `[ ]` Add reproducible notebooks/plots
- `[ ]` Add CI for Python tests, protobuf generation, and Java compilation

## 4. Card data and ontology — Layer 2, Tier 0

### 4.1 Card corpus

- `[ ]` Import Forge card scripts from `cardsfolder`
- `[ ]` Import Scryfall bulk card data
- `[ ]` Normalize card names, faces, sets, and aliases
- `[ ]` Filter and version the Vintage/Legacy-like card pool
- `[ ]` Add Commander Spellbook data as a held-out/validation reference
- `[ ]` Include both script-defined and Java-implemented cards in the initial benchmark subset

### 4.2 Forge script extraction

- `[ ]` Parse the Forge card-script DSL
- `[ ]` Capture parse errors and unsupported syntax rather than silently dropping cards
- `[ ]` Build typed effect records from scripts
- `[ ]` Add hand-authored fallback records for Java-only card behavior
- `[ ]` Link extracted effects to actual engine events observed during play
- `[ ]` Measure script coverage and fallback coverage

### 4.3 Predicate ontology

- `[ ]` Define the initial controlled vocabulary of effect predicates
- `[ ]` Define typed parameters: targets, zones, controllers, costs, timing, restrictions
- `[ ]` Store predicates in a queryable SQLite/Parquet representation
- `[ ]` Build producer/consumer interaction edges
- `[ ]` Build trigger/activation loop edges
- `[ ]` Validate ontology recall against known combos
- `[ ]` Version ontology changes
- `[ ]` Add explanations showing why two cards were linked

## 5. Candidate discovery — Layer 2, Tier 1

- `[ ]` Define a formal combo hypothesis schema
- `[ ]` Define what counts as a combo:
  - `[ ]` all required pieces interact
  - `[ ]` counterfactual removal breaks the result
  - `[ ]` result is not caused by unrelated shell cards
  - `[ ]` result is reproducible across seeds
- `[ ]` Generate candidate pairs from ontology edges
- `[ ]` Generate candidate triples and larger combinations
- `[ ]` Build minimal test decks
- `[ ]` Build shell/accelerant templates
- `[ ]` Implement scripted witness policies
- `[ ]` Implement greedy/backtracking witness search
- `[ ]` Implement MCTS after snapshot/restore is complete
- `[ ]` Produce minimal witness action sequences
- `[ ]` Run counterfactual ablations for each card
- `[ ]` Classify verified, refuted, and inconclusive candidates

## 6. Learned search — Layer 2, Tier 2/3

- `[ ]` Encode cards using ontology predicates and game features
- `[ ]` Encode state and legal actions
- `[ ]` Create trajectory training data from verified searches
- `[ ]` Train a candidate ranker
- `[ ]` Train an action-prior/policy model
- `[ ]` Add active learning: rank → validate → retrain
- `[ ]` Compare ontology features against oracle-text features
- `[ ]` Implement novelty archive keyed by interaction signatures
- `[ ]` Add Go-Explore-style state return and exploration
- `[ ]` Investigate self-play only after deterministic snapshots are reliable

## 7. Model integration

- `[ ]` Add GLM-5.3-flash provider configuration
- `[ ]` Add request/response caching
- `[ ]` Add prompt manifests and model metadata
- `[ ]` Baseline: oracle-text combo proposals
- `[ ]` Ablation: ontology-grounded combo proposals
- `[ ]` Candidate refinement from failed witness searches
- `[ ]` LLM adjudication of verified novel candidates
- `[ ]` Track token usage and cost per experiment
- `[ ]` Publish cached prompts/responses where licensing permits

## 8. Evaluation and novelty

- `[ ]` Build random-pair baseline
- `[ ]` Build embedding-similarity baseline
- `[ ]` Build pure LLM baseline
- `[ ]` Build ontology-grounded LLM baseline
- `[ ]` Split known combos into tuning and held-out evaluation sets
- `[ ]` Measure known-combo recall
- `[ ]` Measure precision@k and discovery efficiency
- `[ ]` Compare scripted, greedy, MCTS, and learned search at fixed budgets
- `[ ]` Validate candidates against Forge AI opponents
- `[ ]` Run seed-sensitivity and robustness checks
- `[ ]` Audit suspected Forge engine/script bugs
- `[ ]` Human-review approximately 50 novel candidates
- `[ ]` Check novelty against Spellbook and known combo references
- `[ ]` Produce final list of human-confirmed novel discoveries

## 9. Public artifact and blog

- `[ ]` Document architecture and protocol
- `[ ]` Document how to build and launch the harness
- `[ ]` Provide a small reproducible demo experiment
- `[ ]` Provide replayable seeds and deck files
- `[ ]` Publish benchmark data and result schemas
- `[ ]` Publish limitations and known Forge integration gaps
- `[ ]` Add diagrams for the engine, ontology, search loop, and evaluation
- `[ ]` Write the research blog post
- `[ ]` Tag a reproducible release
