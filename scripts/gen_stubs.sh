#!/usr/bin/env bash
# Regenerate gRPC stubs from proto/forge_env.proto into src/combo_discovery/generated.
# Compiles the canonical proto directly (v3 combat answers use the
# AttackerList/BlockerList/DamageList wrapper messages, valid proto3).
set -euo pipefail
cd "$(dirname "$0")/.."

uv run python -m grpc_tools.protoc \
  -I proto \
  --python_out=src/combo_discovery/generated \
  --pyi_out=src/combo_discovery/generated \
  --grpc_python_out=src/combo_discovery/generated \
  proto/forge_env.proto

# grpcio-tools emits top-level imports; make them package-relative.
sed -i 's/^import forge_env_pb2 as forge__env__pb2$/try:\n    from . import forge_env_pb2 as forge__env__pb2\nexcept ImportError:\n    import forge_env_pb2 as forge__env__pb2/' \
  src/combo_discovery/generated/forge_env_pb2_grpc.py
touch src/combo_discovery/generated/__init__.py
echo "stubs regenerated (v3, canonical proto)"
