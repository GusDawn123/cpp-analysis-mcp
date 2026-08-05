.PHONY: lint fmt type test integration cov validate golden all

lint:
	uv run ruff check . && uv run ruff format --check .

fmt:
	uv run ruff format . && uv run ruff check --fix .

type:
	uv run mypy

test:
	uv run pytest -m "not integration"

# needs a real C++ toolchain: it compiles and runs the capability probes
integration:
	uv run pytest -m integration

cov:
	uv run pytest -m "not integration" --cov=cpp_analysis_mcp --cov-report=term-missing

validate:
	uv run python scripts/fixtures.py validate

golden:
	uv run python scripts/fixtures.py capture

all: lint type test
