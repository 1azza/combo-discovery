import sys

from combo_discovery.env import ForgeEnvClient, HarnessConnectionError


def main() -> int:
    client = ForgeEnvClient()
    try:
        pong = client.ping()
    except HarnessConnectionError as e:
        print(f"FAIL: {e}")
        return 1
    print(f"OK: harness version={pong.version} game_active={pong.game_active}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
