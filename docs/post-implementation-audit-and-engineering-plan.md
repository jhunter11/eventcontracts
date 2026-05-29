# Post-Implementation Audit And Engineering Plan

**Superseded:** Use `docs/v5-audit-and-agent-implementation-spec.md` as the
current implementation source of truth. This file is retained for historical
context only.

Date: 2026-05-28

Scope: follow-up implementation and audit against `docs/v4-audit-and-agent-implementation-spec.md`, `docs/hyper-comprehensive-codebase-audit-agent-spec.md`, and the older live-readiness findings that remain authoritative where not superseded.

## Implemented In This Pass

1. Restart reconciliation no longer leaves adopted markets quote-dead.
   - `rust/crates/gateway/src/lib.rs:769` adds `RestingOrderSnapshot.observed_at`.
   - `rust/crates/gateway/src/lib.rs:1149` now returns the adopted instrument id.
   - `rust/crates/gateway/src/lib.rs:1197` seeds `last_quote_epoch_secs` for the adopted instrument/outcome side.
   - `rust/crates/live-runner/src/main.rs:496` subscribes to adopted markets before the main loop resumes.

2. Daily loss is restored on live startup reconciliation.
   - `rust/crates/kalshi/src/rest.rs:561` adds paginated `list_fills_since(epoch_secs)`.
   - `rust/crates/live-runner/src/main.rs:460` fetches fills since UTC midnight.
   - `rust/crates/live-runner/src/main.rs:1102` seeds `sleeve_state.daily_realized_loss`.

3. Python strategies now receive risk/reservation feedback.
   - `python/src/eventcontracts/strategy/base.py:36` adds `StrategyFeedback`.
   - `python/src/eventcontracts/runner/base.py:288` emits `IntentAccepted` / `IntentRejected`.
   - `python/src/eventcontracts/plugins/strategies/weather_temperature_arbitrage.py:285` releases pending notional on reject.
   - `python/src/eventcontracts/plugins/strategies/microstructure_obi_scalper.py:187` releases/suppresses pending order state safely.
   - `python/src/eventcontracts/plugins/strategies/sports_tennis_xgboost.py:172` clears pending tennis state on reject feedback.

4. Rust has an exact Kalshi taker fee primitive.
   - `rust/crates/risk/src/fees.rs:10` implements `kalshi_taker_fee_ticks`.
   - Unit coverage matches the Python known points: 50c x 1 = 2c fee, 10c x 1 = 1c fee.

5. ONNX scoring contention is reduced.
   - `rust/crates/model-runtime/src/lib.rs:104` pins ONNX session intra-op threads to 1 and disables spinning.
   - `rust/crates/model-runtime/src/lib.rs:154` adds `OnnxScorerPool`.

6. Kalshi private key material can be injected without a mounted file.
   - `rust/crates/kalshi/src/auth.rs:112` supports `KALSHI_PRIVATE_KEY_PEM`.
   - `python/src/eventcontracts/adapters/venues/kalshi/client.py:72` supports the same environment variable.

## Verification Run

All checks passed:

```text
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo test --manifest-path rust/Cargo.toml --workspace
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo bench --manifest-path rust/Cargo.toml --workspace --no-run
cargo run --manifest-path rust/Cargo.toml --quiet -p eventcontracts-parity --bin parity_check -- --strategy-spec contracts/examples/weather_threshold/strategy_spec.toml --cases contracts/parity/weather_threshold
cd python && python -m compileall -q src tests
cd python && python -m ruff check src tests
cd python && python -m mypy src/eventcontracts tests
cd python && python -m pytest tests -q
```

Observed counts:

- Rust workspace tests: 143 passed.
- Python pytest: 245 passed, 9 skipped.
- Mypy: 205 source files, 0 errors.
- Parity: `weather_threshold_buy_from_external_quote` passed.

## Residual Audit Findings

### R1. Critical: blocking REST execution still runs inside the WebSocket loop

Where:

- `rust/crates/kalshi/src/venue_client.rs:62`, `:95`, `:115`, `:135` use `block_in_place`.
- `rust/crates/live-runner/src/main.rs:590` awaits `ws.next_envelope`.
- `rust/crates/live-runner/src/main.rs:880` calls `gateway.process_batch` inline.

Scenario:

1. A signal emits an intent.
2. `gateway.process_batch` submits through `KalshiVenueClient`.
3. REST submit/cancel stalls or retries.
4. WS ingestion stops while BBO and own-order events continue moving.
5. Later decisions are based on stale local state, and last-look happens later than the strategy expects.

Remediation:

- Split `live-runner` into ingest, decide, and execute loops.
- Move venue I/O into an async execution worker with bounded channels.
- Keep an atomic latest-BBO cache updated by ingest.
- Re-run last-look in the executor immediately before submit.
- Backpressure policy: drop oldest non-cancel intent when the execution queue is full; never drop cancels.

Tests:

- Replay 1000 events/sec while REST submit sleeps 200 ms; assert ingest lag p99 stays below 5 ms.
- Force latest BBO to move after decision but before executor submit; assert send-time last-look rejects.
- Fill the intent queue; assert cancels are retained and stale non-cancels are shed with metrics.

### R2. Critical: Rust live path still has no typed market suspension gate

Where:

- `rust/crates/live-runner/src/main.rs:399` subscribes to `ticker`, `trade`, `orderbook_delta`, `fill`, `order`, but not market lifecycle.
- `rust/crates/runtime-hot/src/project.rs:111` treats lifecycle as passthrough.
- `rust/crates/runtime-hot/src/event.rs:88` has `HotOwnFill` etc., but no typed market state variant.
- `rust/crates/gateway/src/lib.rs:84` has only place/cancel decisions and no market-state reject.

Scenario:

1. Venue pauses or suspends a market.
2. Last BBO remains in local memory.
3. Strategy continues emitting against an administratively non-tradable market.
4. Venue rejects or later reopens with stale resting orders still live.

Remediation:

- Subscribe to Kalshi lifecycle channel in live mode.
- Normalize lifecycle payloads into `{instrument, state}`.
- Add `HotEvent::MarketState { instrument, state }`.
- Add `SleeveState.market_state` or a gateway-owned `market_state` map.
- Gateway rejects new place intents unless state is `Active/Open`.
- On `Paused/Halted/Closed/Determined/Finalized`, gateway cancels own open orders for that instrument.

Tests:

- Fixture with open order then lifecycle pause; assert cancel issued.
- Submit place intent after pause; assert rejection reason `market_suspended`.
- Lifecycle reopen must require fresh BBO before new trading.

### R3. High: Rust fee model exists but is not yet part of trade edge admission

Where:

- `rust/crates/risk/src/fees.rs:10` computes fees.
- `rust/crates/risk/src/lib.rs:90` `IntentSnapshot` has no fair-value or edge fields.
- `rust/crates/risk/src/lib.rs:224` evaluates notional/position/freshness, not expected edge after fee.

Scenario:

1. Rust strategy buys a 50c contract with only 1c model edge.
2. Kalshi taker fee is 2c for one contract.
3. Risk approves because it does not know the model fair value or expected edge.
4. Strategy trades negative expected value after fees.

Remediation:

- Add optional `fair_price` and `min_executable_edge_ticks` to intent metadata or `IntentSnapshot`.
- Risk computes:
  - buy edge = `fair - limit_price - taker_fee_per_contract`
  - sell edge = `limit_price - fair - taker_fee_per_contract`
- Reject with `negative_edge_after_fees` when edge is below configured floor.
- Make fee model venue-specific and strategy-configurable only to stricter values.

Tests:

- 50c buy, fair 51c, qty 1: reject after 2c fee.
- 50c buy, fair 53c, qty 1: approve.
- Python/Rust parity case for fee-adjusted edge.

### R4. High: full reconciliation still lacks positions, balances, and durable checkpoints

Where:

- `rust/crates/kalshi/src/rest.rs:519` lists open orders.
- `rust/crates/kalshi/src/rest.rs:561` lists fills since a supplied epoch.
- No `list_positions`, no balance fetch, no persisted `last_reconcile_checkpoint_epoch`.

Scenario:

1. Process is offline over a fill and balance-changing settlement.
2. Restart restores open orders and daily loss, but not full venue position/balance truth.
3. Risk can size from local cash/positions that differ from the venue.
4. A second restart may reprocess the same private fills if the daily-only window is reused without a checkpoint.

Remediation:

- Add REST methods for positions and balance if exposed by Kalshi.
- Persist reconciliation checkpoints with `{last_fill_epoch, last_order_cursor, restored_at}`.
- Reconciliation should produce a diff report:
  - local-only orders
  - venue-only orders
  - quantity mismatches
  - position mismatches
  - balance mismatches
- Live submit starts only when diff is empty or operator chooses cancel/adopt policy.

Tests:

- Fixture: offline fill plus venue position; local state must match venue after reconcile.
- Fixture: venue balance below expected; risk cash must use venue value.
- Restart twice with same fills; daily loss must not double-count.

### R5. High: no atomic execution group for cross-venue arbitrage

Where:

- `python/src/eventcontracts/domain/decisions.py:119` includes single decisions only.
- No `AtomicExecutionGroup` or `LinkedIntents` type exists.
- `python/src/eventcontracts/plugins/strategies/arbitrage_cross_venue.py:116` emits a normal `PlaceOrder`.

Scenario:

1. Arbitrage logic identifies a Kalshi/other-venue spread.
2. One leg fills and the second leg rejects or moves.
3. The sleeve becomes directionally exposed even though strategy math assumed locked spread.

Remediation:

- Add `AtomicExecutionGroup { group_id, legs, legging_risk_cap_notional, cancel_window_ms }`.
- Gateway executes leg one, waits for ack/fill policy, then leg two.
- On leg-two reject or timeout, auto-cancel or hedge leg one according to group policy.
- Risk reserves legging risk for group lifetime.

Tests:

- Venue A accepts, venue B rejects; assert leg A cancel/hedge issued.
- Venue A partial fills; assert hedge size equals confirmed filled quantity.
- Group timeout must terminally fail and release reservation.

### R6. High: replace and partial-fill remaining are still not wired in Rust live execution

Where:

- `python/src/eventcontracts/domain/decisions.py:85` has `ReplaceOrder`.
- `rust/crates/gateway/src/lib.rs:84` `DecisionPayload` only supports `PlaceOrder` and `CancelOrder`.
- `rust/crates/runtime-hot/src/event.rs:88` `HotOwnFill` has quantity but no remaining.
- `rust/crates/runner/src/lib.rs:252` converts own fills without remaining.

Scenario:

1. Order fills 10%.
2. Strategy needs to cancel or reprice the 90% tail.
3. Rust event path only tells it the filled quantity, not the remaining quantity.
4. Tail can sit stale and becomes free optionality for others.

Remediation:

- Add `DecisionPayload::ReplaceOrder`.
- If Kalshi has no native replace, gateway emulates cancel-then-new atomically:
  - freeze new order until cancel terminal
  - abort new if fill arrives during cancel window
- Extend own-fill events with `remaining_quantity`.
- Strategies use `remaining_quantity` to decide cancel/reprice.

Tests:

- Partial fill of 10/100 emits remaining 90.
- Replace aborts if another fill arrives before cancel confirmation.
- Replace releases old reservation only after terminal cancel.

### R7. High: model price discretization is still ad hoc

Where:

- `rust/crates/runner/src/lib.rs:104` uses floating `.round()` for fixed prices.
- `rust/crates/runner/src/lib.rs:620` rounds min-edge ticks.
- Multiple Python strategies use local `_clip` helpers or raw quote prices, e.g. `python/src/eventcontracts/plugins/strategies/crop_drought_yield_reversion.py:88`, `weather_temperature_arbitrage.py:148`, `sports_tennis_xgboost.py:132`.

Scenario:

1. Model fair value is 0.556.
2. Strategy rounds buy limit to 0.56.
3. The rounding step itself consumes edge and can turn a positive model prediction into negative expected value.

Remediation:

- Add Python `strategy/pricing.py`:
  - `floor_to_tick`
  - `ceil_to_tick`
  - `buy_limit_from_fair`
  - `sell_limit_from_fair`
- Add Rust `runner::pricing` equivalents.
- Live-promotion lint rejects model strategies that directly round/clip prices.

Tests:

- Buy from 0.556 floors to 0.55.
- Sell from 0.556 ceils to 0.56.
- AST lint fails if promoted strategy calls `round()` or raw `_clip` for price.

### R8. High: self-cross protection is process-local, not account-global

Where:

- Gateway self-cross is local OMS based.
- Current architecture is still one sleeve per runner; no central account-wide CEG state.

Scenario:

1. Sleeve A buys YES at 50c.
2. Sleeve B sells YES at 50c in another process.
3. Neither local gateway sees the other's open order.
4. Orders can self-match at the venue, paying fees and creating compliance risk.

Remediation:

- Introduce a central execution gateway process or shared account-order service.
- All strategies submit to the CEG.
- CEG nets crossing intents internally or rejects them before venue dispatch.
- Account-wide self-match check must include live venue open orders from reconciliation.

Tests:

- Two sleeves emit crossing intents; assert no venue submit.
- Unknown account order from reconciliation blocks opposite-side crossing.

### R9. Medium: observability is still exit-time JSON, not live Prometheus

Where:

- `rust/crates/live-runner/src/main.rs` has `metrics_json` output, but no `--metrics-port`.
- No Prometheus scrape endpoint exists.

Scenario:

1. Last-look reject rate spikes.
2. Operator only sees it after process exit or manual log inspection.
3. Kill switch, reconnect exhaustion, and toxicity are not alertable in real time.

Remediation:

- Add `--metrics-port`.
- Expose counters/gauges/histograms:
  - gateway decision latency
  - last-look rejects by reason
  - stale intent drops
  - reconnect attempts/exhaustion
  - kill switch state
  - daily realized loss
- Add alert webhook or documented Prometheus alert rules.

Tests:

- Start runner in dry-run fixture mode, curl `/metrics`, assert expected metric names.
- Force a last-look reject, assert counter increments.

### R10. Medium: raw private payload redaction is not enforced

Where:

- `python/src/eventcontracts/storage/parquet_store.py:166` writes raw `payload_json`.
- `python/src/eventcontracts/storage/parquet_store.py:205` writes rejected raw payloads.

Scenario:

1. Private `fill` or `order` payload includes account id, order ids, or other sensitive fields.
2. Raw parquet capture persists verbatim.
3. Local data directories or artifacts can leak account-specific data.

Remediation:

- Add `RedactionPolicy` with default private-channel redaction.
- Hash or drop account id, key id, and private order metadata by default.
- Store unredacted private payloads only when encrypted and explicitly enabled.
- Add TTL cleanup for private raw partitions.

Tests:

- Private fill payload containing `account_id` writes parquet without that literal.
- Rejected private raw event is redacted too.

### R11. Medium: promotion manifests and stateful parity v2 remain incomplete

Where:

- Current parity binary handles single-event parity.
- Promotion manifest enforcement is not visible in CI for every promoted strategy.

Scenario:

1. Strategy passes a single tick parity case.
2. Live sequence includes quote, risk reject, partial fill, cancel, lifecycle pause.
3. Python and Rust state diverge only after the sequence starts.

Remediation:

- Add `configs/promotion/<strategy>.toml`.
- Add stateful parity cases with event sequence plus mock venue responses.
- Compare decision payload, priority, TTL, audit metadata, risk verdict, gateway verdict, and final strategy state.

Tests:

- CI fails if promoted strategy lacks manifest or zero parity cases.
- Stateful tennis/weather cases include risk reject and partial fill.

### R12. Medium: margin preemption and venue-native margin netting remain absent

Where:

- Gateway has priority scheduling, but no cancel-low-priority-to-free-capital path.
- Risk treats gross exposure conservatively and does not model exchange-native YES/NO margin offsets.

Scenario:

1. Passive orders reserve most of a sleeve.
2. High-conviction arbitrage arrives.
3. Risk rejects for capital even though canceling low-priority resting orders would free room.

Remediation:

- Add priority tiers: `Arbitrage`, `Momentum`, `Passive`.
- On high-tier capital reject, gateway selects lowest-tier open orders in same sleeve/group, cancels them, and retries once.
- Add venue-specific margin model for mutually exclusive outcomes.

Tests:

- Passive order blocks capital; arbitrage intent preempts and is admitted.
- Preemption cannot exceed retry count and must not cancel higher-tier orders.

## Recommended Implementation Order

### Phase 1: Async execution worker and live-market state

Files:

- `rust/crates/kalshi/src/venue_client.rs`
- `rust/crates/live-runner/src/main.rs`
- `rust/crates/runtime-hot/src/event.rs`
- `rust/crates/runtime-hot/src/project.rs`
- `rust/crates/gateway/src/lib.rs`

Steps:

1. Add typed market-state events and lifecycle subscription.
2. Add gateway suspended-market reject and instrument-scoped cancel.
3. Split live runner into ingest, decide, execute tasks.
4. Add latest-BBO atomic cache and executor last-look.
5. Add backpressure counters and drop policy.

Validation:

```text
cargo test --manifest-path rust/Cargo.toml -p eventcontracts-gateway market_state
cargo test --manifest-path rust/Cargo.toml -p eventcontracts-live-runner async_executor
cargo bench --manifest-path rust/Cargo.toml -p eventcontracts-live-runner ws_lag_under_load
```

### Phase 2: Reconciliation completeness

Files:

- `rust/crates/kalshi/src/rest.rs`
- `rust/crates/live-runner/src/main.rs`
- `rust/crates/gateway/src/lib.rs`

Steps:

1. Add REST balance and position fetchers.
2. Persist reconciliation checkpoint.
3. Produce startup reconciliation diff.
4. Halt live submit unless diff is clean or operator policy resolves it.

Validation:

```text
cargo test --manifest-path rust/Cargo.toml -p eventcontracts-kalshi reconcile
cargo test --manifest-path rust/Cargo.toml -p eventcontracts-live-runner daily_loss
```

### Phase 3: Fee-aware edge and deterministic price helpers

Files:

- `rust/crates/risk/src/lib.rs`
- `rust/crates/risk/src/fees.rs`
- `rust/crates/runner/src/lib.rs`
- `python/src/eventcontracts/plugins/strategies/*`
- new `python/src/eventcontracts/strategy/pricing.py`

Steps:

1. Add fair-value fields to intent snapshot or audit metadata contract.
2. Wire `kalshi_taker_fee_ticks` into risk admission.
3. Add Python/Rust price helper APIs.
4. Refactor promoted strategies to use buy-floor/sell-ceil helpers.
5. Add lint preventing raw `round()` or bespoke `_clip` in promoted pricing paths.

Validation:

```text
cargo test --manifest-path rust/Cargo.toml -p eventcontracts-risk fee
cd python && python -m pytest tests/test_strategy_pricing.py tests/test_strategy_promotion_guards.py
```

### Phase 4: Replace, partial-fill remaining, and atomic groups

Files:

- `python/src/eventcontracts/domain/decisions.py`
- `rust/crates/gateway/src/lib.rs`
- `rust/crates/runtime-hot/src/event.rs`
- `rust/crates/runner/src/lib.rs`
- `python/src/eventcontracts/plugins/strategies/arbitrage_cross_venue.py`

Steps:

1. Add Rust `ReplaceOrder`.
2. Add cancel-then-new emulation with fill-race abort.
3. Add remaining quantity to own-fill events.
4. Add `AtomicExecutionGroup` for cross-venue arbitrage.
5. Add group reservation and failure unwind.

Validation:

```text
cargo test --manifest-path rust/Cargo.toml -p eventcontracts-gateway replace partial_fill
cd python && python -m pytest tests/test_arbitrage_cross_venue.py
```

### Phase 5: Account-wide CEG, margin preemption, and observability

Files:

- new CEG process or service crate
- `rust/crates/gateway/src/lib.rs`
- `rust/crates/live-runner/src/main.rs`
- `python/src/eventcontracts/storage/parquet_store.py`
- `.github/workflows/quality.yml`

Steps:

1. Route all sleeves through one account-wide CEG.
2. Add self-match prevention across sleeves.
3. Add priority preemption/cancel-and-retry.
4. Add Prometheus metrics endpoint and alert rules.
5. Add raw private payload redaction and TTL.
6. Add SBOM generation and Python `pip-audit` if not already enforced.

Validation:

```text
cargo test --manifest-path rust/Cargo.toml --workspace
cd python && python -m pytest tests -q
curl http://localhost:9090/metrics
pip-audit -r python/requirements.txt --strict
```

## Live Deployment Verdict

The repo is materially safer after this pass and all local checks are green. It is not yet live-complete for nontrivial capital. The remaining blockers are not syntax-level fixes; they are execution architecture, lifecycle truth, account-wide netting, and observability work. The fastest safe path is to finish Phase 1 and Phase 2 before adding more strategies, because stale execution and incomplete restart truth are still the highest-impact ways to leak capital.
