# Witness Search — Design & Feasibility

Answer to: *"can we test games with a pre-configured board state, and drive a
hypothesized combo line to see if it loops?"* — **Yes.** Forge already ships a
GUI-free scenario serializer (`GameState`); the dev-menu cheats are thin
wrappers around the same engine calls. Spec below.

## The key discovery: `GameState` is a headless scenario format

`forge-game/src/main/java/forge/game/GameState.java` parses and applies a
complete game state with no GUI:

- `parse(List<String>|Stream|InputStream)` (~485-497) → `applyToGame(game)`
  (~582) → `applyGameOnThread` (~586-674).
- Grammar covers: `turn=`, `activeplayer=pX`, `activephase=`, per player
  `pXlife=`, `pXmanapool=`/`pXpersistentmana=` (e.g. `W W U`), `pXcounters=`,
  `pXlandsplayed=`, and ordered zones `pXbattlefield=`, `pXhand=`,
  `pXgraveyard=`, `pXlibrary=` (list order = library order), `pXexile=`,
  `pXcommand=`; card spec `Name|Set:X|Art:N|Tapped|SummonSick|Counters:TYPE=N|Damage:N|Owner:pX|NoETBTrigs|Id:N`.
- The human implementation (`PlayerControllerHuman.DevModeCheats`,
  ~2852-3660) and desktop/mobile menus (`CDev`, `DevModeMenu`, `VDevMenu`) are
  pickers over these calls; `GameState` is the reusable core. The `IDevModeCheats`
  ops map to: `Player.setLife`; dummy-`Card` + `AbilityManaPart.produceMana`
  (mana); `Card.fromPaperCard`/`CardFactory.getCard` + `GameAction.moveTo*` /
  `PlayerZone.setCards` (cards/zones); `Card.addCounterInternal`;
  `card.tap()/untap()`; `PhaseHandler.devModeSet(phase, player, turn)`.

## What v1 needs (and the two things that don't exist yet)

**Settable today (no new engine primitive):** life, mana pool, counters,
lands-played, turn/phase/active player, battlefield permanents (tapped,
sick, counters, damage), hand, graveyard, library order, exile, command.

**Must add:**
1. **A synchronous apply entry point.** `GameState.applyToGame` calls
   `game.getAction().invoke(...)`, which off the engine thread runs on a
   separate `Game-N` pool thread and returns immediately (no completion
   signal) — racy/nondeterministic for a harness. Add
   `applyToGameBlocking(Game)` that calls `applyGameOnThread` directly, the
   way `GameSnapshot.restoreGameState` is already invoked while parked.
2. **A park/invalidate handshake** so injection lands while the engine is
   quiescent and the stale pre-injection PRIORITY options are re-surfaced —
   reuse the `RESTORED` sentinel pattern in `RemoteController`.

**Explicitly defer (not needed for v1):** attachments, remembered/imprint and
chosen-entity cross-references (HashMap iteration order → nondeterministic),
combat setup, scripted precast/stack states, planar/puzzle states.

## Determinism requirements

- Apply **on the game thread while parked**; never the off-thread `invoke` path.
- Card-id assignment is deterministic (`CardFactory.getCard` →
  `game.nextCardId()`) **given fixed creation order**; `applyGameOnThread`
  iterates `EnumMap<ZoneType>` (stable ordinal order), so zone ordering is
  Python-controlled. Avoid cross-reference maps in v1.
- Injection consumes no RNG (no shuffling); the pre-existing seed shuffle
  remains the RNG source. A scenario is reproducible from
  `(seed, canonical scenario bytes, injection point)`.
- Append a deterministic `ScenarioInjected` marker event (same mechanism as
  `SnapshotRestored`) and **fold a scenario hash into `state_hash`**.
- **Hash coverage gap to fix:** `state_hash` today covers turn/phase/active/
  life/mana/hand names/battlefield keys/graveyard names/library *size*/exile/
  stack/last-32 events — it ignores library order, hand/graveyard order, card
  ids, summoning sickness, attachments, player counters, lands-played, chosen
  types. Two different scenarios with the same library size currently collide.
- `Snapshot`/`Restore` keeps working after injection (the harness token
  deep-copies the live game; restore re-applies `devModeSet` and
  `reorderOrderedZones`, and re-seeds `MyRandom`).

## Witness search (v1)

- **Driver:** the algebra's `Combo` object already carries ordered `links`
  (`kind`, `subkind`, `motif`, `src`/`dst` card names), `mechanism`,
  `preconditions`, `infinite` — that is the line to walk. (`Spellbook` `steps`
  is *not* imported today — only prose `description`; a re-import would be
  needed to follow canonical steps. Drive from ontology links for v1.)
- **Policy:** `WitnessPolicy` (same `Callable[[DecisionContext], Answer]`
  protocol as `default_policy`) advances a cursor over the links: at
  `PRIORITY` pick the option matching the cursor link's ability; at
  targets/modes/cards/announce use the link's parameters; one pass = one loop
  iteration.
- **Loop detection:** after each iteration, build a *witness signature* from
  FullState v2 that excludes monotonic resources but captures structure
  (battlefield names/tapped/counters, phase, active player, life, zone
  counts). A loop is `signature[i] == signature[j]` **and** a resource grew
  (total mana, tokens, life, damage, storm/cast count, extra phases).
  Identical consecutive `state_hash` is a degenerate loop. `state_hash` alone
  is insufficient (its last-32-event window changes each iteration).
- **Evidence to record:** verdict (`loops` / `no_loop` / `inconclusive` /
  `refuted` / `error`), canonical scenario, action trace, event seq range,
  `state_hash` before/after, the repeated signature pair, per-iteration
  resource deltas, iterations, seeds.

## Integration shape

**Proto v7** (both copies): `rpc SetupScenario(SetupScenarioRequest) returns
(SetupScenarioResponse)`;
`SetupScenarioRequest{game_id, active_player, turn, phase, repeated PlayerScenario players, bool require_outstanding_decision}`;
`PlayerScenario{life, map<string,int32> mana, repeated CardSpec battlefield/hand/graveyard/library/exile}`;
`CardSpec{name, set, tapped, sick, counters, damage}`;
`SetupScenarioResponse{state_hash, applied_events}`. Bump `PROTOCOL_VERSION=7`.

**Harness:** `ForgeEnvService.setupScenario` → `GameRunner.setupScenario`
(require RUNNING + LIVE outstanding decision, mirroring `snapshot()`, under
`snapshotLock`); proto → `GameState` lines → `applyToGameBlocking`; append
`ScenarioInjected`; include the scenario hash in `projection()`; invalidate the
outstanding decision and complete the sink with a `SCENARIO_APPLIED` sentinel
handled like `RESTORED`.

**Python:** `env.setup_scenario`; `witness.py` (`Scenario`, `WitnessPolicy`,
`run_witness`, `detect_loop`); schema v6 append-only
`witness_runs(id, started_at, engine_commit, proto_version, policy_version, scenario_json, seeds_json, params_json, notes)`
and `witness_results(id, run_id, candidate_kind, candidate_key, card_names_json, verdict CHECK(...), infinite, iterations, signature_json, resource_deltas_json, state_hash_before, state_hash_after, event_start_seq, event_end_seq, trace_json, created_at)`;
TUI Candidate/Lab badge/action; `pool.map_witness`. Do not mutate
`combo_hypotheses.status` (append-only; `adjudications` is the existing bridge).

## Known false-positive class: cast-conditional and landfall triggers

The verifier reports `loops` for some pairs that cannot legitimately loop.
Verified examples (Kiki-Jiki + partner, live harness):

| pair | card's relevant trigger | why it is a false positive |
|---|---|---|
| Kiki-Jiki + **Keldon Overseer** | `ChangesZone | ValidCard$ Card.Self+**kicked**` | scenario injection places the card *without casting*, so a kicked ETB cannot fire |
| Kiki-Jiki + **Elven Raft-Steerer** | `ChangesZone | ValidCard$ **Land.YouCtrl**` (landfall) | no land enters per iteration, so the untap never happens |

Diagnosis (from a live run of Keldon Overseer): `link_hits=[0, 4]` — the
mechanism's untap link **never executed**, while the copy link fired once per
turn as Kiki untapped in the untap step. The detector still certified a loop
because the state structure recurs and a tracked resource grew, and — for these
cards — the growth is partly the injection itself and partly cross-turn
activity.

Guards added so far:
- Observation carries the engine `turn`, and `detect_loop` rejects a recurrence
  whose two observations are in *different known turns* (an infinite combo
  iterates within one turn).
- `WitnessPolicy` is choice-aware (untap over tap, target the engine card).

**Still required (not done):** the same-turn guard only rejects when the
observation turns are known *and* differ; for these false positives the turns
do not discriminate, so the verdict is still `loops`. The principled fix is to
require the *mechanism* to execute within a turn — options:
1. verify the observation `turn` advances across a multi-turn run and
   re-tighten the same-turn recurrence (e.g. require ≥2 completed iterations in
   one turn, with growth between them);
2. treat a mechanism link that never fires as disqualifying — but note that a
   genuine loop can have a *passive* link (FOMO's attack trigger never appears
   as a priority option), so this needs a per-link "expected to surface" flag;
3. add a cast-faithful staging mode (cast the card with the required kicker
   etc. instead of placing it) or return `inconclusive` with reason
   `unverifiable_staging` for cast-conditional abilities.

Until then, treat `loops` verdicts for cards whose ability depends on *how they
were cast* or on *another card type entering* as unverified.

## Remote optional triggers & tap/untap semantics

No new proto/decision type is needed for a combo trigger that is both
**optional** (`OptionalDecider$ You`) and contains a **tap-or-untap** choice
(e.g. Pestermite / Deceiver Exarch / Breaching Hippocamp under Kiki-Jiki):

- **Optional trigger.** The engine asks the *decider's* controller at
  resolution (`WrappedAbility.resolve` → `confirmTrigger`). `RemoteController`
  answers `true` for a remote player, so the witness player always takes its own
  "you may" trigger. There is no CONFIRM decision type in protocol v7 and none
  is required — the policy cannot decline a confirmation, which is the correct
  conservative default for a loop-closing ETB.
- **Trigger targeting.** `RemoteController.orderAndPlaySimultaneousSa` /
  `playTrigger` route a remote trigger whose ability chain uses targeting
  through `prepareRemoteTrigger` → `setupTargets()` → `chooseTargetsFor`, i.e.
  the target is surfaced as `DECISION_TYPE_CHOOSE_TARGETS` and answered by the
  policy's link-aware selection (never the AI heuristic).
- **Tap or untap.** `TapOrUntapEffect` calls the *tapper's* controller
  `chooseBinary(..., BinaryChoiceType.TapOrUntap, default)`. The inherited AI
  hardcodes `tap`; `RemoteController.chooseBinary` instead surfaces a
  `DECISION_TYPE_CHOOSE_MODE` with descriptions `Tap` (id 0) / `Untap` (id 1).
  `WitnessPolicy._choice_mode` answers `Untap` whenever `_loop_needs_untap()`,
  so the token copy untaps Kiki and the cycle closes.

The pair must be built from the current sources; a harness jar older than the
`RemoteController` trigger overrides silently falls back to the AI (decline +
tap) and refutes every such combo.

## Effort & risks

**v1 ≈ 5–6 half-days:** proto+stubs 0.5; sync apply + marker/hash 0.5–1;
runner/service handshake 1; Python scenario + witness + loop detector 1.5–2;
store + tests 1; TUI/CLI 0.5–1; plus debugging against real combos.

**Top risks:**
1. **Thread affinity** — the off-thread `GameAction.invoke` path is the #1
   nondeterminism/race risk; the synchronous apply wrapper is mandatory.
2. **Engine/AI interference** — mana payment, confirms, trigger ordering, and
   other non-overridden callbacks can drift a scripted line. (Note: the claim
   that REMOTE priority options are AI-curated is **stale** — the code uses
   `canPlay` + `canPayCost`, deliberately not `canPlaySa`.)
3. **Loop-detection fidelity** — signatures may not recur bit-identically;
   needs a deliberate signature and an iteration budget.
4. **Scenario/state-hash collisions** — library order/ids/sickness/attachments
   are excluded from `state_hash` today; fold the scenario hash in.
5. **Injection-order nondeterminism** — avoid attachment/remembered maps in v1.
6. **Card resolution** — names must resolve to exactly one `PaperCard`;
   DFC/split/specialize/token faces need explicit set/face handling.
7. **No structured Spellbook steps** — re-import required if step-following is
   wanted; v1 drives from ontology links.
8. **Harness limits** — max 32 live snapshot tokens per game and one active
   game per process constrain parallel witness search.
