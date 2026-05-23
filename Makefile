.PHONY: quality test lint typecheck compile

PYTHON ?= python3

quality: compile test lint typecheck

compile:
	$(PYTHON) -m compileall -q src tests

test:
	PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 $(PYTHON) -m pytest

lint:
	$(PYTHON) -m ruff check src tests

typecheck:
	$(PYTHON) -m mypy src/eventcontracts tests
