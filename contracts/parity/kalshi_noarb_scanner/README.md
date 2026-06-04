# Kalshi No-Arb Scanner — Parity Fixture (placeholder)

`kalshi-noarb-scanner-v1` is a deterministic logical-arbitrage scanner (no
model). It is **paper-mode**, so no parity set is required to run
`ec live-paper`.

This directory is a placeholder. Because the strategy is deterministic, its
parity set is the cleanest of the v7 sleeves: replay a captured ladder window,
record the emitted IOC lock legs (exclusive mode) and the flagged monotonicity
violations (cumulative mode), and diff through `eventcontracts-parity` (see
`docs/strategy-promotion.md` and `docs/v7-live-test-ready-strategy-specs.md` §6).

The headline promotion blocker is **leg risk**: live two-leg / N-leg execution
must handle partial fills atomically before this goes past paper. Until the
fixture exists, both loaders treat its absence as "no parity coverage yet", not
an error.
