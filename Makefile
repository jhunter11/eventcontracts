.PHONY: quality python-quality test lint typecheck compile rust-check parity-check rust-bench-check verify-strategy sports-golf-preflight sports-golf-smoke weather-preflight

PYTHON ?= python3
ifneq ($(findstring /,$(PYTHON)),)
PYTHON_CMD := $(abspath $(PYTHON))
else
PYTHON_CMD := $(PYTHON)
endif

ifeq ($(firstword $(MAKECMDGOALS)),verify-strategy)
STRATEGY_NAME := $(or $(NAME),$(word 2,$(MAKECMDGOALS)))
ifneq ($(word 2,$(MAKECMDGOALS)),)
$(word 2,$(MAKECMDGOALS)):
	@:
endif
endif

quality: python-quality rust-check parity-check rust-bench-check

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
	cd rust && cargo fmt --all -- --check
	cd rust && cargo test --workspace
	cd rust && cargo clippy --workspace --all-targets -- -D warnings

parity-check:
	cd rust && cargo run --quiet -p eventcontracts-parity --bin parity_check -- --strategy-spec ../contracts/examples/weather_threshold/strategy_spec.toml --cases ../contracts/parity/weather_threshold
	cd rust && cargo run --quiet -p eventcontracts-parity --bin parity_check -- --strategy-spec ../configs/strategies/example-threshold.toml --cases ../contracts/parity/example_threshold
	cd rust && cargo run --quiet -p eventcontracts-parity --bin parity_check -- --strategy-spec ../configs/strategies/sports-tennis-xgboost.toml --cases ../contracts/parity/sports_tennis_xgboost
	cd rust && cargo run --quiet -p eventcontracts-parity --bin parity_check -- --strategy-spec ../configs/strategies/flu-hospitalization-surge.toml --cases ../contracts/parity/flu_hospitalization_surge
	cd rust && cargo run --quiet -p eventcontracts-parity --bin parity_check -- --strategy-spec ../configs/strategies/crop-drought-yield-reversion.toml --cases ../contracts/parity/crop_drought_yield_reversion

rust-bench-check:
	cd rust && cargo bench --workspace --no-run

verify-strategy:
	@if [ -z "$(STRATEGY_NAME)" ]; then echo "usage: make verify-strategy <name> or NAME=<name>"; exit 2; fi
	PYTHONPATH=python/src $(PYTHON_CMD) -m eventcontracts.cli verify-strategy $(STRATEGY_NAME)

sports-golf-preflight:
	PYTHONPATH=python/src $(PYTHON_CMD) -m eventcontracts.cli sports-golf-preflight

sports-golf-smoke:
	PYTHONPATH=python/src $(PYTHON_CMD) -m eventcontracts.cli sports-golf-smoke --out data/sports-golf-smoke

weather-preflight:
	PYTHONPATH=python/src $(PYTHON_CMD) -m eventcontracts.cli weather-preflight
