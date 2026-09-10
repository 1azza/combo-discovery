.PHONY: stubs test smoke check lint

stubs:
	bash scripts/gen_stubs.sh

test:
	uv run pytest

smoke:
	uv run python scripts/run_smoke.py

check:
	bash scripts/check.sh
