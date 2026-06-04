# MLB Outright Residual — Parity Fixture (placeholder)

`mlb-outright-residual-v1` runs on the generic `external_edge` runtime (outright
YES probability from sportsbook futures + schedule simulation vs Kalshi mid).
Paper-mode; no parity set required to run `ec live-paper`.

Promotion needs generated parity cases plus a capital-duration / portfolio-
correlation analysis (long-horizon, highly correlated outrights). Loaders treat
the absent fixture as "no coverage yet", not an error.
