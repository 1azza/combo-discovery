# Ground truth and evaluation

How the discovery pipeline measures its proposals against reality, and the
policy that governs when a candidate may be called *novel*.

## Sources

### Tier A — Commander Spellbook (curated, implemented)

- Bulk (preferred): `https://json.commanderspellbook.com/variants.json.gz`
  (~28 MB gz, ~646 MB decompressed). Stream-parsed with `gzip` + `ijson`;
  never `json.load`.
- REST (spot checks only): `https://backend.commanderspellbook.com/`
  (no auth, camelCase, ~80 req/min, server-side/CORS origin-locked,
  `?count=true` for counts).
- A variant's `uses[]` are concrete cards; `requires[]` are **templates**
  (generic wildcard slots, not cards). Only `status` ∈ {`OK`, `EXAMPLE`} is
  ingested.
- Attribution: **data from Commander Spellbook, https://commanderspellbook.com**.

Ingestion writes the `known_*` tables (schema v4): `known_combos`,
`known_combo_cards`, `known_combo_pairs`, `known_aliases`, plus one
`import_runs` row recording the bulk URL, size, SHA-256, library version and
timestamp. Variants are expanded into all `C(n,2)` card pairs;
`is_full_variant = 1` only for an exact 2-card variant with no templates.

### Tier B — independent deck co-occurrence (interface only, not implemented)

Tier B is **independent** evidence from real decks (planned: Archidekt and
MTGTop8). The tables exist (`observed_decks`, `observed_deck_cards`,
`observed_pairs`) and are intentionally empty this round; the interface is
documented so the novelty gate can require a second source from day one.

EDHREC is **not** an independent source: it is Spellbook-powered.

### What is *not* usable

- Scryfall oracle tags carry no combo tags.
- `cards.scryfall_oracle_id` / `cards.set_code` are blank for the whole Forge
  corpus. The store is append-only, so oracle ids live in the separate
  `card_oracle_ids` table, populated by a Scryfall `oracle_cards` backfill.

## Name normalization

`combo_discovery.corpus.names.normalize_card_name` is byte-compatible with the
corpus importer's `normalize_name`: Unicode NFKD, strip combining marks,
lowercase, `//`/punct -> spaces, collapse whitespace. Spellbook cards resolve to
Forge names by:

1. **oracle id** join via `card_oracle_ids` (preferred; precise for multi-face);
2. else a **front-face** name match for `"A // B"` (Forge stores the front face
   in `cards.name`; back faces live in `card_faces.name`).

Gotchas handled:

- `Birgi, God of Storytelling // Harnfel, Horn of Bounty` -> `birgi god of storytelling`;
- `Fable of the Mirror-Breaker // Reflection of Kiki-Jiki` with `usedFace=2` ->
  front-face fallback `fable of the mirror breaker` (oracle id is the precise path);
- `SP//dr, Piloted by Peni` is single-faced (metadata `faces=1`) and is **not**
  split on the literal `//`;
- accents (`Déjà Dûl's Vault`, `Déjà Vu`) fold correctly;
- `A-*` Alchemy rebalances are excluded.

## Evaluation

`combo_discovery.evaluation` classifies each proposed pair (from
`interactions`, which carry evidence) against Tier A:

| verdict | meaning |
|---|---|
| `known_pair` | exact Spellbook 2-card variant |
| `contained_in_known` | the pair appears inside a larger known variant |
| `unmatched` | absent from Tier A — a *candidate*, not a novelty claim |
| `missed` | a known exact variant we did not propose |

Metrics: `precision = known_pair / proposed` (strict exact-variant definition;
`contained_in_known` reported separately and folded into
`precision_incl_partial`), `recall = known_pair / (known_pair + missed)`, F1,
and precision@k. Reported aggregate, per pattern and per card. Persisted to
`evaluation_runs` / `evaluation_results`.

Diagnostics cluster false positives by the predicate set in their evidence, and
misses by the known combo's `produces` features / `requires` templates, so
systematic rule bugs are visible without hand-inspection.

## Novelty policy — the two-source rule

`novelty_status(pair)` returns exactly one of:

- `known` — present in Tier A (Commander Spellbook);
- `observed` — present in Tier B (independent deck co-occurrence);
- `needs_second_source` — absent from both.

It **never returns `novel`.** A pair that is absent from Tier A but not yet
corroborated by Tier B is only a candidate. Calling anything novel requires
Tier B to be populated and a hit in it; until then, every `needs_second_source`
result is explicitly provisional.

## CLI

- `combo-import-spellbook --db research.db --download [--no-vintage-filter] [--limit N]`
  (also `--variants-file`, `--backfill-oracle`, `--scryfall`).
- `combo-evaluate --db research.db [--card NAME] [--pattern NAME] [--top N] [--json]`.

## Vintage filtering

When enabled, a variant is kept only if its own `legalities.vintage` is true,
every concrete `use` is Vintage-legal, and no use/require has
`mustBeCommander`. Per-card legality prefers the Scryfall `legalities.vintage`
from the oracle-cards cache, else the Forge Vintage format file
(`Sets:`/`Banned:`; restricted cards stay legal). Because local `set_code` is
blank (and Vintage's `Sets:` list is comprehensive), the practical Forge check
is the banned list.
