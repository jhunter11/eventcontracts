# Kalshi BTC 60s-Settlement Arb — Validation & Edge Spec (v1)

Status: pre-build validation. Owner workflow: versioned spec, weighted to trading
logic, backed by real runs. Created 2026-06-01. Sibling of
[[weather-kxhigh-validation-and-edge-spec]] and the tennis tradeability findings —
same governing principle: **calibration/clever math ≠ edge; prove it at the
tradable moment, net of fees/spread/fills, before building execution.**

## Thesis (as pitched)

A 15-minute Kalshi crypto contract settles on the **simple mean of the CF
Benchmarks RTI over the final 60 s** before expiry. Anticipate the final state of
that window → arbitrage Kalshi MMs. Four proposed metrics: (1) constituent-lag
synthetic index, (2) perp→spot lead-lag/OFI, (3) 60 s convergence, (4) VPIN/CVD.

## The convergence law (built + proven)

Inside the window at `t` s elapsed, `S_t` = observed partial sum, `tau = T - t`,
`c` = current index, driftless per-second vol `sigma`:

```
V = (1/T) Σ p_i  ~  N(mu_V, s_V^2)
mu_V = (S_t + c·tau) / T
s_V  = sigma · tau^{3/2} / (T·√3)      # Var = sigma^2 tau^3 / (3 T^2)
P(above K) = Φ((mu_V − K) / s_V)
```

`s_V` collapses as **tau^{3/2}** (Asian-average kernel), not the √tau of a vanilla
option. BTC \$100k @ 50% annual → sigma ≈ \$8.9/s:

| tau (s) | s_V | bp of \$100k |
|---|---|---|
| 60 (window open) | \$39.8 | 4.0 |
| 30 | \$14.1 | 1.4 |
| 10 | \$2.71 | 0.27 |

**Implemented** in `python/src/eventcontracts/research/btc_settlement.py`
(`within_window_forecast`, `before_window_forecast`, `delta_to_index`); **validated**
in `python/tests/test_btc_settlement.py` — the closed form reproduces the table AND
agrees with a Monte-Carlo of driftless per-second paths (mean/std/P within MC error).

## Where the edge actually is (the only two knobs)

`S_t` is shared with the MMs, so you can only disagree on **`c`** and **`sigma`**.
- `∂P/∂c ∝ 1/√tau` — knowing true `c` sooner pays most in the final seconds, exactly
  when your order round-trip is least likely to land. **That tension is the game.**
- vol/drift edge is zero at-the-money; only matters once already ITM/OTM.
- At window-open the settlement is already pinned to ±4 bp, so the "40¢ but my math
  says 95¢" scenario almost never survives *inside* the window. The real uncertainty
  is **where `c` lands when the window opens** (ordinary √t diffusion of the level,
  `before_window_forecast`), not the averaging.

Honest scoring of the four metrics: (1) constituent-lag → faster `c`, the one that
survives, but it's a *residual* ms lead vs colocated MMs that must be **measured**.
(2) perp-lead → real but decays in seconds, not "1–5 min". (3) 60 s convergence = the
pricing lens above, **not** an edge. (4) VPIN/CVD = weakest, contested OOS.

Latency claim is misdiagnosed: the binding constraint is **WAN RTT to Kalshi's
matching engine + per-account rate limits**, not L2-cache/thread pinning. Also:
**Binance perp data/exec is closed to US persons** — a data-access blocker.

## Two gating measurements (prove-before-expand — do these before any Rust)

1. **`c`-lead distribution (ms):** your synthetic index (constituent L2) timestamp vs
   the official RTI print. If not reliably positive, metrics 1–4 are moot.
2. **Model-vs-market gap at `t = 60/30/10` s:** Kalshi top-of-book vs model `P(Yes)`.
   Edge exists only where it exceeds fees + your fill-side spread *at a moment you
   could have hit it*. Built as `python/scripts/btc_settlement_gap.py` (recorder).

Only if BOTH clear: build the Rust synthetic-index + execution leg.

## Real-run findings (2026-06-01)

- **No short-duration BTC market is open.** Scan of 60k open Kalshi markets: only
  **monthly** `KX{BTC,ETH}{MAX,MIN}MON` "trimmed mean … by month-end" contracts, and
  **thin** (24h vol ~2,008 / 10). No `KXBTCD`/hourly/15-min in the open set. The 60s
  simple-average product the thesis targets is not currently listed → nothing to
  measure for the actual strategy.
- **No constituent feeds in-repo** (no Coinbase/Kraken/Bitstamp/LMAX/Gemini client, no
  CFB RTI). The `c`-lead measurement (gating #1, the only surviving edge) has no data
  source here. Binance is US-blocked regardless.
- `btc_settlement_gap.py` pricing the open monthly markets (BTC spot ~\$72k, realized
  vol ~39%) → **0 actionable edges**; every gap is **settlement-model mismatch**
  (monthly trimmed-mean/touch ≠ the terminal/60s-average digital), correctly flagged
  not-actionable. A clean demonstration that the naive gap is dominated by
  misspecification, not edge.

## Conclusion / next

Built + proven: the convergence kernel (the lens) + the gap recorder (the rung-1
machinery, runnable on real public data). **Blocked from a real edge measurement** by
(a) no short-duration product currently listed and (b) no constituent feeds/RTI access.
Next real step is **data acquisition**: a forward recorder on the constituent spot WS
(Coinbase/Kraken — US-accessible) to measure the `c`-lead distribution, run only when a
short-duration BTC market relists. Do not build the Rust execution leg until both
gating measurements clear.

## Refresh: official RTI adapter and 2026-06-04 observe run

`python/scripts/btc_clead_recorder.py` now supports an optional read-only CF
Benchmarks WebSocket reference path. If `CFB_WS_USERNAME` and
`CFB_WS_PASSWORD` are configured, it subscribes to `rti_stats` for `BRTI` and
logs `lead_label=official_rti` rows. Without those credentials it logs
`lead_label=proxy_only_no_official_rti` and
`lead_reason=official_cfb_rti_not_configured`.

Current official-source reality:

- CF Benchmarks publicly documents BRTI as a once-per-second Bitcoin real-time
  index and the price input for Kalshi crypto event contracts.
- CF Benchmarks documents timestamped WebSocket `rti_stats` updates, but the
  WebSocket API requires a licensed API key.
- Therefore this repo can parse and record official RTI lead if a legal
  read-only key is supplied, but the current environment still has no official
  lead measurement.

2026-06-04 local observe refresh:

- `btc_settlement_bench.py --net-samples 7 --net-pause 0.25`: Coinbase REST
  median `40 ms`, Kalshi REST read proxy median `37 ms`, compute mean `580 ns`.
- `btc_settlement_gap.py --series KXBTC15M`: one live row,
  `KXBTC15M-26JUN032100-00`; gross gap `+0.0206`, spread `0.0200`, fee
  `0.0152`, net after costs `-0.0046`; not actionable.
- `btc_clead_recorder.py --duration-sec 45 --min-venues 2`: 898 two-venue
  synthetic rows; all `proxy_only_no_official_rti`; official rows `0`;
  component-age median `92.8 ms`, p95 `171.6 ms`, max `576.5 ms`.

Decision: BTC stays observe-only. The public synthetic-index recorder works, but
there is no official CFB RTI lead and no positive post-cost KXBTC15M gap in the
latest run. No execution thread is justified.
