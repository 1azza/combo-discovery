# Engine Signature Harvest — Feasibility & Approach

Answer to: *"can we auto-derive the verb→semantics table (signatures) from
Forge's engine code?"* Investigation verdict, with the fallback plan.

## Verdict

**Full auto-derivation is not possible.** Forge's registries give an index,
not semantics. Reflection yields names and class bindings; everything else
(params, produces/consumes, targets, gates, layered modifies) is either a
string literal read ad hoc in effect classes or expressed in source logic.

## What each registry actually exposes

| Registry | Count (this checkout) | Reflectable | Source-only |
|---|---|---|---|
| `ApiType` → `ability/effects/*.java` | 205 entries / 202 classes | `name()`, verb→class binding | per-verb params, semantics |
| `TriggerType` + `Trigger*` | 148 entries / 140 classes | `name()`, class via constructor | valid params, triggering objects |
| `StaticAbilityMode` | 86 | `name()` only — **no class ref** | mode→class, params |
| `ReplacementType` | 40 | `name()`, package-private class | valid params |
| cost classes | 53 classes / 51 prefixes | `CostPart` amount/type text | prefix→class map (an `if`-chain in `Cost.parseCostPart`) |

- **No param-description tables exist anywhere** (`getParamDesc` = 0 hits).
  Effect params are string literals: `sa.getParam("NumDmg")` etc. — 723
  distinct param names across 169/205 effect classes.
- **No annotations** on effect classes.
- The mutation vocabulary *is* mechanically enumerable: distinct
  `game.getAction().*` calls plus direct `Card`/`Player` mutators
  (`untap`, `tap`, `addCounter`, `drawCards`, `gainLife`, `discard`,
  `setController`, …).

## Accuracy of a source-scan (spot-checked)

Simple verbs (Untap, Tap, Draw, GainLife, LoseLife, Mill, Token, PutCounter,
Discard, Sacrifice, ChangeZone, DealDamage, AddPhase) reach roughly **60–80%
recall on `produces`**. Failure modes: delegation (Mana→`AbilityManaPart`,
Token/Copy→`TokenEffectBase`, ChangeZone→`GameAction`), conditionals (params
select different mutations), layers/continuous effects (Pump/Animate), charms
and sub-abilities (the real action is in a child SA), and replacement/
prevention rewriting events afterwards. `targets`, `gates`, and layered
`modifies` are where both reflection and naive source-scan break.

## Recommended approach (cost-ordered)

1. **Reflective index** (trivial): enum → class, verb/mode/trigger names, coverage.
2. **Param-literal scan** (low): regex `getParam|hasParam` per class → `params[]`.
3. **Docs mining** (low, ~30% coverage): `docs/Card-scripting-API/*.md` prose.
4. **Curate the top ~40 verbs** (1–2 weeks): covers most of the combo corpus;
   leave the tail auto-seeded and explicitly low-confidence.
5. **Validate against the engine** (1–2 weeks): run a scripted ability under a
   scenario and diff the observed event stream (`GameEvent*` already consumed
   by the harness) against the declared signature.

Emit `signatures.{yaml,json}` keyed by verb with:
`verb, api_class, trigger_modes[], params[], produces[], consumes[],
targets{}, triggers_on[], modifies[], gates[], provenance[], confidence`.
Build-time generator, checked in; not a runtime dependency.

**Effort:** seed generator + enum/param harvest 2–4 days; curated top-40
1–2 weeks; engine oracle 1–2 weeks; full 205-verb fidelity is an incremental
1–2 months (complex/layered verbs dominate the tail).

## Why this is not the current priority

The motif-enrichment analysis (`analysis/enrichment.py`) shows our **existing
predicates are already strongly predictive** of known combos (the copy/untap
loop family: lift 328 for the 4-token motif, 18–1094 for cross motifs,
p≈0; `RECURS_FROM_GRAVEYARD~SACRIFICE_OUTLET` lift 2.05). Our false positives
are *enriched*, not anti-enriched — i.e. the signals are right and the
**composition rules are too permissive**, not the vocabulary.

So the leverage is in **composing edges correctly** (link types + cycle
detection + specificity gates), not in re-deriving the vocab for all 205
verbs. Engine signature harvest stays available as a *targeted* enhancement
for the handful of verbs where precise typing is needed for composition
(e.g. `Untap`/`CopyPermanent` target restrictions), rather than a blanket
prerequisite.
