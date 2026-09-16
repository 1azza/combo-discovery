# Finding Magic combos with a real rules engine (and why it's hard)

*Notes from building a combo-discovery pipeline on top of a headless rules oracle.*

## The goal

Magic: The Gathering combos are loops: two or more cards that, together, can be
repeated forever to win. The community has catalogued tens of thousands of them
(Commander Spellbook), but new cards print constantly, and the catalogue always
lags. We wanted a pipeline that could *find* combos — not by chat, but by proving
them inside an actual rules engine, and checking the result against the known
catalogue.

The project has three parts:

1. **An oracle.** [Forge](https://github.com/Card-Forge/forge) is a complete,
   open-source Magic rules engine. We run it headless behind a gRPC harness, so a
   Python program can start games, inspect state, inject a starting board, and
   answer decisions. Same seed → byte-identical game, every time.
2. **A generator.** We parse 33,688 Forge card scripts into 84,190 typed effects,
   project them into a predicate ontology, and search for pairs of cards whose
   effects can compose into a loop.
3. **A verifier ("witness search").** For a candidate pair, we set up a board,
   drive the line with a scripted policy, and look for a game state that recurs
   with something growing — i.e. a real loop, not a plausible story.

Ground truth is Commander Spellbook: 107,325 Vintage-legal variants, 504,709
pairs (3,937 of them exact two-card combos). It is used **only to measure** — and
to decide whether something is already known.

## The part that was actually hard: telling a loop from a story

The generator is easy to make *plausible*. Proving it is not. Early on, the
verifier confidently called things "infinite" that weren't. We found and fixed
**five** distinct false-positive classes, each reproduced live on the engine:

| # | Bug | The tell |
|---|---|---|
| 1 | the pre-iteration **baseline pair** was compared to itself | observations 0 and 1 were bit-identical because *nothing had happened yet* |
| 2 | a **cross-turn recurrence** counted as a loop | a creature that untaps each turn looked "repeating" — normal play, advances the turn every pass |
| 3 | growth in the **policy's own counters** counted as a loop | a static board the pilot kept poking grew `casts`, not resources |
| 4 | **life totals** were part of the "structure" | a combat loop damages the opponent every pass, so the structure never recurred and a real loop (`Combat Celebrant + Kiki-Jiki`) was rejected |
| 5 | a **mana-consuming recurrence** counted as infinite | the copy ability costs mana, the untapper untaps the engine, not the lands — the loop is bounded by the pool (`Orthion, Hero of Lavabrink`) |

A sixth bug lived in the **harness**, not the judge: the FullState reader reported
colorless mana under the wrong key, so eight injected mana were invisible. The
engine's AI prefers to spend invisible mana on generic costs, which made a `{1}`
activation look free — a bounded burst masqueraded as an infinite loop.

Each fix came with a regression matrix. The matrix is the point: any change to
loop detection must keep Kiki + Pestermite `loops`, keep Keldon Overseer and
Elven Raft-Steerer *not* `loops`, and keep the control pair quiet. Six fixes
later, a `loops` verdict is worth something.

## So what did we find?

**No genuinely new combos.** That is the honest result, and it is interesting.

We did find **uncatalogued pairs**. The best: `Splinter Twin + Giant-Sized Flying
Ant` — legal, engine-verified, and absent from Commander Spellbook. The card is
from a 2026 set, exactly where the catalogue lags. But it is a **database gap,
not a new mechanic**: the identical interaction is already catalogued as
`Kiki-Jiki + Giant-Sized Flying Ant`.

The near-misses are equally instructive:

- `Kiki-Jiki + Reptilian Recruiter` is a real loop missing from Spellbook — and
  was called out on Reddit and MTG Salvation the day the card was previewed in
  2024.
- `Splinter Twin + Janjeet Sentry` is a genuine, subtle energy loop — and has
  been listed on TappedOut since 2018.

Our own rule — **never call something new unless two independent sources agree**
— refused all three. That rule did more for the result than any cleverness.

## The real bottleneck: the pilot

The verifier only acts when the engine offers it a decision. That works
beautifully for one family of combos — tap this, choose a target, untap that
(Kiki-Jiki, Splinter Twin) — and fails for everything else. If a combo's key step
is an *automatic* trigger (`when this attacks`, `on your upkeep`), there is no
menu option to click, and the pilot sits still.

We measured this rather than guessing. Running the verifier against **known**
combos (`scripts/known_recall.py`), over 133 exact two-card copy/untap combos:

| engine type | n | confirmed | recall |
|---|---|---|---|
| activated (`{T}`-cost copy) | 99 | 46 | **46%** |
| triggered (attack/ETB/loyalty) | 34 | 1 | **3%** |
| all | 133 | 47 | **35%** |

So even on its home turf the verifier confirms under half of combos we *know* are
real, and it is nearly blind to triggered engines. And that biases discovery:
the combos the pilot *can* play are the textbook shapes — the ones the community
figured out years ago. The unknown ones are more likely to be unusual shapes, so
the current architecture is pointed away from the frontier.

That is why the honest next step is teaching the pilot to *play* — advance turns,
attack, order the stack, search for a line — rather than adding more candidates.
A bigger pile of untestable candidates doesn't help.

## What the result means

A first-generation symbolic generator plus a rules-accurate verifier reliably
finds **database gaps** — real combos nobody has written down in the catalogue —
but not **new mechanics**. And the two-source rule means most "finds" evaporate
on contact with the community. The pipeline's real value so far is the
*negative* result: it can now tell you, with evidence, that a candidate is not a
loop, and exactly which family of false positives it is dodging.

## Reproduce

```sh
# build + boot the oracle (from the Forge fork)
~/tools/apache-maven-3.9.11/bin/mvn -pl forge-harness -am package -DskipTests
cd forge-gui-desktop && java -Djava.awt.headless=true \
  -jar ../forge-harness/target/forge-harness-2.0.15-SNAPSHOT.jar --port 50051

# measure verifier recall on known combos
uv run python scripts/known_recall.py --mode home

# probe uncatalogued functional analogues of known combo partners
uv run python scripts/analogue_transfer.py

# the full test suite
uv run pytest -q      # 546 passed, 1 skipped
```

## Limitations

- **Recall is the player**, not the judge: triggered engines and any line needing
  combat/phase navigation are out of reach.
- **Net-neutral loops** (repeat forever with no surplus, e.g.
  `Palinchron + Molten Echoes`) are rejected, because the same "a resource must
  grow" rule is what removes the false positives.
- **Staging gaps**: combos needing a supporting object the scenario doesn't
  provide stall (`Ghired, Mirror of the Wilds` needs a token that "entered this
  turn").
- One game at a time per harness process, so sweeps are serial.
- Novelty is only as good as the two sources; a combo can be real and simply
  undocumented anywhere we can search.

## The search player, and what it taught us

We then built the "goal-directed pilot": a bounded search over legal decisions
that branches with snapshot/restore, replays a candidate sequence through the
verifier, and can aim directly at the judge's verdict. It works — positive
controls still return `loops` — but measured over the known-combo slice it
**changed no verdicts**:

- the search fallback flipped **0** verdicts;
- replay-branching (to work around the harness's inability to rewind combat)
  flipped **0 of 7** previously undecided pairs;
- aiming the search straight at `loops` found **0 of 7**.

The honest conclusion is not "search doesn't work" but **"search is not the
bottleneck."** Those candidates do not loop on the injected board, or they need
conditions the scenario does not provide (delirium wants a graveyard; soulbond
wants pairing). The blocked recall comes from *what we ask the verifier to test*
and *what the board provides* — not from search power.

## Future work

1. **Staging** — supply the required supporting objects (a varied graveyard for
   delirium, soulbond pairing, a token that "entered this turn"). Bounded, and it
   directly changes verdicts.
2. **Candidate quality** — new-card focus and more drivable engine archetypes.
   The measurements say this, not search, is the limit.
3. **Search correctness** — replay currently matches answers positionally; match
   them by decision type instead.
4. **Net-neutral loops** — a loop that repeats with no surplus resource is
   rejected today by the same rule that removes the false positives.
