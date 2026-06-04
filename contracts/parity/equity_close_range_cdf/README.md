# Equity Close Range CDF — Parity Fixture (placeholder)

`equity-close-range-cdf-v1` runs on the `ladder_cdf` Python runtime — the
coherent-ladder variant of `equity-index-range-ladder`. Paper-mode; no parity set
required to run `ec live-paper`.

Promotion needs generated parity cases from a replayed final-30-minutes-to-close
window, diffed via `eventcontracts-parity`. No Rust `ladder_cdf` archetype yet.
Loaders treat the absent fixture as "no coverage yet", not an error.
