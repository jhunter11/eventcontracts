# Weather Ladder CDF — Parity Fixture (placeholder)

`weather-ladder-cdf-v1` runs on the `ladder_cdf` Python runtime — the
coherent-ladder variant of `weather_temperature_arbitrage`. Paper-mode; no parity
set required to run `ec live-paper`.

Promotion needs generated `(event_id, decision)` parity cases from a replayed
station-day window, diffed via `eventcontracts-parity`. No Rust `ladder_cdf`
archetype yet. Loaders treat the absent fixture as "no coverage yet", not an error.
