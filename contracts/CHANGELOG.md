# Contracts Changelog

## v1 — initial set (unreleased)

First cross-language contract bundle. Defines the file formats both
Python and (future) Rust must agree on.

- `schemas/strategy_spec.schema.json` — TOML schema for
  `StrategySpec`-shaped configs. Mirrors
  `StrategySpecConfig` in `python/src/eventcontracts/config.py`.
- `schemas/sleeve_spec.schema.json` — TOML schema for
  `SleeveSpec`-shaped configs. Mirrors `SleeveSpecConfig`.
- `schemas/manifest.schema.json` — TOML schema for artifact bundle
  manifests. Mirrors the format documented in
  `docs/artifact-contract.md`.
- `schemas/feature_schema.schema.json` — JSON schema for feature schema
  documents that describe `FeatureVector` layout.
- `schemas/raw_envelope.schema.json` — JSON schema for the raw event
  envelope persisted before normalization.
- `schemas/normalized_event.schema.json` — JSON schema for the union
  of `NormalizedEvent` variants emitted by the normalizer.
- `schemas/parity_cases.parquet.schema.md` — Markdown specification of
  the parquet columns used by the CI parity job.

Examples and parity fixtures for `weather_threshold` are placeholders
until the first vertical slice (Kalshi capture → NWS → Parquet → replay
→ paper executor) lands.
