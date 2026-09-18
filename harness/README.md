# Forge harness changes

The rules oracle used by combo-discovery is a gRPC harness added to a clone of
[Forge](https://github.com/Card-Forge/forge). Those changes are kept here as a
patch rather than as a published fork.

- **Base:** `4f577da7b2a9074f9f66544aaf99405e38cf5ac3` (upstream `master` at the time)
- **Patch:** `forge-harness.patch` — 15 commits, `git am`-able
- **Local head when generated:** `fdd5ce75a5`

## What the patch adds

- A `forge-harness` Maven module: a gRPC service (`ForgeEnv`) over a headless game.
- **Protocol v7:** typed decisions, a structured event taxonomy, snapshot/restore
  with deterministic RNG reseed, and `SetupScenario` injection (including
  attached Auras and Equipment via `CardSpec.id` / `attached_to`).
- Remote-player decision surfacing: optional triggers, trigger targets, modal
  ETB targets, and a synthetic `kind="pass"` priority option.
- Fixes found during the work: colorless mana is read from the engine's
  `ManaAtom.COLORLESS` key; scenario card ids/attachments are emitted in the
  `GameState` text so attached permanents can be staged.

## Apply

```sh
git clone https://github.com/Card-Forge/forge.git
cd forge
git checkout 4f577da7b2a9074f9f66544aaf99405e38cf5ac3
git am /path/to/forge-harness.patch

~/tools/apache-maven-3.9.11/bin/mvn -pl forge-harness -am package -DskipTests
```

Launch from `forge-gui-desktop` so asset paths resolve:

```sh
cd forge-gui-desktop
java -Djava.awt.headless=true \
  -jar ../forge-harness/target/forge-harness-2.0.15-SNAPSHOT.jar --port 50051
```

The Python client lives in the parent repository — see the top-level `README.md`,
`STATUS.md` and `docs/WITNESS_SEARCH.md`.
