# Cross-Language Contracts

This directory holds the file-format contracts that both the Python
package under `python/` and any future Rust crates under `rust/` must
agree on. The contracts are the only cross-language seam: per
`docs/architecture.md`, no Python code is imported by Rust and vice
versa.

## Layout

- `schemas/` — JSON Schema documents describing every TOML, JSON, and
  Parquet artifact that crosses the language boundary. Schemas are the
  source of truth; if a schema and a loader disagree, the schema wins.
- `examples/<bundle-name>/` — concrete examples of a complete artifact
  bundle. These exist for human reference, smoke tests, and parity
  fixtures. Each example bundle includes a `manifest.toml`,
  `strategy_spec.toml`, `sleeve_spec.toml`, and a `feature_schema.json`.
- `parity/<bundle-name>/` — small golden datasets used by the CI parity
  job. The Python and Rust loaders must produce identical outputs for
  every row in `parity_cases.parquet` within documented tolerances.
- `CHANGELOG.md` — every schema change is recorded here with the
  schema_version it lands in.

## Consumers

- **Python.** Loaders in `python/src/eventcontracts/config.py` validate
  loaded TOML against the corresponding schema. Future model and
  artifact loaders validate at the bundle boundary.
- **Rust.** Future crates under `rust/crates/` include these schemas at
  build time (`include_str!`) and validate inputs identically.

## Versioning Rule

Every artifact carries a `schema_version` field. Breaking changes
require a new version and an entry in `CHANGELOG.md`. Non-breaking
additions (new optional fields) can land within the same version.
