# Commodity Brent Threshold CDF — Parity Fixture (placeholder)

`commodity-brent-threshold-cdf-v1` runs on the `ladder_cdf` Python runtime
(`plugins/strategies/ladder_cdf.py`). Paper-mode, so no parity set is required to
run `ec live-paper`.

Promotion to dry-run-live requires generated `(event_id, decision)` parity cases
from a replayed Brent close window, diffed through `eventcontracts-parity`. There
is no Rust `ladder_cdf` archetype yet — building it (with parity) is the hot-path
promotion step. Until the fixture exists, loaders treat its absence as "no parity
coverage yet", not an error.
