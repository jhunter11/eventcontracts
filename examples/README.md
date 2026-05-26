# examples/

Standalone demo scripts that exercise the framework end-to-end with
**synthetic** data. Useful for:

- Proving a strategy's decision logic with assertion-grade output.
- Showing the full pipeline (Parquet → replay → runner → risk → sink)
  works against fixture data before you plug in real Kalshi credentials.
- Giving you a copyable template when you want to test a new strategy
  the same way.

## Ground rules

- **No edits to `python/src/eventcontracts/`.** Every script in here
  only imports the framework's public API. The framework code is
  unchanged.
- Each script is self-contained: generates data, persists it to a
  temporary `ParquetEventStore`, runs the pipeline, prints results,
  exits non-zero on assertion failure. Safe to wire into CI later.
- Run from the repo root with `PYTHONPATH=python/src`.

## Available demos

### `synthetic_queue_evader.py`

Drives `microstructure_queue_evader` (`docs/strategy-specs.md#10`) through
a scenario where it should fire its protective cancel:

1. We have a resting BUY at 0.50 for 100 contracts.
2. Same-price book depth shrinks from 200 → 5 over six snapshots.
3. Three taker trades at 0.50 totalling 90 contracts then arrive.

Configured thresholds (`queue_evacuation_threshold=5`,
`adverse_volume_threshold=50`) mean the strategy should emit exactly
one `CancelOrder(priority=CRITICAL)` once the queue has evacuated and
adverse volume is sufficient. The demo asserts on this and prints the
exact decision (with the heuristic state baked into the reason).

Run:

```bash
PYTHONPATH=python/src .venv/bin/python examples/synthetic_queue_evader.py
```

Expected tail:

```
=== Strategy decisions (captured via direct runner) ===
  cancel_order   1
  no_action      9

CancelOrder decisions: 1  PlaceOrder decisions: 0
  cancel coid=...  reason=queue_evade:ahead_5_recent_vol_60

OK: synthetic queue-evader end-to-end run passed all assertions.
```

## Adding a new demo

Copy `synthetic_queue_evader.py` and change three pieces:

1. **The synthetic stream** (`synthetic_stream()`): what events to feed.
2. **The configs** at the top of the file: which strategy + sleeve TOML to
   load.
3. **The assertions** in `main()`: what the strategy should do.

That's the whole pattern. If your strategy needs features or a model,
follow the wiring in
`python/src/eventcontracts/cli/backtest.py::run_backtest` and pass
`feature_builder=` and/or `model_runner=` when invoking it.
