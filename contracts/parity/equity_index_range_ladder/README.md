# Equity Index Range Ladder — Parity Fixture (placeholder)

`equity-index-range-ladder-v1` runs on the generic `external_edge` archetype
(per-bracket terminal-distribution probability vs Kalshi mid). It is
**paper-mode**, so no parity set is required to run `ec live-paper`.

This directory is a placeholder. Promotion to dry-run-live requires a generated
`(event_id, decision)` parity set from a replayed final-30-minutes-to-close
window, diffed through `eventcontracts-parity` (see `docs/strategy-promotion.md`
and `docs/v7-live-test-ready-strategy-specs.md` §5). Joint-CDF / no-arb ladder
enforcement (vs the per-bracket path that runs today) is the deferred Rust
archetype and will need its own parity coverage.

Until the fixture exists, both loaders treat its absence as "no parity coverage
yet", not an error.
