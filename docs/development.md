# Development Guide

This guide covers local setup, verification, and working conventions for the
Python scaffold.

## Environment

All Python sources, tests, and requirements live under `python/`. Create a
virtual environment from the repo root and install runtime dependencies from
`python/requirements.txt`.

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r python/requirements.txt
```

Install development tooling when running tests, lint, or type checks:

```bash
python3 -m pip install -r python/requirements-dev.txt
```

`python/requirements-dev.txt` includes `python/requirements.txt`, so it is
enough for a full local development environment.

## Editable Install

The tests use `pythonpath = ["src"]` from `python/pyproject.toml`, so an
editable install is not required for pytest (run from `python/`). Install
the package editable when you want the `eventcontracts` console script
available:

```bash
python3 -m pip install -e ./python
```

## Environment Variables

Start from the example file:

```bash
cp .env.example .env
```

The example includes placeholders for Kalshi, Polymarket global, and external
data providers. Do not commit real credentials. CLI commands auto-load the
nearest `.env`, so you do not need to `source .env` before running local
commands.

For the current sports-golf research path:

```bash
make PYTHON=.venv/bin/python sports-golf-preflight
make PYTHON=.venv/bin/python sports-golf-smoke
```

`sports-golf-preflight` checks that the relevant keys and configs are present
without printing secret values. `sports-golf-smoke` generates deterministic
bar-compatible golf data, writes a Parquet event lake, and runs the player-cut
and cut-line strategies end to end. DataGolf, PGA Tour, and ShotLink keys are
treated as optional provider upgrades; the local smoke path does not require
them.

## Verification Commands

From the repo root, `make quality` runs everything in `python/`. To invoke
the steps directly:

```bash
cd python
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

Without editable install (run from the repo root):

```bash
PYTHONPATH=python/src python3 -m eventcontracts.cli check-config configs/venues/kalshi.toml
```

## Adding A Strategy

1. Add a module under `python/src/eventcontracts/plugins/strategies/`.
2. Implement `StrategyBase.on_event`.
3. Register a factory with `@register("strategy_name")`.
4. Import the module in `python/src/eventcontracts/plugins/strategies/__init__.py`
   so the registry is populated (or expose it via the
   `eventcontracts.strategies` entry-point group in `python/pyproject.toml`).
5. Add a test using the in-memory ports from `eventcontracts.testing`.

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
