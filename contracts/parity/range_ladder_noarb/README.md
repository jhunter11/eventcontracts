# Range Ladder No-Arb — Parity Fixture (placeholder)

`range-ladder-noarb-v1` is the cumulative-mode config of the deterministic
`kalshi_noarb_scanner` (flags `P(>=t)` monotonicity violations on CPI/equity/Brent
ladders). Paper-mode; no parity set required to run `ec live-paper`.

Because it is deterministic, its parity set is clean: replay a cumulative-ladder
window and record the flagged violations / lock legs, diffed via
`eventcontracts-parity`. Headline promotion blocker = leg risk on two-leg
execution. Loaders treat the absent fixture as "no coverage yet", not an error.
