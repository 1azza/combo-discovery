# Project Status

Last verified during the v2 protocol/lifecycle gate work.

## Verified

- [x] Forge fork exists at `/home/lza/Work/forge`
- [x] Forge source is pinned to `4f577da7b2a9074f9f66544aaf99405e38cf5ac3`
- [x] Java `forge-harness` Maven module exists
- [x] Shaded harness jar builds successfully
- [x] Forge card database boots headlessly
- [x] Python gRPC client connects to the harness
- [x] `Ping` works and reports `protocol_version=2`; client rejects mismatches
- [x] Games can be started from `.dck` files
- [x] Forge AI, goldfish, and remote player types are represented
- [x] Protocol v2: every per-game RPC routes by `game_id`; unknown/stale ids →
      `INVALID_ARGUMENT`, wrong-state calls → `FAILED_PRECONDITION`
- [x] Decisions carry a monotonic `decision_id`; submissions must echo it and an
      option that belongs to the outstanding decision; violations rejected
- [x] `PollEvents(cursor)` returns the per-game event buffer in seq order;
      seq is per-game monotonic starting at 1 and includes game/turn/phase metadata
- [x] Events are decoupled from `SubmitDecision`: goldfish-only and Forge-AI-only
      games stream their full event history without any remote decisions
- [x] Lifecycle state machine `CREATED → RUNNING → OVER → CLOSED`
- [x] `max_turns` is enforced: bounded games end as `OUTCOME_TURN_LIMIT`,
      winner `-1`, reason `turn_limit`
- [x] Outcome classification: `OUTCOME_WIN` (natural end observed live),
      `OUTCOME_TURN_LIMIT`, `OUTCOME_TIMEOUT`, `OUTCOME_ERROR`, `OUTCOME_STOPPED`
- [x] `GetDecision` waiters are woken on every terminal transition and receive
      `FAILED_PRECONDITION` instead of hanging
- [x] Remote-driven games run to natural completion (`OUTCOME_WIN` observed)
- [x] Determinism: same `(decks, seed, config)` produces byte-identical event
      streams including the final `GameOver` event (game_id excluded); a
      different seed diverges. Verified live on the real harness.
- [x] Nondeterministic GameOver event found and fixed: engine match-tally
      narration (HashMap-iteration winner credit on forced ends) no longer
      enters the event buffer; GameOver events are harness-generated from the
      deterministic terminal record
- [x] Deck fixture fixed: `goldfish_A.dck` manabase Plains → Forest
      (Grizzly Bears `{1}{G}` was uncastable, so games could never end naturally)
- [x] Smoke script bounded by `max_turns`, uses absolute deck paths, and cleans
      up the active game on failure
- [x] Client: dedicated long `decision_timeout` for the blocking GetDecision
      wait, bounded retry on transient deadlines, typed error mapping
- [x] Python unit tests pass: `43 passed, 1 skipped` (live test opt-in)
- [x] Live protocol test passes against the real harness
- [x] Java Maven compile/package succeeds
- [x] End-to-end smoke passes: `SMOKE PASS` (turn-limit game, remote-driven
      win, determinism check)

## Known Gaps

- [ ] Typed decision callbacks beyond priority (targets, modes, X values,
      mulligans, combat, selections) — gate item 4, next round
- [ ] `Snapshot`/`Restore` return `UNIMPLEMENTED`; no state hashing — gate item 5
- [ ] `FullState` is a partial inspection state, not a complete search state
- [ ] One active game per harness process at a time (v2 registry routes by
      `game_id` and rejects stale ids, but concurrent games are not hosted yet)
- [ ] Goldfish behavior is still a minimal pass/do-nothing-class policy
- [ ] Cancellation tests for timeout, client disconnect, and server shutdown
- [ ] Event taxonomy is best-effort mapping of raw Forge log types, not yet a
      normalized research vocabulary; raw log text is not preserved separately
- [ ] Trajectory persistence is not implemented
- [ ] Ontology/card-script extraction has not started
- [ ] Candidate generation and combo validation have not started
- [ ] MCTS and learned search have not started
- [ ] Scryfall/Spellbook integration has not started
- [ ] Evaluation baselines have not started

## Next Work

Remaining gate items before Layer 2 (see `ARCHITECTURE.md`):

1. Typed decision callbacks beyond priority (gate item 4).
2. Snapshot/restore and state hashing (gate item 5), plus controller
   conformance tests for every decision callback Forge uses.
