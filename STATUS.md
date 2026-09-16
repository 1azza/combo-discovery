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

### Search player (C)

- [x] A goal-directed search lives in `src/combo_discovery/search.py`: bounded DFS
      over PRIORITY decisions with snapshot/restore backtracking, replay-based
      branching for non-priority decisions, and a verdict-directed mode that aims
      at `detect_loop`'s verdict. Measured honestly: it changed no verdicts on the
      known-combo slice — the limit is candidate quality and staging, not search.

### Tests

- [x] Python: `546 passed, 1 skipped` (`uv run pytest -q`).
- [x] Java conformance tests + Maven package succeed.
- [x] `scripts/check.sh` (stub regen + pytest + optional Java compile) passes.

## Known Gaps

- [ ] **Recall ceiling is the candidate set / staging, not search.** The
      scripted policy is trigger-blind (triggered-engine recall 9%), and a
      goal-directed search player was built and measured — it changed no
      verdicts, because those candidates do not loop on the injected board or
      need conditions/objects the scenario does not provide.
- [ ] **Search player (C) is built but pays nothing yet.** `search_for_repeat`
      (bounded DFS over PRIORITY with snapshot/restore), `search_then_verify`,
      `witness_with_search` (+ `known_recall --search`), `search_by_replay`
      (hybrid replay branching for non-priority decisions) and `search_for_loops`
      (verdict-directed) all live in `src/combo_discovery/search.py`. Measured:
      the fallback changed 0 verdicts; replay branching flipped 0/7 triggered
      `inconclusive` pairs; verdict-directed search found 0/7 loops. The harness
      cannot rewind non-priority decisions (priority has an "ask me again"
      contract, the others do not), which is why branching is replay-based.
- [ ] **Net-neutral loops** (repeat forever with no surplus resource, e.g.
      `Palinchron + Molten Echoes`) are rejected, because the same "a resource
      must grow" rule is what removes the false positives.
- [ ] **Staging gaps.** Combos needing a supporting object the scenario does not
      provide stall: `Ghired, Mirror of the Wilds` needs a token that "entered
      this turn" (22 known pairs); `Fear of Missing Out` needs delirium (four card
      types in the graveyard); `Mirage Phalanx` needs soulbond pairing.
- [ ] Build identity: an in-place ontology rebuild appends a second build, so the
      append-only store can hold duplicate rows that views show.
- [ ] `scripts/known_recall.py` and `scripts/analogue_transfer.py` run one game
      at a time (the harness hosts one active game per process).
- [ ] Carried over from the protocol work: `state_hash` still omits library/hand/
      graveyard order and some scenario fields; combat-damage split validation is
      policy-side only; non-priority snapshot restores fall back to the abort
      NO-OP; `FullState` remains a partial inspection state.

## Next Work

1. **Staging**: supply required supporting objects — a varied graveyard
   (delirium), soulbond pairing, the Ghired token — bounded, and it directly
   changes verdicts.
2. **Candidate quality**: new-card focus (recent sets, where Spellbook lags) and
   more free/activated engine archetypes. The measurements say candidate quality,
   not search power, is the limit.
3. **Search correctness**: `SequentialPolicy` replays positionally, so after a
   variant diverges a recorded answer can land on the wrong decision type
   (`error`, ranked weakest); matching answers by decision type is a small fix.
4. Net-neutral-loop handling (a loop that repeats with no surplus) — rejected
   today by the same rule that removes the false positives.
