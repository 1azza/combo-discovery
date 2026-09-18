#!/usr/bin/env bash
# Local pre-flight: regenerate the gRPC stubs, run the unit tests, and — when a
# Forge checkout and Maven are available — do a quick Java compile.
#
# The Java step is best-effort and never a hard requirement: the harness lives
# in a separate, non-public fork. Point FORGE_DIR at it (default: sibling
# ../forge) to include the compile.
#
# The static lint step (`uv run ruff check .`, configured under [tool.ruff] in
# pyproject.toml) runs before pytest and is a hard requirement.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> regenerating gRPC stubs"
bash scripts/gen_stubs.sh

echo "==> ruff lint"
uv run ruff check .

echo "==> python tests"
uv run pytest

FORGE_DIR="${FORGE_DIR:-../forge}"
if command -v mvn >/dev/null 2>&1 && [ -f "$FORGE_DIR/forge-harness/pom.xml" ]; then
  echo "==> java compile ($FORGE_DIR)"
  (cd "$FORGE_DIR" && mvn -q -pl forge-harness -am compile -DskipTests)
else
  echo "==> skipping java compile (mvn not found, or $FORGE_DIR/forge-harness/pom.xml missing)"
fi

echo "==> check complete"
