# Project Status

Last verified during the proto v4 round (spell-casting-path decisions):
the final oracle re-review's wave-3 fixes held, and v4 added the full
spell-casting path. **Gate item 4 is COMPLETE** — every meaningful decision
class is surfaced (long-tail callbacks deliberately AI-defaulted). Remaining
plumbing per PLUMBING_SPEC.md: snapshot/restore (gate item 5), event
taxonomy + FullState v2, persistence, hardening.

## Verified

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
- [ ] Remote priority options are AI-curated (`canPlaySa` filter, not a
      legality filter): legal actions the AI deems unattractive never appear
      as options — matters now that targeting has landed; switch to a
      pure-legality filter in a hardening round
- [ ] GetState zone snapshot uses bounded retry over live zone lists; an
      immutable game-thread snapshot is the deferred full fix (TODO in code)
- [ ] Long-tail callbacks deliberately keep AI defaults (mana payment,
      generic confirms, trigger ordering, votes/dice/sectors) — documented in
      the proto header, not a defect
- [ ] `Snapshot`/`Restore` return `UNIMPLEMENTED`; no state hashing — gate
      item 5
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

1. Gate item 5: snapshot/restore + state hashing (PLUMBING_SPEC.md section 2).
2. Event taxonomy + FullState v2 (PLUMBING_SPEC.md section 3).
3. Trajectory persistence + SQLite + config/metadata (PLUMBING_SPEC.md
   sections 1/4).
4. Hardening: legality-filtered priority options, real goldfish, conformance
   + cancellation tests, CI.
