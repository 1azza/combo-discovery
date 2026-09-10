"""Top-level research configuration (research.toml) loader.

The TOML file is the operational source for the model/policy/engine metadata
recorded with each experiment plus the batch defaults. ``load_config`` falls
back to the dataclass defaults when the file is absent, so importing this
module never requires the file to exist.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG_PATH = "research.toml"

# Real values for this repo (recorded in research.toml as well).
DEFAULT_PROJECT = "combo-discovery"
DEFAULT_MODEL = "openrouter/z-ai/glm-5.3-flash"
DEFAULT_POLICY_VERSION = "default-v1"
DEFAULT_ENGINE_COMMIT = "4f577da7b2a9074f9f66544aaf99405e38cf5ac3"
DEFAULT_PROTO_VERSION = 6


@dataclass(frozen=True)
class ResearchConfig:
    project: str = DEFAULT_PROJECT
    model: str = DEFAULT_MODEL
    policy_version: str = DEFAULT_POLICY_VERSION
    engine_commit: str = DEFAULT_ENGINE_COMMIT
    proto_version: int = DEFAULT_PROTO_VERSION
    max_turns: int = 0
    timeout_seconds: int = 0
    n_workers: int = 8
    base_port: int = 50060

    def experiment_meta(self) -> dict:
        """Kwargs for ExperimentStore.start_experiment()."""
        return {
            "engine_commit": self.engine_commit,
            "proto_version": self.proto_version,
            "policy_version": self.policy_version,
            "model_version": self.model,
        }


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> ResearchConfig:
    """Load research.toml, falling back to defaults when it is absent."""
    config_path = Path(path)
    if not config_path.exists():
        return ResearchConfig()
    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    meta = data.get("meta", {})
    engine = data.get("engine", {})
    defaults = data.get("defaults", {})
    return ResearchConfig(
        project=str(meta.get("project", DEFAULT_PROJECT)),
        model=str(meta.get("model", DEFAULT_MODEL)),
        policy_version=str(meta.get("policy_version", DEFAULT_POLICY_VERSION)),
        engine_commit=str(engine.get("forge_commit", DEFAULT_ENGINE_COMMIT)),
        proto_version=int(engine.get("proto_version", DEFAULT_PROTO_VERSION)),
        max_turns=int(defaults.get("max_turns", 0)),
        timeout_seconds=int(defaults.get("timeout_seconds", 0)),
        n_workers=int(defaults.get("n_workers", 8)),
        base_port=int(defaults.get("base_port", 50060)),
    )
