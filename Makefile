.PHONY: stubs test smoke lint

stubs:
	bash scripts/gen_stubs.sh

test:
	uv run pytest

smoke:
	uv run python scripts/run_smoke.py
