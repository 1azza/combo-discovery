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

## Measured witness recall on KNOWN combos (Lever 0)

Instrument: `uv run python scripts/known_recall.py --mode home`. It runs the real
verifier on Commander Spellbook's **exact 2-card** combos and splits the outcome
by engine type, so "we could not test it" is separated from "we tested it and
said no". Measured 2026-09-12 over the 133 resolvable known combos in the
copy/untap class (a copy engine plus an untapper):

| engine | n | loops | refuted/no_loop | inconclusive | recall |
|---|---|---|---|---|---|
| all | 133 | 57 | 25 | 51 | **43%** |
| activated (`{T}`-cost copy) | 99 | 54 | 12 | 33 | **55%** |
| triggered (attack/ETB/loyalty) | 34 | 3 | 13 | 18 | **9%** |

Progression across the fixes: 32% -> 35% (activated/static copy engines, life out
of the signature) -> **43%** (credit automatic trigger events to links, and sample
per trigger so a trigger-driven run collects more than the baseline sample).
Activated-engine recall is now 55%. Triggered engines rose 3% -> 9% but remain
the weak spot: crediting a trigger is not enough, because a triggered line must
be *repeated* by playing the game (upkeep after upkeep, combat after combat) —
that is the goal-directed pilot's job.

Readings:

- On its single best class the verifier confirms **about half of known combos**.
- **Triggered engines are effectively undriveable** (3%): the policy only acts on
  offered decisions, so attack/ETB/loyalty copy engines stall. Generator output
  now refuses to compose copy loops around them.
- **17 known combos are still actively `refuted`/`no_loop`** — real false
  negatives, each a concrete, diagnosable target. Most reduce to engines that
  cannot be driven (triggered) or loops with no net resource growth (e.g.
  `Palinchron + Molten Echoes`).
- The largest single cluster is **staging, not logic**: 22 `Ghired, Mirror of the
  Wilds` pairs return `inconclusive` with exactly 2 executed actions because
  Ghired's granted ability needs "target token you control that entered this
  turn" and the scenario stages no token.

Implication: the recall ceiling is the **player + scenario staging**, not the
loop judge. Fixing the false negatives and the staging gaps raises trustworthy
yield without any search/ML work.

## Analogue transfer (Lever 1)

Instrument: `uv run python scripts/analogue_transfer.py`. It takes known
activated copy engines (Kiki-Jiki, Splinter Twin), enumerates cards with the same
*functional* ETB-untap shape as the known combo partners, drops pairs already in
Spellbook, and witness-verifies the rest. It deliberately leans on where
Spellbook lags: functional near-reprints and recent sets.

Result (2026-09-12): 2 engines x 39 partners = **49 uncatalogued analogue pairs,
4 verified `loops`**.

| candidate | Spellbook | independent source | status |
|---|---|---|---|
| Kiki-Jiki + Reptilian Recruiter | absent | Reddit / MTG Salvation (2024-07-15) | documented — not novel |
| Splinter Twin + Giant-Sized Flying Ant | absent | not found | **uncatalogued** |
| Splinter Twin + Janjeet Sentry | absent | TappedOut (2018) | documented — not novel |
| Splinter Twin + Reptilian Recruiter | absent | same card as above | documented |

The one surviving candidate, `Splinter Twin + Giant-Sized Flying Ant`, is legal
(black-bordered, Vintage/Commander) and the loop holds (the token's modal ETB
untaps the original). It is a **database gap, not a new mechanism**: the same
interaction is already catalogued as `Kiki-Jiki + Giant-Sized Flying Ant`. The
card is from Marvel Super Heroes (2026-06-26) — a very recent set, which is where
Spellbook lags most.

Takeaway: the method reliably produces uncatalogued pairs (4/49), but most are
known in another form or documented in community threads. Genuine *novel
mechanics* still require the search/player work.

## Fourth false-positive class: mana-consuming recurrences

Found by widening the engines in the analogue run. `Orthion, Hero of
Lavabrink` (`{1}{R}, {T}: Create a token that's a copy of another target
creature you control`) with any ETB untapper recurs in-turn with tokens growing,
so the judge certified `loops`. But the untapper untaps **Orthion**, not the
lands: every pass costs mana, so the "loop" is bounded by the starting pool
(observed mana 40 -> 39 -> 38 ...).

Fix: a same-turn recurrence may only certify `loops` when **mana is
non-decreasing** across it. A recurrence that strictly consumes mana is bounded
by the pool and returns `inconclusive` with reason "recurrence consumes mana each
pass".

Live: `Orthion + Pestermite` `loops` -> `inconclusive`; the genuine loops
(Kiki/Pestermite, Splinter Twin, Combat Celebrant, Reptilian) and the
false-positive matrix are unchanged.

**Resolved:** `The Jolly Balloon Man` also costs `{1}` to activate
(`Cost$ 1 T` in its Forge script, versus `Cost$ 1 R T` for Orthion) yet its runs
reported mana constant at 40 and verified as `loops`, while Orthion's pool
drained. Root cause found: the harness's FullState mana reader used
`MagicColor.COLORLESS` (0) while the engine stores colorless mana at
`ManaAtom.COLORLESS` (32), so the 8 injected colorless mana were invisible
(reported 40 instead of 48). The AI prefers to spend that invisible mana on
generic shards, so a pure-generic cost looked free. Fixed in the harness by
summing both keys. `The Jolly Balloon Man` now reports 48 and drains per pass ->
`inconclusive`, as does Orthion; the genuine loops are unchanged.

## Search player (C)

Goal: a goal-directed player that finds a legal decision sequence producing a
loop, since the scripted policy only follows a fixed recipe. Built additively in
`src/combo_discovery/search.py`; the witness driver and the harness are untouched.

| milestone | what | status |
|---|---|---|
| M1 | `search_for_repeat` — deterministic DFS over PRIORITY options with snapshot/restore backtracking, bounded by `max_nodes`/`max_depth`/`max_branch`, seeking a sequence where a named card fires twice | done |
| M2 | `search_then_verify` — replay the found sequence through `run_witness` (`SequentialPolicy`) so `detect_loop` decides | done |
| M3 | `witness_with_search` fallback + `known_recall --search` | done, **measured +0** |
| M4a | harness snapshot/restore for *non-priority* decisions | **not bounded** — priority has an "ask me again" contract (empty list); the non-priority callbacks have none and fall back to an abort NO-OP, so a real fix needs engine-level re-entrancy |
| M4a' | `search_by_replay` — hybrid replay branching: branch at a non-priority decision by replaying the game from the start with one substituted answer (the harness is deterministic) | done, **0/7 payoff** |
| M4b | `search_for_loops` — make the judge's verdict the objective: verify the baseline, then one sequence per recorded variant; first `loops` wins, else the strongest verdict | done, **0/7 loops** |

Measured findings (the 70-pair recall slice unless noted):

- The search fallback changed **zero** verdicts (triggered 3/17 and activated
  22/53 with and without `--search`), at ~1.8x wall-clock.
- Replay branching flipped **0 of 7** triggered-inconclusive pairs, and the "fires
  twice" objective was reached in the *baseline* by 2 of them — so the objective,
  not the branching, was the problem.
- M4b (verdict-directed) still found **0/7 loops**; the failures are
  driver/observation reasons ("need at least two post-baseline observations",
  counter-only growth, policy-never-matched), i.e. those pairs do not loop on the
  injected board, or need conditions/objects the scenario does not provide.

Conclusion: the search machinery is built and aimed at the judge, but it cannot
manufacture a loop that is not there. The blocked recall on the triggered class is
a property of the candidate set and of scenario staging, not of search power.

Known limitation: `SequentialPolicy` replays positionally, so once a variant
diverges the decision types can differ and a recorded answer lands on the wrong
type; `run_witness` reports that as `error` (ranked weakest, never overriding).
