# Project Status

Last verified on the live harness and full test suite, 2026-09-12.

The pipeline is **end-to-end and sound**: a headless rules oracle (Forge behind a
gRPC harness), a card-effect corpus + interaction algebra, Commander Spellbook
ground truth, and a combo tester that plays candidate combos in the real
engine. The Combo Tester and Loop Detector have been hardened through six
concrete correctness fixes (five loop-detection false-positive classes plus one
harness mana-read bug), each reproduced live and guarded by a regression matrix.

Current focus: **trustworthy recall + candidate yield**. Recall on known combos
is now measured (`scripts/known_recall.py`), and candidate novelty is probed by
analogue transfer (`scripts/analogue_transfer.py`).

## Vocabulary

Prose uses plain, functional names. The system was previously described with
metaphors; this table is the canonical mapping.

| old name | new name | what it is |
|---|---|---|
| the Table | **Rules Engine** | the thing running the game (Forge behind the gRPC harness) |
| the Scripts / card scripts | **Card Scripts** | the parsed Forge card-effect scripts |
| the Index | **Ability Index** | the corpus of typed card effects and predicates |
| the Map | **Interaction Graph** | the producer/consumer interaction edges between cards |
| the Prospector | **Candidate Finder** | generates candidate pairings |
| the Book | **Known Combos** | ground-truth combos (source: Commander Spellbook) |
| the Pilot | **Combo Player** | the policy that plays a combo line out |
| the Witness (plays a pairing out) | **Combo Tester** | drives a candidate pairing through the engine |
| the Witness (decides whether it looped) | **Loop Detector** | judges recurrence + resource growth into a verdict |
| observations / board samples | **Board Samples** | per-step snapshots of the board |
| narration / events / the replay | **Game Log** | the recorded event stream for a Test Run |
| a run / witness run | **Test Run** | one execution of a candidate pairing |
| the Gauntlet | **Known-Combo Check** | checks a candidate against the known combos |
| The Gallery (as a screen) | **Combos** | the user-facing list of pairings being checked |
| The Goldfish | **The Goldfish** | kept — `goldfish` is real MTG vocabulary; one execution is a **goldfish run** |

### Deliberate name mismatches

The rename is documentation-only. These identifiers did **not** change, so prose
must be mapped back to code by hand:

- **DB tables:** `witness_runs` = Test Runs; `witness_results` = Test Results;
  `witness_events` = Game Log; `witness_observations` = Board Samples;
  `combo_hypotheses` = candidate pairings; `known_combos` = Known Combos;
  `interactions` = Interaction Graph; `card_effects` / `card_predicates` =
  Ability Index.
- **CLI:** `combo-witness` (unchanged).
- **Package:** `combo_discovery.witness` (unchanged).
- **Web routes:** `/goldfish` and `/run/{id}` (unchanged).

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
- [x] PRIORITY decisions offer a synthetic `kind="pass"` option, so a client can
      deliberately pass priority. The Combo Tester's spin guard uses it to advance
      past a no-op action — this is what lets `Fear of Missing Out + Helm of the
      Host` reach combat within the default Board Sample budget.

### Science layer

- [x] Corpus: 33,688 Forge Card Scripts parsed, 0 errors, 84,190 typed effects.
- [x] Predicate ontology + **interaction algebra** (ports / links / cycles /
      queries / budgets); 12 patterns. Fresh algebra pool after precision gates:
      **3,472 candidate pairings**, no illegal (Un-set/novelty) cards, no triggered
      copy engines.
- [x] Ground truth: Commander Spellbook — 107,325 Vintage-legal variants,
      504,709 pairs (3,937 exact 2-card), 33,636 Scryfall oracle-id links.
- [x] Evaluation: `known_pair` / `contained_in_known` / `unmatched` / `missed`
      classification, precision@k / recall / F1, FP/miss diagnostics, Tier-A/B
      two-source novelty policy (never claims "novel" from one source).
- [x] Store schema v7, append-only (WAL, no UPDATE/DELETE).
- [x] TUI (omarchy-styled): Corpus, Experiments, Candidates, Card Lab, Activity.
- [x] **Web console** (`uv run combo-web`): a local, read-only, stdlib-only
      server — a live feed of Test Runs and a Test Run page whose star is a
      per-step **state graph** drawn from `witness_observations`, where a repeated
      board is drawn as a highlighted back-edge coloured by verdict. Wording is
      plain English for Magic players; internal detail lives in a collapsed
      "Technical details" section. Store v7 streams per-step Board Samples,
      evidence and diagnostics live.

### Combo Tester and Loop Detector

- [x] Scenario-driven combo testing: inject a board, drive decisions with a
      choice-aware policy, detect structural recurrence + resource growth.
- [x] **Seven false-positive / structural classes found and fixed** (each
      live-reproduced, each guarded by the acceptance matrix below):
      1. the pre-iteration baseline pair certified a loop;
      2. cross-turn recurrence treated as a loop;
      3. counter-only (policy-driven) growth treated as a loop;
      4. life totals in the structural signature broke combat loops;
      5. mana-consuming recurrences certified as infinite (bounded by the pool);
      6. monotonic zone counts (`graveyard`/`library`/`hand`) sat in the
         structural signature, so loops whose discard/draw grows the graveyard
         never recurred (`Fear of Missing Out + Helm of the Host`); they are now
         tracked resources, with the durable-resource requirement kept;
      7. a bit-identical consecutive state certified a loop with no requirement
         that anything happened between the samples — a **stall**, not a loop
         (`The Fire Crystal + Captain of the Mists`); the degenerate branch now
         requires durable growth, so genuine loops are certified by the
         recurrence branch instead.
- [x] Automatic abilities/triggers are credited to their link (the harness
      already broadcasts every stack push as `SpellCast`), and Board Samples are
      taken per trigger and at phase/turn boundaries, with a sticky duplicate
      guard against the degenerate branch.
- [x] Acceptance matrix (all live on the harness): Kiki + {Deceiver Exarch,
      Pestermite, Reptilian Recruiter, Bounding Krasis, Breaching Hippocamp,
      Zealous Conscripts, Village Bell-Ringer, Sky Hussar, Hyrax Tower Scout,
      Glamermite, Great Oak Guardian} → `loops`; Splinter Twin + Deceiver Exarch
      → `loops`; Combat Celebrant + Kiki → `loops`; Keldon Overseer / Elven
      Raft-Steerer / Firbolg Flutist → not loops; Aurelia + Genji Glove →
      `inconclusive`.
- [x] **Measured recall on known combos** (`scripts/known_recall.py`): over 133
      known exact 2-card copy/untap combos — **46% overall**, **55% on
      activated-engine combos**, **21% on triggered-engine combos** (up from
      35% / 46% / 3% at v0.2.0). Undecided Test Runs fell from 51 to 14.

### Candidate yield

- [x] Generator precision gates: recursion/flicker origin, one-shot (instant/
      sorcery) engines, activated/static copy engines only.
- [x] Analogue transfer (`scripts/analogue_transfer.py`): functional ETB-untap
      analogues of known partners. 49 uncatalogued pairs → 4 loops → after
      independent triage **1 uncatalogued pair survives**
      (`Splinter Twin + Giant-Sized Flying Ant`, a 2026-set card).
- [x] Recent-engine sweeps (`scripts/recent_engines.py`): the copy class (3 recent
      engines) and the tap-cost class (101 engines) crossed with untappers, on an
      interleaved fair sample — **0 loops in both**, 60 Test Runs. The sweeps also
      exposed and fixed three generator leaks (mana lands, mana rocks with riders,
      and partner-outer sampling that tested one partner 30 times).
- [x] Honest result: **no genuinely novel mechanic has been found.** Every
      near-miss was either documented elsewhere (Reddit / TappedOut / MTG
      Salvation) or exposed as a Combo Tester bug.

### Search player (C)

- [x] A goal-directed search lives in `src/combo_discovery/search.py`: bounded DFS
      over PRIORITY decisions with snapshot/restore backtracking, replay-based
      branching for non-priority decisions, and a verdict-directed mode that aims
      at `detect_loop`'s verdict. Measured honestly: search itself changed no
      verdicts. The recall gains that did land came from the Combo Tester side —
      trigger attribution, per-trigger and phase-boundary sampling, the zone-count
      resource split, and the PRIORITY pass option — not from search power.

### Tests

- [x] Python: `585 passed, 1 skipped` (`uv run pytest -q`).
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
