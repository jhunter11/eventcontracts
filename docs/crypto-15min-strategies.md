# Crypto 15-min Strategy Specs

This document is the strategy-specs companion for the
`eventcontracts.plugins.strategies.crypto_*` family. It follows the
same shape as `docs/strategy-specs.md` and `docs/golf-strategy-specs.md`:
each strategy has a discovery block, a review packet, a feature
contract, a label, a policy, and sizing rules. Every section refers to
the Researcher Guide (`docs/ml-strategy-researcher-guide.md`) for the
underlying contract.

## Why 15-min Crypto

Kalshi lists 15-minute BTC, ETH, and SOL settlement markets keyed off
the CF Benchmarks index. Each expiry exposes a disjoint, exhaustive
partition of strike brackets covering the entire price space. Three
properties make this family especially productive for research:

1. **Deep, clean history**: 24/7 underlying with second-level tick data
   from Binance, Coinbase, Kraken, Bybit, and CME. Multi-year
   walk-forward windows are trivial to assemble.
2. **Dense expiry cadence**: ~96 expiries per ticker per day produce
   many independent samples. ML estimators converge quickly.
3. **Deterministic settlement**: TWAP of the CF Benchmarks index over
   the final minute of the contract. No subjective resolution. Labels
   can be computed exactly from spot history alone.

## Shared ML Contract

### Universe

* **Venues**: Kalshi (binary brackets); Polymarket where listed.
* **Instrument patterns**: `KXBTCD*`, `KXETHD*`, `KXSOLD*`.
* **Lifecycle**: trade only `OPENED`, cancel on `PAUSED`/`CLOSED`/`DETERMINED`.

### Features

Allowed inputs (point-in-time correct):

| Feature | Source | Notes |
| --- | --- | --- |
| spot last price | `ExternalSignalEvent(source="binance")` | one-second cadence target |
| spot returns 1m/5m/15m/1h | derived | log-returns |
| realized vol 5m/15m/1h | derived | annualized via `√(year/window)` |
| ATM implied vol | `ExternalSignalEvent(source="deribit")` | matching expiry; payload `atm_iv`, `expiry_iso` |
| Kalshi mid | `QuoteEvent` | mid = (bid + ask) / 2 |
| Kalshi spread bps | `QuoteEvent` | gates wide-spread bookings |
| bracket parity sum | derived | sum of mids across the partition |
| time to expiry | derived | `(expiry_at - ctx.now).total_seconds()` |
| strike normalized distance | derived | `(strike - spot) / spot` |
| strike in vol units | derived | `(strike - spot) / (spot * σ * √τ)` |

Disallowed (per the researcher guide):

* Future spot prices.
* Final settlement before it was known.
* Revised CF Benchmarks values as if they were original.
* Any Kalshi mid after `ctx.now`.

### Labels

| Label | Definition | Use |
| --- | --- | --- |
| `settlement_value` | 1 if YES bracket settles in-the-money else 0 | primary; horizon = time to expiry |
| `bs_implied_vs_kalshi_diff` | BS prob minus Kalshi mid at t0 | mispricing target |
| `realized_vol_next_15min` | annualized RV in `(t0, t0 + 900s]` | regime model target |
| `bracket_parity_deviation` | `sum(mids) - 1` at t0 | parity classifier target |
| `next_mid_change_bps` | bps change in mid over horizon | execution-aware short-horizon model |

### Latency assumptions

* External signal floor: **200ms** (Binance WS → normalizer → bus).
* Spot tick cadence target: **1s**.
* Decision-to-send: per default execution priority in each spec.

### Censoring

* Spot feed missing for > 30s in the feature window.
* `MarketLifecycleEvent` of kind `PAUSED` within the prediction horizon.
* `SettlementResolvedEvent` flagged disputed or missing.
* External vol feed older than 60s when the vol-surface strategy fires.

---

## 1. Bracket Parity Arbitrage

**Strategy ID**: `crypto-bracket_parity_arb-v1`

**Module**: `python/src/eventcontracts/plugins/strategies/crypto_bracket_parity_arb.py`

**Hypothesis**: The sum of Kalshi YES probabilities across a disjoint,
exhaustive bracket partition must equal 1. Retail flow disproportionate
to "interesting" strikes breaks parity in tradable amounts because each
bracket has a separate MM cohort.

**Game theory**: Pure no-arbitrage trade — winning at settlement does
not depend on which bracket settles in the money, because the venue
pays exactly 1 unit to the winning bracket.

**Feature inputs**: `QuoteEvent` only. No external data.

**Label**: `bracket_parity_deviation` at t0; expected PnL per round is
`|deviation| - n_brackets * (fee_rate * size)`.

**Policy**:

```
if abs(sum(probs) - 1) > min_parity_edge
and max(spread_bps) < max_spread_bps
and all brackets have current mid:
    emit one PlaceOrder per bracket on the favorable side
else:
    emit NoAction(reason)
```

**Sizing**: Constant `size` per leg, scaled by deviation magnitude in a
later allocator policy.

**Sleeve**: `crypto-bracket-parity-kalshi-paper-a`.

---

## 2. Vol-Surface Mispricer

**Strategy ID**: `crypto-vol_surface_mispricer-v1`

**Module**: `python/src/eventcontracts/plugins/strategies/crypto_vol_surface_mispricer.py`

**Hypothesis**: Deribit ATM implied volatility at the matching expiry
provides a model-implied Black-Scholes probability for every Kalshi
strike. Kalshi retail flow over-pays for moderately-OTM strikes
("almost achievable" lottery tickets); the gap exceeds round-trip fees
on size-disciplined entries.

**Game theory**: Short retail's OTM bias. The signal is strongest at
roughly 0.5-1.5σ away from spot.

**Feature inputs**: `ExternalSignalEvent(source="binance")` for spot,
`ExternalSignalEvent(source="deribit")` for ATM IV and `expiry_iso`,
`QuoteEvent` for Kalshi mids.

**Label**: `settlement_value` paired with `bs_implied_vs_kalshi_diff`
at t0; calibration target.

**Policy**:

```
if abs(bs_prob - kalshi_mid) * 10000 > min_edge_bps
and spot and atm_iv and expiry_at are all current:
    emit PlaceOrder(outcome_side=YES if bs > mid else NO)
```

**Sizing**: Constant `size`, future enhancement: edge-proportional with
Kelly-fraction cap.

**Sleeve**: `crypto-vol-surface-kalshi-paper-a`.

---

## 3. Terminal Drift Tracker

**Strategy ID**: `crypto-terminal_drift_tracker-v1`

**Module**: `python/src/eventcontracts/plugins/strategies/crypto_terminal_drift_tracker.py`

**Hypothesis**: Inside the last ~60 seconds of an expiry, the binary
outcome is effectively determined by spot vs strike. Retail quotes lag
spot ticks, leaving stale mids exploitable by latency.

**Game theory**: Pure latency edge. The strategy degrades gracefully —
its edge equals the slowest counterparty's stale-quote dwell time.

**Feature inputs**: `ExternalSignalEvent` for spot (and `expiry_iso`),
`QuoteEvent` for Kalshi mids, `TimerEvent(label="crypto_terminal_check")`
to gate evaluation.

**Label**: `next_mid_change_bps` over the remaining `τ`, post-fee.

**Policy**:

```
on TimerEvent("crypto_terminal_check"):
    if τ <= terminal_window_seconds
    and realized_vol history >= min_realized_samples
    and |bs_prob(spot, strike, realized_σ, τ) - kalshi_mid| > min_terminal_edge:
        emit PlaceOrder(time_in_force=IOC, priority=FAST)
```

**Sizing**: Constant `size`. IOC orders only — passive resting is
inappropriate inside the terminal window.

**Sleeve**: `crypto-terminal-drift-kalshi-paper-a`.

---

## 4. Realized-Vol Regime Trader

**Strategy ID**: `crypto-realized_vol_regime-v1`

**Module**: `python/src/eventcontracts/plugins/strategies/crypto_realized_vol_regime.py`

**Hypothesis**: Crypto volatility clusters at 1-15 minute horizons.
Kalshi's at-the-money bracket implies a vol; comparing it to a
short-window realized vol forecast yields a tradable regime signal.
Retail buys far-OTM "tail tickets" insensitive to current vol, so
wide-tail brackets print structurally rich in calm regimes and cheap
in storms.

**Game theory**: Short retail's flat tail-pricing. Edge concentrated in
regime transitions, not steady-state.

**Feature inputs**: `ExternalSignalEvent` (binance spot), `QuoteEvent`
on ATM and tail brackets.

**Label**: `realized_vol_next_15min` minus Kalshi-implied vol at t0.

**Policy**:

```
if abs(rv_short_window - kalshi_iv_from_atm) > min_vol_edge
and full set of tail-bracket mids is current:
    if rv > iv → buy YES on each tail leg
    else       → buy NO on each tail leg (sell YES)
```

**Sizing**: Constant `size` per tail leg.

**Sleeve**: `crypto-realized-vol-kalshi-paper-a`.

---

## 5. Cross-Strike Skew Arbitrage

**Strategy ID**: `crypto-cross_strike_skew_arb-v1`

**Module**: `python/src/eventcontracts/plugins/strategies/crypto_cross_strike_skew_arb.py`

**Hypothesis**: ``P(S_T >= K)`` must be monotone non-increasing in K.
Across a Kalshi strike grid for one expiry, separate MM cohorts at
each strike sometimes break monotonicity; the inversion is a
butterfly arbitrage.

**Game theory**: Pure no-arbitrage relationship. Convergence is
guaranteed by the venue's settlement rule.

**Feature inputs**: `QuoteEvent` only.

**Label**: `bs_implied_vs_kalshi_diff` per strike at t0; secondary:
realized PnL of the two-leg trade at expiry.

**Policy**:

```
on QuoteEvent:
    sort tracked strikes ascending
    detect (strike_low, p_low) → (strike_high, p_high) with p_high > p_low
    for each violation:
        if p_high - p_low > min_skew_edge and both spreads < max:
            emit PlaceOrder YES @ low strike
            emit PlaceOrder NO  @ high strike
```

**Sizing**: Constant `size` per leg, kept small because two-leg
adverse selection compounds.

**Sleeve**: `crypto-skew-arb-kalshi-paper-a`.

---

## 6. Signal Ensemble (Meta-Strategy)

**Strategy ID**: `crypto-signal_ensemble-v1`

**Module**: `python/src/eventcontracts/plugins/strategies/crypto_signal_ensemble.py`

**Hypothesis**: Each of the five strategies above attacks a different
mispricing regime and produces a structured signal (instrument, side,
edge, confidence). A confluence-based aggregator that only fires when
two or more independent sources agree on the same instrument and the
weighted net edge clears a threshold trades less, but every trade has
a higher Sharpe-after-fees than any single source.

**Game theory**: The five underlying signal sources are deliberately
non-overlapping in the *types* of edge they extract (no-arbitrage,
external-data calibration, latency, regime, butterfly). Two of them
agreeing on the same instrument is therefore strong evidence — the
joint probability of an accidental coincidence is much lower than the
per-source false-positive rate. The ensemble degrades gracefully when
any one source is unavailable (no upstream data, market paused) since
the others still contribute.

**Architecture**:

```
Signal {instrument_id, side, edge_bps, confidence, source}
  ↓
combine_signals: weighted Σ(edge_bps × confidence × sign(side)) per instrument
  ↓
EnsembleVerdict {side, net_edge_bps, contributing_sources}
  ↓
PlaceOrder on dominant side (+ Alert with per-source breakdown)
```

**Feature inputs**: union of every underlying source — `QuoteEvent`,
`ExternalSignalEvent(binance|deribit)`, `TimerEvent`.

**Policy**:

```
for each instrument with >= min_confluence contributing sources:
    net = Σ weight[source] * edge_bps * confidence * sign(side)
    if |net| > min_combined_edge_bps:
        PlaceOrder on sign(net)
    else:
        NoAction (HOLD verdict, Alert with breakdown)
```

**Sizing**: Constant `size` per fired order; future enhancement can
scale by `|net_edge_bps|`.

**Sleeve**: `crypto-signal-ensemble-kalshi-paper-a`.

**Synthetic backtest harness**:
:mod:`eventcontracts.crypto.synthetic` generates deterministic 15-min
event streams with two configurable mispricings (uniform parity bump,
single-strike skew bump). Six tests in
`tests/test_crypto_synthetic_backtest.py` validate the ensemble's
behavior on fair vs mispriced regimes including replay determinism.

**Live Deribit demo**:
`scripts/run_ensemble_demo.py` pulls the live BTC ATM IV from
Deribit's public REST API, seeds the synthetic generator with it,
and runs the ensemble end-to-end. Use the `EVENTCONTRACTS_INSECURE_TLS=1`
env var in research VMs whose system clock is far enough in the future
for Deribit's certificate to appear "not yet valid".

## Promotion Checklist

Each crypto strategy must clear the standard researcher-guide gates
before paper → dry-run → live promotion:

1. Feature schema validates.
2. Label code reviewed for spot/Kalshi leakage.
3. Walk-forward backtest over ≥ 90 days of CF Benchmarks history.
4. Replay determinism test passes on a fixed Parquet partition.
5. Risk-reject coverage: order-notional, position-notional,
   max-open-orders, gross exposure, daily loss, kill switch.
6. Paper PnL positive after Kalshi 7%-of-price-times-(1-price) fees.
7. Artifact bundle (manifest + spec + feature schema + parity cases).
8. Parity cases pass against the Rust runtime (when implemented).
