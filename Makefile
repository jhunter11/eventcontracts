.PHONY: quality python-quality test lint typecheck compile rust-check sports-golf-preflight sports-golf-smoke

PYTHON ?= python3
ifneq ($(findstring /,$(PYTHON)),)
PYTHON_CMD := $(abspath $(PYTHON))
else
PYTHON_CMD := $(PYTHON)
endif

quality: python-quality rust-check

python-quality: compile test lint typecheck

compile:
	cd python && $(PYTHON_CMD) -m compileall -q src tests

test:
	cd python && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 $(PYTHON_CMD) -m pytest

lint:
	cd python && $(PYTHON_CMD) -m ruff check src tests

typecheck:
	cd python && $(PYTHON_CMD) -m mypy src/eventcontracts tests

rust-check:
	cd rust && cargo check --workspace

sports-golf-preflight:
	PYTHONPATH=python/src $(PYTHON_CMD) -m eventcontracts.cli sports-golf-preflight

sports-golf-smoke:
	PYTHONPATH=python/src $(PYTHON_CMD) -m eventcontracts.cli sports-golf-smoke --out data/sports-golf-smoke
