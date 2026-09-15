# Project Status

Last verified on the live harness and full test suite, 2026-09-12.

The pipeline is **end-to-end and sound**: a headless rules oracle (Forge behind a
gRPC harness), a card-effect corpus + interaction algebra, Commander Spellbook
ground truth, and a witness verifier that plays candidate combos in the real
engine. The verifier has been hardened through six concrete correctness fixes
(five loop-judge false-positive classes plus one harness mana-read bug), each
reproduced live and guarded by a regression matrix.

Current focus: **trustworthy recall + candidate yield**. Recall on known combos
is now measured (`scripts/known_recall.py`), and candidate novelty is probed by
analogue transfer (`scripts/analogue_transfer.py`).

## Verified

### Engine / protocol

- [x] Forge fork at `/home/lza/Work/forge`, base pinned to
      `4f577da7b2a9074f9f66544aaf99405e38cf5ac3` plus local harness work.
- [x] **Protocol v7**: 13 typed decision types (`PRIORITY`, `MULLIGAN_KEEP`,
      `MULLIGAN_TUCK`, `DECLARE_ATTACKERS`, `DECLARE_BLOCKERS`,
      `ASSIGN_COMBAT_DAMAGE`, `ORDER_BLOCKERS`, `CHOOSE_CARDS`, `ANNOUNCE`,
      `SCRY_ARRANGE`, `CHOOSE_TARGETS`, `CHOOSE_MODE`, `OPTIONAL_COSTS`).
      Canonical proto mirrored byte-identically between both repos.
- [x] Snapshot/restore (v5) with deterministic RNG reseed and a `state_hash`
      projection; scenario injection (v7) on the game thread with no RNG use.
- [x] Scenario injection now supports **attachments**: `CardSpec.id` /
      `attached_to` (proto fields 8/9) let an Aura **or Equipment** be staged
      attached to its host, so the granted ability is offered and driven
      (`Umbral Mantle`, `Thornbite Staff`, `Splinter Twin`).
- [x] 28-type normalized event vocabulary (v6), structured GameEvent source.
- [x] Harness mana read fixed: colorless mana is read from the engine's
      `ManaAtom.COLORLESS` key (was `MagicColor.COLORLESS`, which hid 8 injected
      mana and made generic costs look free).

### Science layer

- [x] Corpus: 33,688 Forge scripts parsed, 0 errors, 84,190 typed effects.
- [x] Predicate ontology + **interaction algebra** (ports / links / cycles /
      queries / budgets); 12 patterns. Fresh algebra pool after precision gates:
      **3,472 hypotheses**, no illegal (Un-set/novelty) cards, no triggered copy
      engines.
- [x] Ground truth: Commander Spellbook — 107,325 Vintage-legal variants,
      504,709 pairs (3,937 exact 2-card), 33,636 Scryfall oracle-id links.
- [x] Evaluation: `known_pair` / `contained_in_known` / `unmatched` / `missed`
      classification, precision@k / recall / F1, FP/miss diagnostics, Tier-A/B
      two-source novelty policy (never claims "novel" from one source).
- [x] Store schema v6, append-only (WAL, no UPDATE/DELETE).
- [x] TUI (omarchy-styled): Corpus, Experiments, Candidates, Card Lab, Activity.

### Witness verifier

- [x] Scenario-driven witness search: inject a board, drive decisions with a
      choice-aware policy, detect structural recurrence + resource growth.
- [x] **Five false-positive classes found and fixed** (each live-reproduced,
      each guarded by the acceptance matrix below):
      1. the pre-iteration baseline pair certified a loop;
      2. cross-turn recurrence treated as a loop;
      3. counter-only (policy-driven) growth treated as a loop;
      4. life totals in the structural signature broke combat loops;
      5. mana-consuming recurrences certified as infinite (bounded by the pool).
- [x] Acceptance matrix (all live on the harness): Kiki + {Deceiver Exarch,
      Pestermite, Reptilian Recruiter, Bounding Krasis, Breaching Hippocamp,
      Zealous Conscripts, Village Bell-Ringer, Sky Hussar, Hyrax Tower Scout,
      Glamermite, Great Oak Guardian} → `loops`; Splinter Twin + Deceiver Exarch
      → `loops`; Combat Celebrant + Kiki → `loops`; Keldon Overseer / Elven
      Raft-Steerer / Firbolg Flutist → not loops; Aurelia + Genji Glove →
      `inconclusive`.
- [x] **Measured recall on known combos** (`scripts/known_recall.py`): over 133
      known exact 2-card copy/untap combos — **35% overall**, **46% on
      activated-engine combos**, **3% on triggered-engine combos**.

### Candidate yield

- [x] Generator precision gates: recursion/flicker origin, one-shot (instant/
      sorcery) engines, activated/static copy engines only.
- [x] Analogue transfer (`scripts/analogue_transfer.py`): functional ETB-untap
      analogues of known partners. 49 uncatalogued pairs → 4 loops → after
      independent triage **1 uncatalogued pair survives**
      (`Splinter Twin + Giant-Sized Flying Ant`, a 2026-set card).
- [x] Honest result: **no genuinely novel mechanic has been found.** Every
      near-miss was either documented elsewhere (Reddit / TappedOut / MTG
      Salvation) or exposed as a verifier bug.

### Tests

- [x] Python: `546 passed, 1 skipped` (`uv run pytest -q`).
- [x] Java conformance tests + Maven package succeed.
- [x] `scripts/check.sh` (stub regen + pytest + optional Java compile) passes.

## Known Gaps

- [ ] **Recall ceiling is the player.** The witness policy only acts on offered
      decisions, so triggered engines (attack/ETB/loyalty) are effectively
      undriveable (3% recall) and loops needing combat/phase navigation return
      `inconclusive`. General search is the remaining large workstream.
- [ ] **Net-neutral loops** (repeat forever with no surplus resource, e.g.
      `Palinchron + Molten Echoes`) are rejected, because the same "a resource
      must grow" rule is what removes the false positives.
- [ ] **Staging gaps.** Combos needing a supporting object the scenario does not
      provide stall, e.g. `Ghired, Mirror of the Wilds` needs a token that
      "entered this turn" (22 known pairs).
- [ ] Build identity: an in-place ontology rebuild appends a second build, so the
      append-only store can hold duplicate rows that views show.
- [ ] `scripts/known_recall.py` and `scripts/analogue_transfer.py` run one game
      at a time (the harness hosts one active game per process).
- [ ] Carried over from the protocol work: `state_hash` still omits library/hand/
      graveyard order and some scenario fields; combat-damage split validation is
      policy-side only; non-priority snapshot restores fall back to the abort
      NO-OP; `FullState` remains a partial inspection state.

## Next Work

1. Public artifact: write-up + this status + tagged release.
2. Recall: net-neutral-loop handling and Ghired-style staging (both improve the
   verifier's reach without reintroducing false positives).
3. Candidate yield: new-card focus (recent sets, where Spellbook lags) and more
   free/activated engine archetypes.
4. Longer term: a goal-directed search player (the only route to genuinely new
   mechanics) — see `docs/WITNESS_SEARCH.md`.
