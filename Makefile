# Developer shortcuts. Everything works with no external services.
.PHONY: help install test test-fast lint format typecheck check demo clean build

help:
	@echo "install    Install the package with dev extras"
	@echo "test       Run the full test suite"
	@echo "test-fast  Run unit tests only (no database)"
	@echo "lint       Run ruff"
	@echo "format     Apply ruff formatting"
	@echo "typecheck  Run mypy"
	@echo "check      lint + typecheck + test"
	@echo "demo       Seed the quickstart warehouse and run its suite"
	@echo "build      Build the sdist and wheel"
	@echo "clean      Remove build and cache artefacts"

install:
	python -m pip install -e ".[dev,duckdb,mcp,server,notify]"

test:
	python -m pytest

test-fast:
	python -m pytest -m "not integration"

lint:
	python -m ruff check src tests
	python -m ruff format --check src tests

format:
	python -m ruff format src tests
	python -m ruff check --fix src tests

typecheck:
	python -m mypy src/nexassure

check: lint typecheck test

demo:
	python examples/quickstart/seed.py
	cd examples/quickstart && nexassure run

build:
	python -m pip install --quiet build
	python -m build

clean:
	rm -rf build dist .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage coverage.xml
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type d -name "*.egg-info" -prune -exec rm -rf {} +
