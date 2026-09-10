# Project Status

Last verified during the v3 typed-decision round (gate item 4, partial).

## Verified

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
- [x] Python unit tests pass: `69 passed, 1 skipped` (live test opt-in)
- [x] Live protocol test passes against the real harness (protocol v3)
- [x] Java Maven compile/package succeeds
- [x] End-to-end smoke passes: `SMOKE PASS` (turn-limit game, remote-driven
      natural win with typed decisions, determinism check)

## Known Gaps

- [ ] Spell targeting and mode selection are still engine-internal: a remote
      player's spells resolve with AI-chosen targets/modes
      (`chooseTargetsFor`/`chooseModeForAbility` not yet surfaced) — next
      slice of gate item 4, together with optional costs
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

1. Finish gate item 4: surface spell targeting (`chooseTargetsFor`),
   mode selection (`chooseModeForAbility`), and optional costs
   (`chooseOptionalCosts`) — the spell-casting-path decisions.
2. Gate item 5: snapshot/restore and state hashing, plus controller
   conformance tests.
