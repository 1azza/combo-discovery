"""Configuration defaults for combo-discovery."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    port: int = 50051
    base_port: int = 50060
    n_workers: int = 8
    max_turns: int = 0
    timeout_seconds: int = 0


DEFAULT_CONFIG = Config()
