.PHONY: quality python-quality test lint typecheck compile rust-check

PYTHON ?= python3

quality: python-quality rust-check

python-quality: compile test lint typecheck

compile:
	cd python && $(PYTHON) -m compileall -q src tests

test:
	cd python && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 $(PYTHON) -m pytest

lint:
	cd python && $(PYTHON) -m ruff check src tests

typecheck:
	cd python && $(PYTHON) -m mypy src/eventcontracts tests

rust-check:
	cd rust && cargo check --workspace
