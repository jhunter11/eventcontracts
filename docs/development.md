# Development Guide

This guide covers local setup, verification, and working conventions for the
Python scaffold.

## Environment

Create a virtual environment and install runtime dependencies from
`requirements.txt`.

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
```

Install development tooling when running tests, lint, or type checks:

```bash
python3 -m pip install -r requirements-dev.txt
```

`requirements-dev.txt` includes `requirements.txt`, so it is enough for a full
local development environment.

## Editable Install

The tests use `pythonpath = ["src"]` from `pyproject.toml`, so an editable
install is not required for pytest. Install the package editable when you want
the `eventcontracts` console script available:

```bash
python3 -m pip install -e .
```

## Environment Variables

Start from the example file:

```bash
cp .env.example .env
```

The example includes placeholders for Kalshi, Polymarket global, and external
data providers. Do not commit real credentials.

## Verification Commands

```bash
python3 -m compileall -q src tests
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest
python3 -m ruff check src tests
python3 -m mypy src/eventcontracts tests
```

The current test suite is intentionally small and focuses on import stability,
event/decision type coverage, and the strategy/runner smoke path.

## CLI

After editable install:

```bash
eventcontracts check-config configs/venues/kalshi.toml
```

Without editable install:

```bash
PYTHONPATH=src python3 -m eventcontracts.cli check-config configs/venues/kalshi.toml
```

## Adding A Strategy

1. Add a module under `src/eventcontracts/strategies/`.
2. Implement `StrategyBase.on_event`.
3. Register a factory with `@register("strategy_name")`.
4. Import the module in `src/eventcontracts/strategies/__init__.py` so the
   registry is populated.
5. Add a test using the in-memory runner ports.

## Adding A Domain Type

Domain types should be immutable dataclasses where possible. Prefer adding a
new event or decision variant only when the behavior is genuinely cross-cutting.
Use metadata fields for venue-specific extra fields until the framework has a
clear venue-neutral meaning for them.

## Documentation Updates

When changing the strategy boundary, update:

- `README.md`
- `docs/architecture.md`
- `docs/strategy-runner-contract.md`
- tests that show the expected wiring

When changing artifact or model export assumptions, update:

- `docs/artifact-contract.md`
- `docs/implementation-roadmap.md`
