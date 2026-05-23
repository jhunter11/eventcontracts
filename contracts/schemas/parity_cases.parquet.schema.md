# `parity_cases.parquet` Column Specification

The CI parity job loads a bundle's `parity_cases.parquet` in both Python
and Rust. For each row, both loaders must produce identical outputs
within the tolerances documented below. Parquet has no native schema
document format, so this Markdown file is the source of truth.

## Required Columns

| Column                | Logical Type            | Description |
|-----------------------|-------------------------|-------------|
| `case_id`             | `string` (UTF8)         | Stable identifier for the case. Required to debug parity failures. |
| `event_kind`          | `string` (UTF8)         | One of the NormalizedEvent kinds (see `normalized_event.schema.json`). |
| `event_payload`       | `string` (UTF8)         | Canonical JSON of the source event, parseable into a NormalizedEvent. |
| `expected_features`   | `string` (UTF8)         | Canonical JSON of the FeatureVector after the feature builder runs. |
| `expected_prediction` | `string` (UTF8)         | Canonical JSON of the model output, or `null` for rule-only strategies. |
| `expected_decisions`  | `string` (UTF8)         | Canonical JSON array of StrategyDecision values. |
| `expected_pnl_delta`  | `decimal(38, 12)`       | Mark-to-mark PnL change attributable to this case after the paper executor closes the bundle. |

## Tolerances

- Feature vectors: bitwise identical for integer and boolean fields;
  `abs(a - b) <= 1e-9` for float32 / float64.
- Predictions: same shape; `abs(a - b) <= 1e-7` per element for
  floating outputs; bitwise identical for categorical outputs.
- Decisions: identical after stripping correlation ids and timestamps,
  which the runner generates.
- PnL delta: `abs(a - b) <= 1e-6` in the bundle currency.

## Failure Mode

CI must fail with a row-level diff: `case_id`, the diverging column,
and both values. A single row mismatch fails the build; there is no
"mostly green" tolerance for total mismatch count.

## File-Level Properties

- Compression: `snappy`.
- Row group size: small (target ~100 rows) — parity bundles are
  tiny on purpose, ~10 to ~1000 rows.
- Schema metadata: optional, but exporters should write the
  contract version in a metadata key
  `eventcontracts.contracts.version`.
