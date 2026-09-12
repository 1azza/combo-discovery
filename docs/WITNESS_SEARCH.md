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

## Solved false-positive class: baseline-pair and cross-turn recurrence

The verifier used to report `loops` for pairs that cannot loop. Live examples
(Kiki-Jiki + partner): Keldon Overseer, Elven Raft-Steerer, and — notably —
**Firbolg Flutist**, which had been recorded as a genuine discovery before this
bug was found.

Root cause (from live runs, 2026-09-12; `combo-witness --json` + a driver that
dumps per-observation `turn` and resources). Two independent traps:

1. **Baseline pair.** Observation 0 is sampled *before* the first iteration
   completes. When the engine has not acted yet, observations 0 and 1 are
   bit-identical, and `detect_loop`'s degenerate branch certified `loops` from
   that pair alone. This fired on *every* candidate whose policy made no
   immediate progress, whether or not a loop existed. The earlier `turn` guard
   did not help because that pair is trivially in the same turn.
2. **Cross-turn recurrence.** Kiki untaps in the untap step, so a card whose
   copy does not untap Kiki produces a structural signature that recurs *across
   turns* while tokens/permanents grow and the opponent loses life. That is a
   beatdown line, not an infinite loop.

The earlier hypothesis in this document — that cast-conditional / landfall
triggers were the cause — was **wrong**. Those cards were false-positive, but
the verdict came from the baseline pair, before their missing trigger was ever
relevant.

Fix (in `detect_loop`):
- ignore the pre-iteration baseline: a verdict needs >= 2 **post-baseline**
  observations;
- require both endpoints of any recurrence (and the degenerate identical-hash
  check) to share a **known same turn**; an unknown turn (0) stays permissive.

Live acceptance (fresh standalone harness on 50051):

| pair | before | after | truth |
|---|---|---|---|
| Kiki + Keldon Overseer | loops | refuted | false positive |
| Kiki + Elven Raft-Steerer | loops | no_loop | false positive |
| Kiki + Firbolg Flutist | loops | refuted | false positive |
| Kiki + Deceiver Exarch | loops | loops | genuine |
| Kiki + Pestermite | loops | loops | genuine |
| Kiki + Reptilian Recruiter | loops | loops | genuine |

The discriminators are now explicit: a genuine loop keeps `turn` constant across
iterations (Pestermite/Deceiver/Reptilian stay in turn 1 while tokens grow),
whereas the false positives advance `turn` (1 -> 3 -> 5 -> 7) as Kiki untaps
normally.

Consequence for the discovery claim: **Firbolg Flutist was a false positive**,
and the pipeline's own two-source policy then removed the only survivor.
**Kiki-Jiki + Reptilian Recruiter is documented elsewhere** — it was called out as
"another Kiki-Jiki infinite" on MTG Salvation and r/magicTCG the day BLB previewed
(2024-07-15) and appears in real decklists — even though Commander Spellbook has
no variant for it (0 of 791 Kiki combos). It is a genuine **Spellbook coverage
gap**, not a novel discovery.

## Third false-positive class: counter-only growth (combat loops)

Found while running the fresh combat-loop pool (`q:combat_loop`). Pairs such as
Aurelia, the Warleader + Genji Glove, Genji Glove + Lightning Runner, and
Hexplate Wallbreaker + Godo, Bandit Warlord were certified `loops` with a
*sustained same-turn recurrence* — exactly the shape the baseline/turn guards
accept.

They are still false positives, and the cards explain why: every extra-combat
trigger in those pairs is gated to **once per turn** ("if it's the first combat
phase of the turn" / "first time each turn"), so the chain is bounded, not
infinite. The only thing growing across iterations was `casts` and
`spells_resolved` — counters the *witness policy itself* drives by re-acting each
pass — while the board stayed static and mana drained (40 -> 30).

Fix: `GAME_STATE_GROWTH_KEYS = (mana, tokens, life, damage, permanents)`. A
same-turn recurrence may only certify `loops` when at least one of these grows.
Growth in event counters alone (`casts`, `spells_resolved`, `extra_phases`) now
returns `inconclusive` with reason "only policy-driven event counters grew".

Live acceptance: all six combat pairs (Aurelia / Genji Glove / Hexplate
Wallbreaker / Godo / Lightning Runner combinations) went `loops` ->
`inconclusive`, while Deceiver Exarch, Pestermite and Reptilian Recruiter stayed
`loops`.

Residual limitations:
- `link_hits` is **not** a valid mechanism gate: a genuine untap that resolves
  automatically (Deceiver Exarch) never appears as a policy action, so a real
  loop still reports `link_hits=[0, 6]`. Mechanism execution must be judged from
  events/state, not from policy link counters.
- A loop that genuinely spans turns (e.g. an extra-turn chain) is rejected by
  the same-turn rule. That is a known, documented false-negative class.
- Requiring a game-state resource forfeits legitimate loops whose only growth is
  casts/spells (e.g. some buyback/storm lines) — a second documented
  false-negative class, chosen deliberately over shipping false positives.
- Extra-combat loops backed by a genuinely unbounded extra-phase engine are not
  distinguishable yet, because `extra_phases` is not populated by the driver.
- One completed iteration cannot certify a loop; runs with `--max-iterations 1`
  now return `inconclusive`.

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
