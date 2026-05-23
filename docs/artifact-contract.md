# Artifact Contract

This is the planned bundle format for shipping a trained model and strategy
configuration from research into replay, paper, and eventually live runners.
The format is not fully implemented yet; this document defines the target
contract so exporters, loaders, CI, and Rust parity work can converge.

## Bundle Goals

An artifact bundle should answer five questions:

1. What strategy is this?
2. What model and feature schema does it use?
3. What exact files were exported?
4. What replay cases prove Python/Rust parity?
5. What immutable version should a sleeve run?

## Directory Layout

```text
artifacts/
  strategies/
    weather_threshold/
      2026-05-23T120000Z_abcd1234/
        manifest.toml
        strategy_spec.toml
        feature_schema.json
        parity_cases.parquet
        model.onnx
        README.md
```

`model.onnx` is optional for rule-only strategies. `README.md` is optional but
recommended for human notes that do not affect execution.

## manifest.toml

The manifest is the immutable bundle index. It should be signed or otherwise
integrity-checked before a runner accepts it.

```toml
schema_version = "1"
bundle_id = "weather_threshold/2026-05-23T120000Z_abcd1234"
created_at = "2026-05-23T12:00:00Z"
created_by = "research"

[strategy]
name = "weather_threshold"
version = "0.1.0"
strategy_spec = "strategy_spec.toml"

[model]
name = "weather_threshold"
version = "v3"
format = "onnx"
path = "model.onnx"
sha256 = "REPLACE_WITH_SHA256"
optional = false

[features]
schema = "feature_schema.json"
schema_id = "weather_threshold_features"
schema_version = "1"
sha256 = "REPLACE_WITH_SHA256"

[parity]
cases = "parity_cases.parquet"
sha256 = "REPLACE_WITH_SHA256"
expected_rows = 1000

[[files]]
path = "manifest.toml"
sha256 = "REPLACE_WITH_SHA256"

[[files]]
path = "strategy_spec.toml"
sha256 = "REPLACE_WITH_SHA256"

[[files]]
path = "feature_schema.json"
sha256 = "REPLACE_WITH_SHA256"

[[files]]
path = "parity_cases.parquet"
sha256 = "REPLACE_WITH_SHA256"

[[files]]
path = "model.onnx"
sha256 = "REPLACE_WITH_SHA256"
```

## strategy_spec.toml

This file maps onto `eventcontracts.domain.spec.StrategySpec`.

```toml
strategy_id = "weather-threshold-v3"
name = "weather_threshold"
version = "0.1.0"
description = "Weather threshold strategy for Kalshi daily high temperature markets."
feature_schema_id = "weather_threshold_features"

[subscription]
venues = ["kalshi"]
instrument_patterns = ["KXHIGH*"]
event_kinds = ["quote", "trade", "book", "external", "timer", "lifecycle"]
external_sources = ["nws", "metar"]

[parameters]
buy_below = 0.42
sell_above = 0.68
max_contracts = 25
min_edge_bps = 150

[execution_priority]
tier = "fast"
max_delay_ms = 100
expires_after_ms = 250
allow_rate_limit_borrow = true
reason = "Weather threshold repricing near observation release."

[model]
name = "weather_threshold"
version = "v3"
artifact_uri = "s3://eventcontracts/artifacts/strategies/weather_threshold/2026-05-23T120000Z_abcd1234/model.onnx"
sha256 = "REPLACE_WITH_SHA256"

[tags]
family = "weather"
venue = "kalshi"
```

Sleeve deployment config should be separate from strategy config. A strategy
describes logic. A sleeve describes venue, capital allocation, and risk limits.
The strategy's execution priority is a default; individual decisions may still
override it when a specific signal is more or less latency-sensitive.

## sleeve_spec.toml

This file maps onto `eventcontracts.domain.spec.SleeveSpec` and is environment
specific.

```toml
sleeve_id = "weather-kalshi-paper-a"
strategy_id = "weather-threshold-v3"
strategy_version = "0.1.0"
venue = "kalshi"
capital_allocation = "10000"
currency = "USD"

[risk]
max_order_notional = "250"
max_position_notional = "2000"
max_daily_loss = "500"
max_open_orders = 20
max_gross_exposure = "3000"
currency = "USD"

[tags]
mode = "paper"
family = "weather"
```

## feature_schema.json

The feature schema is the Python/Rust parity contract. It fixes order, dtype,
null handling, and meaning.

```json
{
  "schema_id": "weather_threshold_features",
  "schema_version": "1",
  "created_at": "2026-05-23T12:00:00Z",
  "features": [
    {
      "name": "latest_observed_temp_f",
      "dtype": "float32",
      "nullable": false,
      "default": 0.0,
      "description": "Latest point-in-time observed temperature in Fahrenheit."
    },
    {
      "name": "threshold_distance_f",
      "dtype": "float32",
      "nullable": false,
      "default": 0.0,
      "description": "Observed or forecast distance to contract threshold."
    },
    {
      "name": "minutes_to_close",
      "dtype": "float32",
      "nullable": false,
      "default": 0.0,
      "description": "Minutes from feature timestamp to market close."
    }
  ],
  "target": {
    "name": "next_mid_change_bps",
    "horizon_seconds": 300
  }
}
```

Feature values in `FeatureVector.values` must follow this order exactly when
building model inputs.

## parity_cases.parquet

Parity cases should be generated from replay windows, not fabricated by hand.
Each row should contain enough information for Python and Rust to produce the
same feature vector, prediction, decision, and paper-execution result.

Recommended columns:

- `case_id`
- `event_id`
- `event_kind`
- `event_payload_json`
- `context_snapshot_json`
- `feature_values_json`
- `expected_prediction_json`
- `expected_decisions_json`
- `expected_reject_reasons_json`
- `expected_pnl_delta`
- `tolerance_bps`

CI should fail if:

- feature values differ beyond tolerance
- model outputs differ beyond tolerance
- decision kinds differ
- order intent price, size, side, or instrument differ
- paper PnL differs beyond tolerance

## Object Store Layout

Use content-addressed or immutable version directories. Do not mutate a bundle
after publication.

```text
s3://eventcontracts/
  artifacts/
    strategies/{strategy_name}/{bundle_version}/...
  feature-schemas/{schema_id}/{schema_version}/feature_schema.json
  manifests/{bundle_id}.toml
```

Local development can mirror this layout under `./artifacts/`.

## Promotion Rules

Suggested states:

- `candidate`: exported from research.
- `validated`: parity and replay checks passed.
- `paper`: approved for paper sleeves.
- `champion`: current production candidate for a family.
- `rejected`: failed validation or retired.

Promotion should be metadata outside the immutable bundle, for example in a
registry database or object-store pointer. The bundle contents should not change
when promotion status changes.

## Loader Rules

A runner should reject a bundle if:

- required files are missing
- checksums do not match
- manifest schema version is unsupported
- strategy name is not registered
- feature schema id does not match the strategy spec
- model file is required but missing
- parity metadata is absent for a non-experimental sleeve

## Near-Term Implementation Tasks

1. Add dataclass loaders for `StrategySpec` and `SleeveSpec` TOML.
2. Add `FeatureSchema` dataclasses and JSON validation.
3. Add bundle writer that computes SHA-256 checksums.
4. Add bundle loader that validates checksums and registered strategy names.
5. Add parity case writer from replay windows.
6. Add CI job to load each bundle and run parity cases.
