# Project Status

Last verified during the ground-truth / evaluation round (Layer 2 science).
Gate items 1–5 are complete; the card corpus, predicate ontology, and the
known-combo evaluation layer are in place. Current focus: tuning hypothesis
precision against Commander Spellbook, driven from the TUI Card Lab.

## Verified

- [x] Science layer (Layer 2):
      - Card corpus: 33,688 Forge scripts parsed (0 errors) into schema v2
        (`cards`, `card_faces`, `card_scripts`, `card_effects`, 84,190 effects).
      - Predicate ontology (schema v3): 29-predicate vocabulary, restriction
        parsing, ability-scoped effects, 7 patterns in a per-pattern registry.
      - Ground truth (schema v4): Commander Spellbook bulk ingested —
        107,325 Vintage-legal variants, 504,709 known pairs (3,937 exact
        2-card), 5,614 aliases; Scryfall oracle-id bridge (33,636 cards).
      - Evaluation harness: pair/subset classification (`known_pair`,
        `contained_in_known`, `unmatched`, `missed`), precision@k / recall /
        F1 per card + per pattern + aggregate, FP/miss diagnostics, Tier-A/B
        novelty policy (never claims "novel" without a second source).
      - Baseline (Kiki-Jiki): 791 known combos, 78 exact 2-card variants;
        our 14 proposals → 12 `known_pair`, 2 `unmatched`
        (Eager Beaver, White Plume Adventurer); P=0.857, P@10=0.900,
        R=0.154; 66 missed (incl. Fear of Missing Out — a real known combo
        our current gates exclude; the tuning signal).
      - Aggregate pre-tuning: 437,463 proposals → 22 `known_pair`,
        1,015 `contained_in_known`, 436,426 `unmatched`; per-pattern precision
        is near zero outside `infinite_etb_loop` — the measurable tuning target.
      - TUI **Card Lab** (tab 4): pick a card → Known (Spellbook combos with
        step descriptions, prerequisites, results, popularity) vs Proposed
        (our hypotheses with mechanism/evidence), verdict badges
        (known/contained/candidate), a Missed section, card + per-pattern +
        aggregate metrics, and FP/miss diagnostics; `r` re-runs and persists
        an evaluation. Candidates rows now carry known/contained/candidate
        badges (never "novel" — two-source policy).
- [x] Protocol v6 (event taxonomy): the GameLog substring matcher is gone —
      engine events are built from Forge's structured GameEvent records via
      a single EventBus-registered collector (synchronous game-thread
      dispatch, no duplicate sources); 28-type normalized vocabulary
      (`EVENT_VOCABULARY` in runner.py, validated in the smoke); raw log
      text preserved in `detail_raw`; GameEvent carries card_id,
      old_value/new_value (counter/life deltas), and extra (zones, sa/mode
      descriptions). Deterministic `max_turns` moved to the structured
      `GameEventTurnBegan` handler (same game-thread timing).
- [x] Event semantics spot-checked live: SpellCast/LandPlayed/
      PermanentEntered (from/to zones in extra), LifeChanged with old/new,
      PlayerDamaged with amount/combat/infect, AttackersDeclared;
      229/358 events carried raw log text; TurnStarted count == turns+1
      (single source, no duplication).
- [x] FullState v2: typed per-player mana pools, stack entries (verified
      mid-resolution), exile and command zones, hidden-info redaction via
      `GetState(view_as_player=...)` (observer sees counts only; the chosen
      slot sees its own hand), and a flat `battlefield_cards` listing with
      full Permanent detail (typed counters — verified with Arcbound Worker
      modular — damage, attachments, tokens); `Zone.permanents` ids index
      into it.

- [x] Protocol v5 (snapshot/restore, gate item 5): `Snapshot` requires an
      outstanding decision (engine parked = quiescent), returns an opaque
      game-scoped `StateToken` (max 32 live, LRU-evicted, die with the game)
      plus a SHA-256 `state_hash` projection; `Restore` invalidates the
      outstanding decision, re-applies zone ordering (fixing a real
      `GameSnapshot` limitation: same-zone cards are appended, not
      repositioned), re-seeds the engine RNG deterministically from
      (game seed, snapshot id), truncates the event trajectory to the
      snapshot seq (numbering stays monotonic), and appends a deterministic
      `SnapshotRestored` marker; tokens are reusable (branch repeatedly).
- [x] MCTS determinism guarantee verified live: snapshot → branch → restore
      → replay the same answers TWICE → state hash stable and post-restore
      event suffixes byte-identical (relative-seq comparison). Priority
      decisions re-present naturally after restore; non-priority (combat/
      target) restores fall back to the abort NO-OP — documented limitation.
- [x] `state_hash` exposed in `SnapshotResponse` and `FullState`; token
      rules live-verified (foreign game rejected, >32 evicts oldest, tokens
      die with the game, snapshot without outstanding decision →
      FAILED_PRECONDITION).

- [x] Protocol v4 (spell-casting path): `CHOOSE_TARGETS` (server-validated
      candidates via `canTarget`, `TargetSelection` answer, mandatory/optional,
      count bounds), `CHOOSE_MODE` (option list, repeat rules honored), and
      `OPTIONAL_COSTS` (kicker selection, empty = no kicker). Every engine
      re-prompt is a fresh `decision_id`; illegal answers are rejected with
      the decision outstanding.
- [x] Remote casting now flows through the human `PlaySpellAbility` path
      (the AI casting path bypassed `setupTargets` — fixed by overriding
      `playChosenSpellAbility`), with a `canPayCost` filter so unpayable
      abilities don't spin the priority loop.
- [x] Live staged verification on the real harness: Giant Growth surfaced
      `CHOOSE_TARGETS` with correct candidates (own bears), illegal target
      rejected with decision outstanding, legal target accepted, spell
      resolved; `Return to Nature` surfaced `CHOOSE_MODE` (invalid mode
      rejected, valid accepted); `Kavu Titan` surfaced `OPTIONAL_COSTS`
      (empty = no kicker accepted).
- [x] Deck fixtures extended: `4 Giant Growth` + `4 Return to Nature` in
      both goldfish decks; smoke requires live `CHOOSE_TARGETS` +
      `CHOOSE_MODE` (both observed in the remote-driven game).

- [x] Forge fork exists at `/home/lza/Work/forge`
- [x] Forge source is pinned to `4f577da7b2a9074f9f66544aaf99405e38cf5ac3`
- [x] Java `forge-harness` Maven module exists
- [x] Shaded harness jar builds successfully
- [x] Forge card database boots headlessly
- [x] Python gRPC client connects to the harness
- [x] `Ping` works and reports `protocol_version=3`; client rejects mismatches
- [x] Games can be started from `.dck` files
- [x] Forge AI, goldfish, and remote player types are represented
- [x] Protocol v2 semantics: `game_id` registry routing, `decision_id` +
      option validation, `PollEvents` with per-game monotonic seq, lifecycle
      state machine, outcome classification, wake-on-terminal
- [x] Protocol v3 typed decisions (10 decision types with per-type payloads
      and oneof answers, per-type server validation; invalid arm/ids/sizes →
      `INVALID_ARGUMENT` with the decision left outstanding and retryable):
      `PRIORITY`, `MULLIGAN_KEEP`, `MULLIGAN_TUCK`, `DECLARE_ATTACKERS`,
      `DECLARE_BLOCKERS`, `ASSIGN_COMBAT_DAMAGE`, `ORDER_BLOCKERS`,
      `CHOOSE_CARDS`, `ANNOUNCE`, `SCRY_ARRANGE`
- [x] Live combat: a remote player declared Grizzly Bears as attackers
      (observed `DECLARE_ATTACKERS` + `MULLIGAN_KEEP` in the smoke run), the
      Forge AI opponent blocked/fought, and the game ended in a natural
      `OUTCOME_WIN` for the remote player
- [x] Deterministic `max_turns`: enforced on the game thread at the first
      turn boundary beyond N (max_turns=3 → turn 4 in every run; max_turns=20
      → turn 21 in every run). The wall-clock monitor remains only for
      `timeout_seconds`/`OUTCOME_TIMEOUT`.
- [x] Determinism gate holds at natural game end too (`max_turns=0`,
      unbounded, same seed → byte-identical streams incl. the GameOver tail)
- [x] Engine gameplay determinism confirmed by side-by-side log comparison:
      two same-seed runs were byte-identical through event 510 (turn 34)
      before the (now-fixed) turn-limit monitor race diverged them — no
      engine randomness issue exists on the exercised paths
- [x] `GameResult.decision_trace` records `(decision_id, decision_type,
      answer)` per remote decision; `default_policy` answers every v3 type
- [x] Client: protocol negotiation, dedicated blocking-wait timeout with
      bounded retry, typed error mapping, `GameNotActiveError` treated as
      normal game-over exit from the remote loop
- [x] Concurrency hardening (delta-review round):
      decision single-winner state machine (LIVE → ANSWERED/TIMED_OUT/ABORTED,
      one transition under the runner lock, exactly one fallback marker),
      with a validation lease so answer validation cannot race timeout/abort;
      `isOver()` reflects runner state so the final `GameOver` event is
      exactly-once and observable only after it is appended; previous game
      thread joined before RNG reseed on every stop→start handoff (join
      timeout refuses to reseed); blocker legality validated as a set via
      `CombatUtil.validateBlocks` plus correct per-blocker capacity checks
      (verified: one ordinary blocker accepted, capacity-0 multi-block
      rejected, menace two-blocker accepted); no engine calls under the
      runner lock; stop/start serialized under `startLock`; force-stop
      validates the request before stopping; `PLAYER_TYPE_UNSPECIFIED`
      rejected; IsGameOver returns one atomic (over, outcome, winner,
      reason) read
- [x] Combat-damage candidates fixed: the harness surfaces the attacker's
      ordered blockers (engine assignment order preserved), not unrelated
      battlefield attackers; default policy assigns all damage to the first
      blocker when blocked (always-legal by construction)
- [x] Pool correctness (delta-review + wave-2 rounds): per-seed round budget
      replaces the broken global 2×workers cap (25 seeds over 2 workers
      complete); successes consume no budget; timeout/stale errors are
      retryable job failures; `InvalidRequestError` (deterministic policy
      bugs) propagates without blaming workers; excluded workers probed
      one-per-iteration (rotating, 2s timeout) and re-admitted at most once
      per call; all-excluded pools wait for respawn readiness (bounded by
      `_startup_timeout`); wedged owned processes killed+respawned;
      probe/revive treat connection death AND timeout as "not recovered";
      `run_game` best-effort-stops its game on any failure; transient poll
      timeouts retried
- [x] Python unit tests pass: `103 passed, 1 skipped` (live test opt-in)
- [x] Live protocol test passes against the real harness (protocol v3)
- [x] Java Maven compile/package succeeds
- [x] End-to-end smoke passes: `SMOKE PASS` (turn-limit game, remote-driven
      natural win with typed decisions, determinism check)

## Known Gaps

- [ ] Combat-damage assignment validation is policy-side only: candidates are
      now the attacker's ordered blockers and the default policy is
      always-legal by construction (blocked → first blocker), but the server
      does not validate trample/deathtouch damage splits — deferred until
      the targeting round
- [ ] Stale-vs-policy error distinction relies on INVALID_ARGUMENT detail
      text markers because the harness exposes no machine-readable subcode
      (documented in `env.py` `_STALE_DECISION_MARKERS`); a proto subcode
      would make it robust — v4 item
- [ ] Engine AI can hang in mana-payment loops with certain aggressive decks
      (observed during Wave-2 staging with custom decks; engine-side, not a
      harness defect — the harness correctly detects the unterminated engine
      thread)
- [x] Remote priority options use a pure-legality filter (`canPlay` +
      `canPayCost`, deliberately **not** `canPlaySa`) — resolved in the
      hardening round; legal-but-AI-unattractive actions are surfaced
- [ ] `state_hash` coverage is incomplete for scenario work: it ignores
      library/hand/graveyard order, card ids, summoning sickness, attachments,
      player counters and lands-played, so distinct scenarios can collide.
      Fold a scenario hash into the projection when scenario injection lands
      (see `docs/WITNESS_SEARCH.md`)
- [ ] GetState zone snapshot uses bounded retry over live zone lists; an
      immutable game-thread snapshot is the deferred full fix (TODO in code)
- [ ] Long-tail callbacks deliberately keep AI defaults (mana payment,
      generic confirms, trigger ordering, votes/dice/sectors) — documented in
      the proto header, not a defect
- [ ] FullState library zones show counts only; revealed-card state (top
      card when revealed) is not yet implemented
- [ ] Non-priority (combat/target) snapshot restores fall back to the abort
      NO-OP rather than re-presenting the same decision
- [ ] `FullState` is a partial inspection state, not a complete search state
- [ ] One active game per harness process at a time (registry routes by
      `game_id`, concurrent games not hosted yet)
- [ ] Goldfish behavior is still AI-defaults + empty priority
- [ ] Cancellation tests for timeout, client disconnect, and server shutdown
- [ ] Event taxonomy is best-effort mapping of raw Forge log types; raw log
      text is not preserved separately
- [ ] Trajectory persistence is not implemented
- [ ] Ontology/card-script extraction has not started
- [ ] Candidate generation and combo validation have not started
- [ ] MCTS and learned search have not started
- [ ] Scryfall/Spellbook integration has not started
- [ ] Evaluation baselines have not started

## Next Work

1. Trajectory persistence + SQLite + config/metadata (PLUMBING_SPEC.md
   section 4).
2. Hardening: legality-filtered priority options, real goldfish, conformance
   + cancellation tests, CI.
