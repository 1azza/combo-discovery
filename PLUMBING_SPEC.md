# Plumbing Completion Spec

Implementation-ready spec for the remaining plumbing before Layer 2 (the
science). Grounded in the verified v3 harness state and the Forge engine
sources at commit `4f577da7`. Each section is implementation-ready: decisions
are made, interfaces are written out, open questions are marked TBD.

## 1. Proto v4 — spell targeting, mode selection, optional costs

**Decision: new decision types, not an inline sub-flow of PRIORITY.**

Rationale:
- The park/answer pattern generalizes mechanically: each engine callback
  (`chooseTargetsFor`, `chooseModeForAbility`, `chooseOptionalCosts`) parks a
  `PendingDecision` on the game thread and blocks; the gRPC thread validates
  and completes. This is exactly the existing REMOTE pattern — no new
  concurrency machinery.
- Each engine re-prompt (invalid targets cause `chooseTargetsFor` to be
  re-entered) gets a fresh `decision_id` via the existing
  `registerDecision` path — stale submissions are rejected by existing rules.
- Research ergonomics: a combo is a *spell-with-targets* pattern; a policy
  needs targeting as a first-class decision with legal candidates, not as an
  opaque sub-step of a priority option. Server-side validation against
  `TargetRestrictions` keeps illegal answers out of trajectories.

**Engine grounding (verified):**
- `SpellAbility.setupTargets()` (SpellAbility.java ~2156) calls
  `chooseTargetsFor` during casting; the SA's `TargetRestrictions` carries
  min/max (`sa.getMinTargets()` ~1903, `sa.getMaxTargets()` ~1906) and valid
  target classes. Legal candidates can be enumerated with
  `sa.canTargetSpellAbility(...)` / per-entity `canTarget` checks; the
  SA mutates its own `Targets` as the answer.
- `chooseModeForAbility(SA, List<AbilitySub> possible, int min, int num,
  boolean allowRepeat)` (PlayerController.java ~281) — answer is a sized
  subset of the mode list.
- `chooseOptionalCosts(SA, List<OptionalCostValue>)` (~333) — answer is the
  chosen subset (kicker/multikicker).
- `chooseNewTargetsFor(SA, filter, optional)` (~118, Spellskite) — v1: leave
  AI-defaulted (rare), noted as a follow-up.

**Proto v4 additions (paste-ready):**

```proto
// --- new DecisionType values ---
// Choose targets for a spell/ability being cast or resolving;
// candidates = legal target entities; answer = target_selection.
DECISION_TYPE_CHOOSE_TARGETS = 11;
// Choose modes for a spell/ability; answer = mode_selection (indices into
// mode_options), size within [min_choices, max_choices]; repeat allowed iff
// allow_repeat.
DECISION_TYPE_CHOOSE_MODE = 12;
// Choose optional costs (kicker etc.); answer = mode_selection (indices into
// cost_options), size within [min_choices, max_choices].
DECISION_TYPE_OPTIONAL_COSTS = 13;

// --- new DecisionRequest payload fields ---
string spell_description = 20;      // host card + ability text summary
bool allow_repeat = 21;             // CHOOSE_MODE: repeats allowed
repeated ModeOption mode_options = 22;  // CHOOSE_MODE / OPTIONAL_COSTS
bool mandatory = 23;                // targets must reach min_choices

message ModeOption {
  int32 id = 1;          // stable within this DecisionRequest
  string description = 2; // engine mode/cost text
}

// Typed target selection: cards and/or players, order = assignment order.
message TargetSelection {
  repeated int32 card_ids = 1;
  repeated int32 player_slots = 2;   // for player-targeting effects
}
```

`DecisionSubmit.answer` gains one arm:
```proto
TargetSelection targets = 11;   // CHOOSE_TARGETS
```
`mode_selection` reuses the existing `IntList card_ids` arm (interpreted as
mode/cost option ids) — no new arm needed; document per-type semantics in the
proto header.

**Validation (server, in `submitDecision` under the existing lease):**
- CHOOSE_TARGETS: count within `[min_choices, max_choices]` (mandatory =
  min>0); each card id must be a candidate; each player slot must be in
  `defender_players`-equivalent candidate players; duplicates only if the
  restriction allows multiple targets on one entity (reject by default).
  On success, apply into the SA's targets in given order.
- CHOOSE_MODE: subset of mode option ids, size in range, repeats only if
  `allow_repeat`.
- OPTIONAL_COSTS: subset of cost option ids.
- All violations → `INVALID_ARGUMENT`, decision stays outstanding (existing
  semantics).

**RemoteController changes:** override `chooseTargetsFor(SpellAbility)` —
enumerate legal targets (engine legality per entity), build candidates +
restrictions, park, translate `TargetSelection` into
`sa.getTargets().add(...)` per engine expectations; on ABORTED → deterministic
NO-OP (empty targets, same as current abort semantics); on timeout → AI
fallback (`super.chooseTargetsFor`). Same shape for `chooseModeForAbility`
and `chooseOptionalCosts`. Each re-entry (engine retry) creates a fresh
decision_id (existing invalidation-on-re-entry covers this).

**Client changes:** `default_policy` handles the three new types
(CHOOSE_TARGETS → min_choices first legal candidates (mandatory) or empty if
optional; CHOOSE_MODE → first `min_choices` modes; OPTIONAL_COSTS → none
(empty = no kicker)); `DecisionContext` exposes `mode_options` and
`target_candidates`; smoke gains assertions that a targeted spell path
surfaces CHOOSE_TARGETS (use a deck with a targeted effect, e.g. add
`Lightning Strike`-style card to goldfish decks or stage REMOTE-vs-FORGE_AI).

---

## 2. Snapshot / Restore + state hashing (gate item 5)

**Engine grounding (verified):** Forge has a native in-memory snapshot
mechanism — `GameSnapshot` (forge-game/.../GameSnapshot.java):
`makeCopy()` deep-copies the full game (players, zones, phase handler, stack
via `assignGameState`, entity mapping via `SnapshotEntityMap`), and
`restoreGameState(currentGame)` copies back with
`GameEventSnapshotRestored` fired. `Game.stashGameState()/restoreGameState()`
(Game.java ~203-219) wraps this behind
`EXPERIMENTAL_RESTORE_SNAPSHOT`. It is object-graph copying in-process —
**not** byte-serializable out of the box.

**Design: in-process token table (v1).**
- `GameRunner.snapshot()` → under runner lock, require state RUNNING and no
  outstanding decision; call `new GameSnapshot(game).makeCopy()` on the
  game thread via the existing park mechanism (an internal decision-like
  barrier) so the copy is thread-consistent; store the returned `Game` copy
  in a per-runner token map: `token = game_id || "-" || counter` (int64
  opaque), cap 32 tokens per game (evict oldest, log).
- `StateToken.token` = opaque bytes: UTF-8 of the token id + an HMAC-less
  checksum (token id already scoped per game; validity enforced
  server-side).
- `Restore(RestoreRequest)`: validates token belongs to this game and game
  is RUNNING; invalidates the outstanding decision if any (client must
  refetch); executes `snapshot.restoreGameState(game)` on the game thread;
  appends a harness event `type="SnapshotRestored"` with
  `detail=token=<id>` (deterministic, part of the stream).
- Expiry: all tokens for a game are dropped on game over / StopGame /
  registry removal.
- Determinism contract: same (seed, action prefix) → snapshot token ids are
  deterministic; restore → subsequent identical actions produce identical
  event suffixes (seq continues monotonically; the restore event itself is
  part of replay identity).
- **State hashing for cache keys:** SHA-256 over a canonical projection:
  turn, phase, active player, life totals, sorted (card name, controller,
  tapped, counters) for battlefield/hand/graveyard, library counts, mana
  pools, and the last 32 event-identity tuples. Hash is exposed in
  `GetState` (`state_hash` field, v4) and logged on snapshot. It is a cache
  key only — replay correctness still rests on the event stream.
- **Honest infeasibilities (v1):** tokens live in one harness process (no
  cross-process MCTS distribution without a serialization format — defer);
  snapshotting mid-decision is rejected (park first, answer or let it time
  out); `restore` across a natural game end is invalid (state OVER).

Proto additions:
```proto
message StateToken { bytes token = 1; int64 game_id = 2; }
message SnapshotResponse { StateToken token = 1; string state_hash = 2; }
// service: rpc Snapshot(GameQuery) returns (SnapshotResponse);  // was UNIMPLEMENTED
// Restore unchanged (returns Empty), plus FullState gains:
string state_hash = 12;
```

---

## 3. Event taxonomy + FullState v2

**Principle: stop substring-matching log text.** The engine already emits
structured records (`forge-game/src/main/java/forge/game/event/GameEvent*.java`,
58 verified types) with typed payloads. The harness registers as a game
observer for the subset below (via the game's event handler, alongside the
existing GameLog observer) and emits normalized events; raw log lines remain
in `detail_raw` (from GameLog) for audit.

**Normalized vocabulary (proto `GameEvent.type` + structured fields):**

| Normalized type | Engine source (verified) | Structured fields |
|---|---|---|
| `CardDrawn` | GameEventZone (DRAW) | player, card_id, card_name |
| `LandPlayed` | GameEventLandPlayed | player, card |
| `SpellCast` | GameEventSpellAbilityCast | player, card, sa_desc, stack_index, target_desc |
| `SpellResolved` | GameEventSpellResolved | player, card, fizzled, stack_desc |
| `SpellRemovedFromStack` | GameEventSpellRemovedFromStack | player, card |
| `PermanentEntered` / `PermanentLeftBattlefield` | GameEventCardChangeZone | player, card, from_zone, to_zone |
| `CardTapped` | GameEventCardTapped | card, tapped |
| `CardCounters` | GameEventCardCounters | card, counter_type, old, new |
| `PlayerCounters` | GameEventPlayerCounters | player, counter_type, old, amount |
| `AttachmentMoved` | GameEventCardAttachment | equipment, old_target, new_target |
| `ManaProduced` / `ManaSpent` | GameEventManaPool (ADD/REMOVE) | player, colors bitmask, delta |
| `LifeChanged` | GameEventPlayerLivesChanged | player, old, new |
| `PlayerDamaged` | GameEventPlayerDamaged | target, source_card, amount, combat, infect |
| `CardDamaged` | GameEventCardDamaged | card, source, amount, damage_type |
| `AttackersDeclared` | GameEventAttackersDeclared | player, attacker→defender map |
| `BlockersDeclared` | GameEventBlockersDeclared | defender, blocker→attacker map |
| `TriggerOrdered` | GameEventCardModeChosen (mode) | player, card, mode |
| `TurnStarted` / `Phase` | GameEventTurnBegan / GameEventTurnPhase | player, turn, phase |
| `Shuffled` | GameEventShuffle | player |
| `Scry` / `Surveil` | GameEventScry / GameEventSurveil | player, to_top, to_bottom |
| `Mulligan` | GameEventMulligan | player |
| `GameOver` | (harness terminal, unchanged) | winner, reason, outcome |

Proto: replace the free `type` string + `detail` string with:
```proto
message GameEvent {
  int64 seq = 1; int64 game_id = 2; int32 turn = 3; string phase = 4;
  string type = 5;            // normalized vocabulary above
  int32 player = 6;
  int32 card_id = 7;          // 0 = N/A
  string card_name = 8;
  string detail_raw = 9;      // raw log text, preserved
  int32 old_value = 10;       // counters/life deltas
  int32 new_value = 11;
  string extra = 12;          // sa_desc / target_desc / zone names / mode
}
```
(v4 is breaking for event consumers; Python `event_identity` gains
card_id/extra; determinism gate re-run.)

**FullState v2 additions:** per-player zones gain graveyard (names+counts),
exile (names), library (count only; top card only when revealed),
command-zone names; stack (`repeated StackEntry {int32 stack_index; string
sa_desc; string card_name; int32 controller;}`); typed mana pools:
```proto
message ManaPool { int32 white=1; int32 blue=2; int32 black=3; int32 red=4; int32 green=5; int32 colorless=6; }
```
`FullState.mana_pools` becomes `repeated ManaPool mana_pools` (index=player);
`Permanent` gains `repeated Counter {string type; int32 count;} counters`,
`int32 damage = 12;`. Hidden-info policy: a `view_as_player` field on
`GameQuery` (0 = observer) — hand/library-of-others redacted to counts.
Implementation lives in `ForgeEnvService.getState` with the existing bounded
retry; zone walks snapshot into immutable proto builders.

---

## 4. Trajectory persistence + SQLite schema

`combo_discovery/src/combo_discovery/store.py`:

```python
class ExperimentStore:
    def __init__(self, path: str | Path): ...            # sqlite file
    def start_experiment(self, *, engine_commit: str, proto_version: int,
                         policy_version: str, model_version: str,
                         config: dict) -> str: ...        # run id (uuid4)
    def record_game(self, run_id: str, result: GameResult,
                    decks: list[tuple[str, str]], seed: int,
                    player_types: list[int], max_turns: int) -> int: ...
    def record_decision(self, game_row: int, ctx: DecisionContext,
                        answer: Answer, accepted: bool = True) -> None: ...
    def record_events(self, game_row: int,
                      events: Iterable[pb.GameEvent]) -> None: ...
    def export_jsonl(self, table: str, path: Path) -> None: ...
    def close(self) -> None: ...
```

```sql
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS experiments (
  id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
  engine_commit TEXT, proto_version INTEGER, policy_version TEXT,
  model_version TEXT, config_json TEXT);
CREATE TABLE IF NOT EXISTS games (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES experiments(id),
  server_game_id INTEGER, seed INTEGER, decks_json TEXT,
  player_types_json TEXT, max_turns INTEGER,
  outcome TEXT, winner INTEGER, reason TEXT,
  turn_count INTEGER, duration_ms INTEGER,
  event_count INTEGER, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  game_row INTEGER NOT NULL REFERENCES games(id),
  decision_id INTEGER, player INTEGER, type TEXT,
  answer_json TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  game_row INTEGER NOT NULL REFERENCES games(id),
  seq INTEGER, turn INTEGER, phase TEXT, type TEXT,
  player INTEGER, card_name TEXT, detail_raw TEXT);
CREATE TABLE IF NOT EXISTS candidates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES experiments(id),
  card_names_json TEXT, status TEXT NOT NULL
    CHECK (status IN ('proposed','verified','refuted','inconclusive')),
  evidence_json TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS adjudications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  candidate_id INTEGER NOT NULL REFERENCES candidates(id),
  verdict TEXT, reviewer TEXT, notes TEXT, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_events_game ON events(game_row, seq);
CREATE INDEX IF NOT EXISTS idx_decisions_game ON decisions(game_row, decision_id);
```

Append-only: no UPDATE/DELETE anywhere; inserts use WAL mode
(`PRAGMA journal_mode=WAL`); `schema_version` table gates migrations
(bump + migration function per version). `runner.run_game` gains an optional
`store:` parameter — events recorded incrementally after drain; pool passes
it through. JSONL export mirrors each table.

---

## 5. Implementation rounds (≈ half-day each; ordering = dependencies)

1. **Proto v4: targeting/modes/optional costs** (Java + Python + live staged
   targeted-spell test). Closes PROJECT_TODO 1.3 targeting/modes/optional-cost
   checkboxes; gate item 4 complete.
2. **Snapshot/restore + state hashing** (Java + Python + restore-equivalence
   test: snapshot → divergent branch → restore → same actions → identical
   suffix). Closes gate item 5, TODO 1.5 snapshot items.
3. **Event taxonomy + FullState v2** (Java mapping table + Python
   event_identity update + determinism re-gate). Closes TODO 1.4 taxonomy +
   1.5 state items.
4. **Persistence: store.py + runner/pool integration + config file +
   version metadata** (record full trajectories; TODO section 3 core, 0.x
   leftovers).
5. **Hardening round:** real goldfish policy, controller conformance tests,
   cancellation tests, CI stub regen, asset-path independence (harness
   resolves decks via an explicit --assets arg), pool spawn/supervise +
   games/minute benchmark.
6. **Doc round:** README refresh, demo experiment script, replayable seeds —
   blog groundwork (TODO section 9 partial).

**YAGNI cuts for the research goal:** async client (sync + pool suffices);
multi-game hosting per process; sideboard/ante/plane dice/vote decisions;
convoke/delve/splice surfacing; Parquet export (JSONL first); planeswalker/
battle damage defenders; cross-process snapshot serialization. All documented
as deferred in the proto header/STATUS, revisit only if the science needs
them.
