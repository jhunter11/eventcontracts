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


## Tennis go-live: odds feed and schema guard (F8 / F9)

The live tennis sleeve sets `require_odds_present = true`, so the live path only
trades a market when the scored snapshot carries **both** players' decimal odds
(> 1.0). Two operator steps make this safe and observable.

### 1. Wire the odds feed (F8) - required before funding

`research/tennis_odds.py` is training-time only; it does **not** feed live odds.
Populate the upcoming-matches table from a vendor-neutral odds export before
scoring:

```
# odds.csv : one row per player, exported from any book/aggregator
#   player,decimal_odds
#   Carlos Alcaraz,1.50
#   Novak Djokovic,2.60
python -m eventcontracts.cli tennis-merge-odds     --matches upcoming_matches.csv     --odds odds.csv     --out  upcoming_matches.with_odds.csv     --min-match-rate 1.0        # exit 2 if any match is missing odds
```

The command prints per-match coverage and the list of unmatched matches; with
`--min-match-rate` it fails loudly rather than letting a thin feed turn the
sleeve into a silent no-op. The matched-name columns default to `p1_name` /
`p2_name` (the scoring template's columns). Feed `--out` to
`tennis-xgboost-score` to produce the snapshot JSONL consumed by
`--tennis-snapshots-jsonl`.

On the Rust side, the live runner counts snapshots that arrived without odds and
surfaces them in the metrics snapshot as `tennis_snapshots_missing_odds`
(alongside `tennis_snapshots_scored`) plus a stderr warning - so a zero-order
run is never silently attributed to "no edge". Watch that gauge on the first run.

### 2. Pin the bundle schema version (F9) - fail-closed

Pass `--expect-tennis-schema-version 2` to the live runner. If the promoted
bundle's `feature_schema_version` does not match, the runner refuses to start
instead of silently scoring a v1 bundle with the v2 (34-feature) vector, or
vice versa:

```
eventcontracts-live-runner ...     --tennis-artifact /path/to/promoted/bundle     --tennis-snapshots-jsonl snapshots.jsonl     --expect-tennis-schema-version 2
```

Confirm promotion first with
`python -m eventcontracts.cli verify-strategy sports-tennis-xgboost`.

### 3. v2 live snapshot via helper scripts ($5 first-live flow)

The v1 `tennis-xgboost-score` path above pre-computes a probability and emits a
schema-v1 payload. For the **v2** bundle the live runner *re-scores from the raw
snapshot*, so feed it a v2 snapshot JSONL built directly from player names — no
34-feature hand-fill. Two helper scripts cover odds + features:

```powershell
$env:PYTHONPATH = "python/src"; $py = ".venv\Scripts\python.exe"

# 1) live pre-match odds (prefers Pinnacle, the sharp book) -> odds.csv
& $py python\scripts\fetch_tennis_odds.py --list-sports             # find the active tournament key
& $py python\scripts\fetch_tennis_odds.py --sport tennis_atp_<event> `
    --p1 "Player One" --p2 "Player Two" --out odds.csv              # needs THE_ODDS_API_KEY

# 2) build the runner's v2 snapshot JSONL (state from canonical history + odds)
& $py python\scripts\build_upcoming_snapshot.py `
    --p1 "Player One" --p2 "Player Two" --surface Hard --date 2026-06-10 `
    --best-of 3 --round R32 --tourney-level A `
    --market-id <KALSHI_TICKER> --odds-csv odds.csv --out snapshots.jsonl

# 3) launch — the runner scores snapshots.jsonl with the v2 bundle
cargo run -p eventcontracts-live-runner --manifest-path rust\Cargo.toml -- `
    --strategy-spec configs\strategies\sports-tennis-xgboost.toml `
    --sleeve-spec   configs\sleeves\sports-tennis-kalshi-live-a.toml `
    --tennis-artifact artifacts\tennis_xgboost\bundles\sports_tennis_xgboost__live-candidate-20260530 `
    --tennis-snapshots-jsonl snapshots.jsonl --expect-tennis-schema-version 2 `
    --tickers <KALSHI_TICKER> --duration-secs 3600 --live-submit `
    --max-live-orders 1 --reconcile-on-start --reconcile-report reconcile.json `
    --metrics-snapshot-file metrics.txt
```

`--market-id` (step 2) MUST equal `--tickers` (step 3) so the prediction maps to the
right Kalshi market. `build_upcoming_snapshot.py` prints the resolved players + key
features and warns loudly if odds are missing (the sleeve is a no-op without them).
Watch `tennis_snapshots_missing_odds` in metrics.txt on the first run. NOTE: the
sleeve prices off the model, which backtests *worse* than the closing line — this run
validates the live path + collects real fills, it is not a validated-edge strategy
(see `docs/tennis-tradeability-findings-and-plan.md`).

**Position lifecycle (trailing stop).** Once filled, the position HOLDS to settlement
unless the held side's best bid falls `trailing_stop_loss` (default `0.12` = 12c in
`configs/strategies/sports-tennis-xgboost.toml`) below its running peak — then it
liquidates as a taker sell at the bid (a SELL on the live metrics). Set
`trailing_stop_loss = "0"` for pure hold-to-completion. This is drawdown control, not
alpha. Entry **and** exit are cross-language parity-covered (`contracts/parity/
sports_tennis_xgboost/03..05`).
