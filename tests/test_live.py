"""LIVE tests: need a running forge-harness server (protocol v3).

Skipped unless the FORGE_TEST_HARNESS env var is set to "host:port".
Run with: FORGE_TEST_HARNESS=localhost:50051 uv run pytest -m live
"""

import os

import pytest

from combo_discovery.env import ForgeEnvClient, ProtocolMismatchError

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.environ.get("FORGE_TEST_HARNESS"),
        reason="set FORGE_TEST_HARNESS=host:port to run live tests",
    ),
]

TARGET = os.environ.get("FORGE_TEST_HARNESS", "localhost:50051")


def _target() -> tuple[str, int]:
    host, _, port = TARGET.rpartition(":")
    return host or "localhost", int(port)


def test_live_protocol_v4():
    host, port = _target()
    client = ForgeEnvClient(host=host, port=port)
    try:
        pong = client.connect()
        assert pong.protocol_version == 4
    except ProtocolMismatchError:
        pytest.fail("live harness does not speak protocol v4")
    finally:
        client.close()
