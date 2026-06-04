# Macro CPI CDF — Parity Fixture (placeholder)

`macro-cpi-cdf-v1` runs on the `ladder_cdf` Python runtime — the coherent-CDF
variant of `macro_cpi_predictor` (pre-release only). Paper-mode; no parity set
required to run `ec live-paper`.

Promotion needs generated parity cases from a replayed pre-release window, diffed
via `eventcontracts-parity`; the walk-forward event study (no post-release
leakage) is the model gate. No Rust `ladder_cdf` archetype yet. Loaders treat the
absent fixture as "no coverage yet", not an error.
