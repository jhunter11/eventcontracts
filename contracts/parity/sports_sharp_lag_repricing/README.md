# Sports Sharp-Lag Repricing — Parity Fixture (placeholder)

`sports-sharp-lag-repricing-v1` runs on the generic `external_edge` runtime
(de-vigged sharp-consensus probability vs Kalshi mid). Paper-mode; no parity set
required to run `ec live-paper`.

Promotion needs generated `(event_id, decision)` parity cases from a replayed
slate, plus the CLV-vs-sharp-close gate (split by lag bucket, league, liquidity).
Loaders treat the absent fixture as "no coverage yet", not an error.
