# Project Status

Last verified during the M1 harness work.

## Verified

- [x] Forge fork exists at `/home/lza/Work/forge`
- [x] Forge source is pinned to `4f577da7b2a9074f9f66544aaf99405e38cf5ac3`
- [x] Java `forge-harness` Maven module exists
- [x] Shaded harness jar builds successfully
- [x] Forge card database boots headlessly
- [x] Python gRPC client connects to the harness
- [x] `Ping` works
- [x] Games can be started from `.dck` files
- [x] Forge AI, goldfish, and remote player types are represented
- [x] Python receives Forge-generated remote decisions
- [x] Python submits decisions and Forge continues the game
- [x] Basic state queries work: turn, phase, life, hand, battlefield
- [x] Forge log events reach `StepResult.events`
- [x] Python unit tests pass: `16 passed`
- [x] Java Maven compile/package succeeds
- [x] M1 remote decision integration test passes

## Known Gaps

- [ ] `game_id` and `decision_id` are missing from decision routing
- [ ] Multiple simultaneous remote players are not safely supported
- [ ] Events are coupled to `SubmitDecision`
- [ ] Event sequence numbers are missing
- [ ] Full typed Forge decision coverage is missing
- [ ] Snapshot/restore is unimplemented
- [ ] `max_turns` is not enforced by the Java harness
- [ ] Full state serialization is incomplete
- [ ] Goldfish behavior is currently a minimal pass/do-nothing policy
- [ ] Trajectory persistence is not implemented
- [ ] Ontology/card-script extraction has not started
- [ ] Candidate generation and combo validation have not started
- [ ] MCTS and learned search have not started
- [ ] Scryfall/Spellbook integration has not started
- [ ] Evaluation baselines have not started

## Next Work

Implement the protocol/lifecycle gate described in `ARCHITECTURE.md` before
starting ontology or search work:

1. Decision identity and validation.
2. Independent sequence-numbered event stream.
3. Correct lifecycle and outcome semantics.
4. Typed decision callbacks.
5. Snapshot/restore and state hashing.
6. Replay and determinism tests.
