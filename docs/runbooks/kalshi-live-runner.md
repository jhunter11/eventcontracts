# Kalshi Live Runner Runbook

## Preconditions

- **Promotion gate green.** `python -m eventcontracts.cli verify-strategy <name>` must pass (parity instantiation in both languages, ≥1 parity case, `parity_check`, and the no-trade smoke proving ≥1 risk-APPROVED intent). Do not take a strategy live that has not passed this.
- `KALSHI_ENV` must be explicitly set to `demo` or `prod` (live mode refuses an implicit env).
- `KALSHI_API_KEY_ID` and the private key path/env used by `KalshiAuth` must be present.
- Pass `--sleeve-spec <live sleeve>` so the live risk limits (order/position/daily-loss/open-orders/gross) come from the funded sleeve, not dev defaults.
- Use `--max-live-orders N` with `--live-submit`; live mode refuses to start without the cap.
- Pass either `--reconcile-on-start` (full reconciliation — see below) or `--cancel-orphans-on-start`; live mode refuses to start without one.
- Market-data freshness on the live path uses the runner's built-in window (~30s) plus last-look `require_executable_bbo` / `require_l1_depth` (auto-enabled under `--live-submit`). The sleeve TOML's `max_market_data_age_ms` governs the Python/backtest gate only.

## Safe Startup

```powershell
cargo run -p eventcontracts-live-runner -- `
  --tickers KXEXAMPLE-26MAY27-T50 `
  --duration-secs 3600 `
  --strategy-spec ..\configs\strategies\example.toml `
  --live-submit `
  --max-live-orders 3 `
  --cancel-orphans-on-start
```

The runner subscribes to public market data plus authenticated `fill` and `order` channels. Sequence gaps force a reconnect and resubscribe. Missing market-data freshness is a hard risk reject.

## Tennis go-live (real capital)

`sports_tennis_xgboost` is the first promoted single-taker target. It crosses the spread (IOC at the opposite touch), attaches its own market snapshot, and is gated post-fee (the negative-edge-after-fees check fires; fee defaults to Kalshi's 7% coefficient). Steps:

1. **Pass the gate:** `python -m eventcontracts.cli verify-strategy sports-tennis-xgboost` → `OK strategy promotable`.
2. **Fund + review the sleeve:** edit `configs/sleeves/sports-tennis-kalshi-live-a.toml` — set `capital_allocation` to your funded amount and confirm the `[risk]` circuit breakers (`max_order_notional`, `max_daily_loss`, `max_gross_exposure`). These are conservative first-deploy starters, not tuned sizes.
3. **Set credentials + env:** `KALSHI_ENV=prod`, `KALSHI_API_KEY_ID`, and the `KalshiAuth` private key.
4. **Launch** (writes a reconciliation diff and a live metrics snapshot you can tail):

```powershell
$env:KALSHI_ENV = "prod"
cargo run -p eventcontracts-live-runner -- `
  --strategy-spec ..\configs\strategies\sports-tennis-xgboost.toml `
  --sleeve-spec   ..\configs\sleeves\sports-tennis-kalshi-live-a.toml `
  --tickers KXTENNIS-... `
  --duration-secs 3600 `
  --live-submit `
  --max-live-orders 3 `
  --reconcile-on-start `
  --reconcile-report   .\reconcile.json `
  --metrics-snapshot-file .\metrics.txt `
  --metrics-json       .\metrics-final.json
```

The runner prompts `Type yes to proceed` before any live submission (pass `--yes` only for an automated run that already has an external review gate). Start with `--max-live-orders 1` and a single ticker for the very first session, then widen.

## Kill Switch

Create the configured kill-switch file to halt the sleeve and bulk-cancel:

```powershell
New-Item .eventcontracts.KILL_SWITCH -ItemType File
```

Ctrl-C also triggers venue bulk cancel before shutdown. If cancel-all fails, the runner logs the failure; the operator must verify open orders directly in Kalshi.

## Reconciliation

With `--reconcile-on-start` the runner, before placing any order: restores `daily_realized_loss` by re-summing venue fills since UTC midnight (idempotent across restarts — no double-count), seeds venue **positions** and **balance** into local risk state, and writes a diff report to `--reconcile-report`. **If the adopted baseline already breaches risk, the runner halts before submitting** — clear or repair venue state, then restart. Resting venue orders are adopted into the local OMS, or cancelled if you pass `--cancel-orphans-on-start`.

Investigate immediately if the runner logs:

- `own fill for unknown order`
- `own order update for unknown order`
- `sequence gap detected`
- `private event projection err`
- `bulk cancel failed`

For unknown private events, query venue orders by `client_order_id`, cancel or adopt the order manually, then restart with `--cancel-orphans-on-start` if unsure.

## Hard Stops

Stop live submission when any of these occur:

- repeated WS reconnect budget exhaustion
- REST circuit breaker opens after auth failures
- risk rejects for missing market data on subscribed instruments
- private feed events stop while public quotes continue
- ledger/fill counts diverge from the venue UI

## Post-Run Checks

- Confirm the report shows own fills/order updates when orders traded.
- Confirm `gateway errors`, `private event errors`, and `sequence gaps` are zero or explained.
- Verify the venue has no unexpected resting orders.
