# Eventcontracts Hyper Comprehensive Audit And Agent Implementation Spec

**Superseded:** Use `docs/v5-audit-and-agent-implementation-spec.md` as the
current implementation source of truth. This file is retained for historical
context only.

Date: 2026-05-28

Scope audited: Rust crates under `rust/crates/` with emphasis on `oms`, `risk`, `gateway`, `kalshi`, `runner`, `live-runner`, `model-runtime`, `runtime-hot`, `parity`, and Python strategy, risk, gateway, replay, storage, and config paths under `python/src/`, `python/tests/`, `configs/`, and `contracts/`.

## Executive Verdict

The codebase is substantially safer than the earlier audit baseline, but it is not yet the absolute fastest or the easiest possible strategy-development platform. It is now much closer to live-safe execution because the most dangerous market-state assumptions found in this pass were implemented before this document was drafted:

- Rust order, risk, gateway, and ledger state now distinguish `YES` and `NO` outcome exposure instead of collapsing both into one signed instrument position.
- Rust last-look now uses side-specific executable BBO when available, so a buy is checked against the latest ask and a sell against the latest bid instead of only a synthetic midpoint.
- Venue submit transport failure now leaves a local non-terminal `SubmitUnknown` order and open risk reservation instead of returning an error after mutating OMS state without synchronizing sleeve and portfolio state.
- Live submit now requires an explicit startup reconciliation policy.
- Unknown private venue events now halt the loop and bulk-cancel in live mode rather than becoming a soft log line.
- Remaining Python strategy market-order emissions found in promoted strategy modules were converted to IOC limits with attached market snapshots.
- Git hygiene now ignores `data/` and `*.parquet`; Rust CI now includes `cargo fmt --check`.

The remaining high-risk work is architectural rather than a small patch: full sleeve-config ingestion in Rust live execution, full venue reconciliation including positions and balances, stateful strategy ack/reject feedback, parity v2 across every promoted strategy, async execution worker decoupling, and stronger external replay and performance budgets.

## Current Verification

The current implementation was verified with:

- `cargo fmt --all -- --check`
- `cargo test --workspace` (135 Rust tests passed)
- `cargo clippy --workspace --all-targets -- -D warnings`
- `cargo run --quiet -p eventcontracts-parity --bin parity_check -- --strategy-spec ../contracts/examples/weather_threshold/strategy_spec.toml --cases ../contracts/parity/weather_threshold`
- `cargo bench --workspace --no-run`
- `python -m compileall -q src tests`
- `python -m ruff check src tests`
- `python -m mypy src/eventcontracts tests` (205 files, 0 errors)
- `python -m pytest tests -q` (240 passed, 9 skipped)
- `git diff --check`

Local `cargo audit` and `gitleaks` binaries were not installed, so those
checks are now enforced in CI via `.github/workflows/quality.yml`; a local
repository pattern scan for common secret/private-key strings found no new
literal credential material in the implementation files.

## Completion Update: Remaining Code-Level Fixes Implemented

This pass implemented the remaining safety controls from the spec that had a
well-defined insertion point in the current architecture:

- P1.1 implemented for live submit. `LastLookConfig` now has
  `require_executable_bbo` and `require_l1_depth`; `live-runner` enables both
  in live mode. Gateway rejects `no_executable_bbo`, `missing_l1_depth`, and
  `l1_depth_insufficient` before OMS mutation. See
  `rust/crates/gateway/src/lib.rs:408`, `rust/crates/gateway/src/lib.rs:1334`,
  and `rust/crates/live-runner/src/main.rs:390`.
- P1.2/P3.1 partially implemented for Rust promoted strategies. The
  `StrategyRuntime` trait now receives `on_intent_rejected` feedback for
  risk, last-look, stale, wrap, live-cap, and gateway rejects. Rust tennis and
  ONNX strategies clear emit-time pending state on this callback. See
  `rust/crates/runner/src/lib.rs:389`,
  `rust/crates/live-runner/src/main.rs:727`, and the new regression tests at
  `rust/crates/runner/src/lib.rs:1632` and `rust/crates/runner/src/lib.rs:1765`.
- P2.3 implemented as a live-promotion guard. Promoted Python strategy code is
  statically scanned for `OrderType.MARKET`; any reintroduction fails pytest.
  See `python/tests/test_strategy_promotion_guards.py:7`.
- P4.1 implemented. Rust live submit now requires `--sleeve-spec`, parses the
  sleeve TOML risk block, and refuses live mode if the sleeve risk file is not
  supplied. See `rust/crates/live-runner/src/main.rs:96`,
  `rust/crates/live-runner/src/main.rs:383`,
  `rust/crates/live-runner/src/main.rs:892`, and
  `rust/crates/live-runner/src/main.rs:969`.
- P6.7 implemented. Gateway now has a millisecond-resolution fill-velocity
  toxicity circuit breaker; when confirmed own fills exceed `N` fills inside
  the rolling window, it engages the kill switch and live-runner bulk-cancels
  remaining orders. See
  `rust/crates/gateway/src/lib.rs:427`,
  `rust/crates/gateway/src/lib.rs:1118`, and
  `rust/crates/live-runner/src/main.rs:601`.
- P6.8 implemented at the executable guard layer. Rust tracks book-derived L1
  displayed quantities via `record_book_bbo`; live last-look requires
  immediately executable orders to fit inside displayed L1. Python risk now
  rejects executable limits whose quantity exceeds snapshot L1 depth. See
  `rust/crates/risk/src/lib.rs:390`,
  `rust/crates/gateway/src/lib.rs:1348`, and
  `python/src/eventcontracts/risk/limits.py:183`.
- CI hardening implemented. The quality workflow now runs Rust/Python checks,
  parity, benchmark build, `cargo audit`, gitleaks, and Docker image build.
  See `.github/workflows/quality.yml:31`.

Residual items that still require a larger architectural change, not a safe
single-pass patch, are explicitly retained below: async execution worker
decoupling (P2.1), portfolio-native correlation metadata beyond CLI rules
(P4.2), atomic cross-venue execution groups (P6.1), exact exchange-native fee
and margin formulas (P6.2/P6.3), receipt-time enforcement across every
historical exogenous dataset (P6.4), explicit market lifecycle suspension gates
(P6.5), and priority-based margin preemption/cancel-and-retry (P6.9).

## Continuation Update: Phase Alpha And Quant-Trader Guards

Additional work landed after the Phase 6 quant-trader additions were appended:

- A1 is implemented. `PortfolioGuard::evaluate` now projects post-intent exposure instead of blindly adding notional, so closing sells can pass while exposure-increasing sells into shorts are still blocked. See `rust/crates/gateway/src/lib.rs:604` and `rust/crates/gateway/src/lib.rs:1347`.
- A2 is implemented. Adoption now validates venue-resting state against projected risk and portfolio policy, and engages the kill switch if adopted exposure is already in breach. See `rust/crates/risk/src/lib.rs:157`, `rust/crates/gateway/src/lib.rs:1076`, and `rust/crates/gateway/src/lib.rs:1088`.
- P6.6 / self-matching is implemented at the gateway. Incoming orders that would cross an existing own open order on the same instrument and outcome side are rejected before last-look or venue dispatch. See `rust/crates/gateway/src/lib.rs:58` and `rust/crates/gateway/src/lib.rs:1149`.
- G1/G2 were already satisfied in the current tree: `Makefile:28` runs `cargo test --workspace`, and `.github/workflows/quality.yml:30` runs the parity gate through `make parity-check`.

New regression coverage:

- `portfolio_allows_sell_to_close_when_buy_would_exceed_cap`
- `portfolio_sell_into_short_increments_gross`
- `adoption_that_breaches_projected_risk_engages_kill_switch`
- `adoption_within_projected_risk_does_not_engage_kill_switch`
- `gateway_rejects_self_crossing_buy_against_own_sell`

Remaining Phase 6 quant-trader blockers after this implementation pass:

- P6.9 margin lockup and opportunity cost: priority-based preemption/cancel of low-conviction resting orders.
- P6.1/P6.2/P6.3: atomic cross-venue execution groups, exact venue fee curves, and exchange-native margin netting still need dedicated design work.

## Implemented In This Pass

### I1. Outcome-Side Accounting Is Now Explicit

Files and logic:

- `rust/crates/oms/src/lib.rs:59`: new shared `OutcomeSide`.
- `rust/crates/oms/src/lib.rs:103`: `Order` now stores `outcome_side`.
- `rust/crates/risk/src/lib.rs:82`: `IntentSnapshot` now stores `outcome_side`.
- `rust/crates/risk/src/lib.rs:272`: `outcome_position_key(instrument_id, outcome_side)`.
- `rust/crates/gateway/src/lib.rs:605`: `RestingOrderSnapshot` now carries `outcome_side`.
- `rust/crates/gateway/src/lib.rs:621`: ledger entries now carry `outcome_side`.
- `rust/crates/gateway/src/lib.rs:909`: fill accounting updates the side-specific position key.
- `rust/crates/kalshi/src/rest.rs:209`: REST adoption preserves Kalshi `side` as outcome side.

Prior vulnerability scenario:

1. Strategy buys `YES` on `kalshi:M-1`.
2. Later another strategy buys `NO` on the same market.
3. The OMS stored only `side = Buy/Sell`; risk and gateway keyed position by `instrument_id` only.
4. The two economically distinct positions were treated as the same long position.
5. Risk, gross exposure, PnL, and portfolio accounting could approve trades based on false netting.

Remediation now implemented:

- Exposure is keyed as `instrument|yes` or `instrument|no`.
- Fill ledger entries preserve outcome side.
- Risk projected position checks use the outcome-specific position key.
- The integration test `fill_accounting_keeps_yes_and_no_positions_separate` locks this behavior.

### I2. Side-Specific BBO Last-Look

Files and logic:

- `rust/crates/risk/src/lib.rs:65`: `best_bid_ticks`.
- `rust/crates/risk/src/lib.rs:69`: `best_ask_ticks`.
- `rust/crates/risk/src/lib.rs:280`: `record_quote_bbo`.
- `rust/crates/gateway/src/lib.rs:995`: `last_look_check`.
- `rust/crates/gateway/src/lib.rs:1045`: buy last-look reads current ask.
- `rust/crates/gateway/src/lib.rs:1051`: sell last-look reads current bid.
- `rust/crates/live-runner/src/main.rs:573`: live runner records executable side-specific BBO.

Prior vulnerability scenario:

1. A market has `bid = 0.10`, `ask = 0.90`.
2. A strategy buys at the observed ask `0.90`.
3. Old last-look compared `0.90` to synthetic midpoint `0.50`.
4. The order was rejected as "moved" despite matching actual top-of-book.
5. Conversely, a stale side could pass if midpoint stayed stable.

Remediation now implemented:

- Last-look compares buy limits to ask and sell limits to bid when BBO is present.
- The midpoint remains only a backward-compatible fallback.
- The regression test `last_look_compares_buy_against_side_specific_ask_not_mid` locks this.

### I3. Submit Transport Unknown Is Treated As Risk-Open

Files and logic:

- `rust/crates/oms/src/lib.rs:79`: new `OrderState::SubmitUnknown`.
- `rust/crates/oms/src/lib.rs:197`: legal transitions from `Submitted` into `SubmitUnknown`, then into venue-confirmed terminal or live states.
- `rust/crates/gateway/src/lib.rs:826`: venue submit error transitions to `SubmitUnknown`.
- `rust/crates/gateway/src/lib.rs:986`: `sync_runtime_state` keeps sleeve open orders and portfolio reservations in sync after mutation.

Prior vulnerability scenario:

1. Gateway opens an order locally and transitions it to `Submitted`.
2. REST submit times out after the request may have reached the venue.
3. Old code returned `Err` before syncing sleeve open-order count and portfolio reservations.
4. Local risk could undercount exposure while the venue might still have a live order.

Remediation now implemented:

- Transport error after local submission is treated as unknown, not rejected.
- The order stays non-terminal and risk-open.
- Idempotency record remains reserved and incomplete.
- The regression test `submit_transport_error_keeps_risk_reserved_as_submit_unknown` locks this.

### I4. Live Submit Requires Startup Venue-Truth Policy

Files and logic:

- `rust/crates/live-runner/src/main.rs:744`: `--live-submit` now requires either `--reconcile-on-start` or `--cancel-orphans-on-start`.
- `rust/crates/live-runner/src/main.rs:336`: startup query uses `KalshiRest::list_open_orders`.
- `rust/crates/kalshi/src/rest.rs:420`: `list_open_orders` now paginates.

Prior vulnerability scenario:

1. A previous process left resting orders at the venue.
2. A new live process started without querying venue truth.
3. The gateway risk snapshot began empty and admitted new orders against incomplete exposure.

Remediation now implemented:

- Live submit refuses to run unless the operator chooses startup reconciliation or cancellation.
- Open-order REST listing paginates instead of reading only one page.

### I5. Unknown Private Venue Events Halt Live Trading

Files and logic:

- `rust/crates/live-runner/src/main.rs:534`: unknown private event triggers kill switch and live bulk cancel.
- `rust/crates/live-runner/src/main.rs:954`: private event application returns a halt flag when an own fill/order update references an unknown order.

Prior vulnerability scenario:

1. Venue sends an own fill for an order missing from local OMS.
2. Old code incremented an error metric and kept trading.
3. Local position and cash truth were already divergent.

Remediation now implemented:

- Unknown private event is treated as reconciliation-required.
- Live mode attempts `cancel_all` and stops.

### I6. Promoted Python Strategy Market Orders Removed

Files and logic:

- `python/src/eventcontracts/plugins/strategies/arbitrage_cross_venue.py:121`: IOC limit with `market_snapshot=this.snapshot`.
- `python/src/eventcontracts/plugins/strategies/politics_primary_momentum.py:128`: IOC limit at latest state-B ask with `market_snapshot=snapshot`.
- `python/src/eventcontracts/plugins/strategies/sports_hole_by_hole_pin.py:176`: IOC limit at latest YES ask with `market_snapshot=state.last_yes_snapshot`.

Prior vulnerability scenario:

1. Strategy generated a market order from model or cross-market signal.
2. Execution had no bounded price and sometimes no attached market-data snapshot.
3. Risk could reject in many paths, but strategy intent itself encoded unbounded slippage.

Remediation now implemented:

- These strategy decisions are now bounded IOC limits.
- They carry the exact quote-derived `MarketSnapshot` used to choose the price.

### I7. Basic Security And CI Hygiene

Files and logic:

- `.gitignore:30`: ignore `data/`.
- `.gitignore:31`: ignore `*.parquet`.
- `Makefile:27`: Rust quality includes `cargo fmt --all -- --check`.

Prior vulnerability scenario:

1. Raw captures or parquet datasets appear under `data/`.
2. Developer accidentally stages large or sensitive research data.
3. CI allows formatting drift in Rust.

Remediation now implemented:

- Local data artifacts are ignored at the git level.
- Formatting is now part of the standard Rust check.

## Phase 1: Real Data Vs Expected Data

### Finding P1.1: Rust Live Pricing Is Improved But Still Has A Mark Fallback

Current logic:

- Live quotes are normalized and projected into `StrategyEvent::Quote`.
- `rust/crates/live-runner/src/main.rs:573` records side-specific BBO via `record_quote_bbo`.
- `rust/crates/gateway/src/lib.rs:1045` and `1051` use executable ask/bid when available.
- `rust/crates/gateway/src/lib.rs:1007` still falls back to `mark_price_ticks` when BBO is missing.

Vulnerability scenario:

1. A quote message loses one side, or a non-quote event triggers a stale decision.
2. The BBO map is missing but a mark remains from compatibility paths or a test/source that only writes mark.
3. Last-look can compare against midpoint instead of real executable price.

Remediation:

- Make side-specific BBO mandatory for live submit.
- Add a `LastLookConfig.require_executable_bbo = true` default for live, optional only for deterministic tests.
- Split mark usage from executable last-look: mark may be used for gross exposure valuation, never as the submit-time price reference.

### Finding P1.2: Python Risk Is Strong, But Strategy-Local State Still Assumes Some Success

Current logic:

- `python/src/eventcontracts/plugins/strategies/microstructure_obi_scalper.py:152` records `_open_buy_orders` before exchange or gateway confirmation.
- `python/src/eventcontracts/plugins/strategies/microstructure_obi_scalper.py:171` removes it before cancel confirmation.
- `python/src/eventcontracts/plugins/strategies/weather_temperature_arbitrage.py:253` increments `_active_notional` when emitting an intent.
- `python/src/eventcontracts/plugins/strategies/sports_tennis_xgboost.py:129` sets `pending_client_order_id` before downstream risk/gateway acceptance.
- Rust runner has the same pattern in `rust/crates/runner/src/lib.rs:653`.

Vulnerability scenario:

1. Strategy emits an order.
2. Risk rejects it, last-look rejects it, or gateway drops it as stale.
3. Strategy-local state still believes the order is pending.
4. It suppresses valid future opportunities or fails to release capital until an own-order update that will never arrive.

Remediation:

- Introduce a strategy feedback event: `IntentRejected`, `IntentAccepted`, `VenueAckUnknown`, `VenueAccepted`, `VenueTerminal`.
- Runner must feed these events back into strategies after risk/gateway processing.
- Strategy-local pending state must only advance from feedback events, not from emit-time assumptions.
- Tests must replay a risk rejection and assert strategy state is released.

### Finding P1.3: Queue Position Is Still Modeled, Not Observed

Current logic:

- `python/src/eventcontracts/execution/queue.py` contains queue estimators, including optimistic front-of-queue models.
- Market simulator uses these estimators for backtests.
- Live execution has no venue-confirmed queue position or fill-probability calibration.

Vulnerability scenario:

1. Backtest assumes a passive order is near the front of queue.
2. In live trading, the order is behind large hidden/resting interest.
3. Simulated fill probability is inflated, so strategy appears profitable while live fills occur only when adverse selection is highest.

Remediation:

- Label all queue estimates as simulation-only.
- Require a venue-calibrated fill model before any passive microstructure strategy can be live-approved.
- Add replay metrics: quote-to-fill latency, fill probability by queue-depth proxy, adverse move after fill.

## Phase 2: Adverse Selection And Toxic Flow

### Finding P2.1: Blocking REST Submit In The Data Loop Creates Stale Decisions

Current logic:

- `rust/crates/kalshi/src/venue_client.rs:62` uses `block_in_place` to submit REST orders.
- `rust/crates/live-runner/src/main.rs:678` calls `gateway.process_batch` inline from the WebSocket processing loop.

Vulnerability scenario:

1. Strategy emits an order.
2. REST submit stalls or retries.
3. WebSocket processing pauses while the market continues moving.
4. Subsequent strategy decisions are based on stale local state.

Remediation:

- Split live runner into three workers:
  - market data ingestion and normalization,
  - strategy and risk decisioning,
  - execution worker with send-time last-look.
- Use bounded channels and backpressure.
- Execution worker must read latest BBO atomically at send time, not from the decision snapshot alone.

### Finding P2.2: Model Correctness Is Not The Same As Trade Edge

Current logic:

- Strategies such as CPI, tennis, and golf infer fair probability from external data.
- Risk checks staleness and snapshots, but it does not require a post-model market-impact or "already priced in" check.

Vulnerability scenario:

1. Model predicts correctly that event probability is high.
2. The market has already moved to the same probability.
3. Strategy buys the ask because prediction exceeds threshold, but edge after spread and fees is gone.

Remediation:

- Every model strategy must compute executable edge: `fair_value - ask` for buys, `bid - fair_value` for sells.
- Edge must include fees, expected slippage, stale-data penalty, and adverse-selection buffer.
- Add config fields: `min_executable_edge_bps`, `max_spread_bps`, `max_signal_age_ms`, `min_quote_persistence_ms`.

### Finding P2.3: Market Orders Are Still Allowed By Domain Types

Current logic:

- Python risk can reject market orders when policy disallows them in `python/src/eventcontracts/risk/limits.py:120`.
- Gateway last-look rejects unbounded Python market orders in `python/src/eventcontracts/gateway/base.py:188`.
- Domain types still allow `OrderType.MARKET`.

Vulnerability scenario:

1. A new strategy emits a market order.
2. A permissive sleeve or test profile allows it.
3. Slippage becomes unbounded in a thin event contract book.

Remediation:

- Keep `OrderType.MARKET` in domain types only for replay/import compatibility.
- Add a live-promotion linter that fails any strategy/config pair that can emit `MARKET`.
- Require all live orders to be priced IOC/GTC limits with market snapshots.

## Phase 3: Edge Cases And Adversarial Mechanics

### Finding P3.1: Partial Fill Handling Is Stronger In Rust, But Strategy State Still Needs Feedback

Current logic:

- Rust OMS handles partial fills and overfill rejection.
- Gateway position update uses fill price and realized PnL.
- Strategies still often treat "one emitted order" as "market locked".

Vulnerability scenario:

1. Order fills 10 percent.
2. Remainder rests or is canceled later.
3. Strategy either thinks it has full exposure or suppresses all future orders.

Remediation:

- Strategies must receive `OwnFill` and `OwnOrderUpdate` feedback through a uniform runtime path.
- Add per-strategy `desired_position` vs `confirmed_position` state.
- Add tests for 10 percent partial fill, cancel remainder, and immediate re-signal.

### Finding P3.2: Market Reversal Abort Is Only Partially Covered

Current logic:

- Gateway last-look catches stale quotes and price movement.
- Live loop invalidates extremely wide quotes.
- There is no strategy-level "abort if market crossed against me during queue wait" for resting orders.

Vulnerability scenario:

1. Strategy places a passive limit.
2. New public trades indicate adverse information.
3. The order remains resting and becomes free optionality for informed traders.

Remediation:

- Add resting-order TTL per priority tier.
- Add cancel-on-adverse-move logic: if latest BBO moves against order by `x` bps or new trade crosses through fair value, cancel.
- Gateway should own stale-resting-order cancellation independent of strategy.

### Finding P3.3: Fake Liquidity Can Still Drive Microstructure Features

Current logic:

- `python/src/eventcontracts/plugins/strategies/microstructure_obi_scalper.py` uses L1 book imbalance.
- Order book depth and queue simulation can be spoofed by temporary orders.

Vulnerability scenario:

1. Adversary posts large top-of-book size to distort imbalance.
2. Strategy emits buy.
3. Adversary cancels spoofed size and sells into the triggered order.

Remediation:

- Require persistence-weighted imbalance: only count size that survives `min_quote_persistence_ms`.
- Add cancellation-rate features.
- Require trade confirmation or multi-level depth confirmation before order emission.
- Add adversarial replay fixtures with quote stuffing, size flicker, and spoof-cancel patterns.

## Phase 4: Risk And Capital Allocation

### Finding P4.1: Rust Live Runner Still Does Not Load Sleeve Risk Configs

Current logic:

- `rust/crates/live-runner/src/main.rs:321` and `326` construct `RiskGate::new(default_limits())`.
- Sleeve configs exist under `configs/sleeves/*.toml`.
- CLI has `--strategy-spec` but no required `--sleeve-spec`.

Vulnerability scenario:

1. Operator configures a sleeve cap in TOML.
2. Rust live runner ignores it and uses hardcoded defaults.
3. Actual live risk differs from research/backtest/paper.

Remediation:

- Add `--sleeve-spec`.
- Parse the same sleeve schema Python uses.
- Refuse live submit if risk limits are defaults unless `--allow-default-risk-for-demo` and `KALSHI_ENV=demo`.
- Add parity test: Python sleeve risk vs Rust sleeve risk for the same TOML.

### Finding P4.2: Portfolio Correlation Is CLI-Based, Not Strategy-Native

Current logic:

- `rust/crates/gateway/src/lib.rs:564` has portfolio guard group checks.
- `rust/crates/live-runner/src/main.rs:778` builds group policy from CLI flags.

Vulnerability scenario:

1. CPI, NFP, Fed, and correlated macro markets trigger together.
2. Operator forgot a CLI `portfolio-group-rule`.
3. Each strategy passes its local cap, but portfolio risk is over-allocated to one macro factor.

Remediation:

- Add strategy/sleeve metadata: `risk_group`, `factor_tags`, `correlation_cluster`, `max_cluster_gross`.
- Allocator must consume these tags before gateway admission.
- CI must fail live promotion for strategies missing group metadata.

## Phase 5: No-Assumptions Verdict

The codebase still contains the following assumption classes:

1. "Emitted means pending" in strategy-local state.
   - Files: microstructure OBI, weather arbitrage, tennis XGBoost, Rust tennis strategy.
   - Required refactor: feedback events from risk/gateway/venue.

2. "Model fair value means executable edge."
   - Files: model-driven strategy modules.
   - Required refactor: executable-edge contract for every live strategy.

3. "Midpoint is acceptable for execution validation."
   - Files: some compatibility and test paths still write only `mark_price_ticks`.
   - Required refactor: live last-look requires executable BBO.

4. "Queue position can be inferred from public depth."
   - Files: queue simulator and microstructure strategies.
   - Required refactor: venue-calibrated fill model and spoof-resilient features.

5. "Open orders are enough for startup reconciliation."
   - Files: live runner and Kalshi REST reconciliation.
   - Required refactor: reconcile open orders, fills since checkpoint, positions, balance, and venue order IDs.

6. "Inline REST submit is fast enough."
   - Files: Kalshi venue client and live runner.
   - Required refactor: async execution worker and latest-BBO shared state.

7. "Parity can be one fixture per strategy."
   - Files: `rust/crates/parity`, `contracts/parity/weather_threshold`.
   - Required refactor: stateful parity v2 with full envelope, risk, gateway verdicts, and strategy state.

8. "Promoted strategy set is obvious from registry."
   - Files: Python strategy registry, Rust runner registry, configs.
   - Required refactor: live-promotion manifest that links strategy spec, sleeve spec, parity cases, external replay fixtures, and deployment target.

## Phase 6: Quant Trader & Alpha Execution Vulnerabilities

The codebase has primarily been audited from a systems/safety perspective. Evaluating it as a high-level quantitative trader reveals several systemic alpha-leakage and capital-efficiency blind spots.

### Finding P6.1: Cross-Venue Legging Risk (Execution)

Current logic:

- `arbitrage_cross_venue.toml` and corresponding Python strategies dispatch independent intents for disjoint venues (e.g., Kalshi and Polymarket).
- The gateway and risk engine treat each intent as a standalone bounded limit order.

Vulnerability scenario:

1. Strategy identifies a cross-market arbitrage (Kalshi YES at 40c, Polymarket NO at 65c).
2. Strategy emits two intents simultaneously.
3. Kalshi intent fills instantly. Polymarket intent is rejected due to a nonce collision, network delay, or a moving book.
4. The strategy is now naked long YES on Kalshi, carrying unintended directional delta instead of a locked-in 5c spread.

Remediation:

- Introduce `LinkedIntents` or `AtomicExecutionGroup` in the domain model.
- If execution is inherently non-atomic (across two exchanges), the strategy MUST explicitly model the legging risk and the risk engine MUST reserve capital buffers for the unhedged leg delta.

### Finding P6.2: Dynamic Fee Modeling & Discretization Bias (Pricing)

Current logic:

- The PnL tracker and strategy simulators assume flat basis point execution fees or static slippage logic.
- Price rounding uses standard arithmetic functions without side-specific bias awareness.

Vulnerability scenario:

1. **Kalshi Dynamic Fees:** Kalshi V2 taker fees are dynamic and non-linear based on the contract price (e.g., deeply out-of-the-money options cost significantly more as a percentage of premium). A flat bps fee model will cause the system to drastically over-trade tail probabilities and under-trade near-50/50 contracts.
2. **Discretization Bias:** If the model computes a continuous fair value of `0.456`, rounding to nearest (`0.46`) for a bid creates artificial adverse selection. In discrete cent-ticks, strategies lose their statistical edge if they do not round pessimistically (floor for bids, ceil for asks).

Remediation:

- Implement the exact Kalshi V2 taker fee curve in `rust/crates/risk` and evaluate the `executable_edge` *after* the precise fee deduction.
- Enforce strict `floor`/`ceil` discretization rules depending on the order side inside the strategy context before intent emission.

### Finding P6.3: Capital Velocity & Margin Netting (Capital)

Current logic:

- `SleeveState` tracks exposure based on gross notional outlays.
- Holding `YES` and holding `NO` in the same market are tracked via outcome sides but capital usage is treated linearly.

Vulnerability scenario:

1. An arbitrage strategy buys 100 YES at 40c ($40) and 100 NO at 55c ($55).
2. The risk engine reserves $95 of capital.
3. However, Kalshi natively offsets margin for mutually exclusive positions. Holding 100 YES and 100 NO guarantees a $100 payout, requiring $0 additional margin on the exchange.
4. The local risk engine artificially starves the sleeve of capital, drastically limiting the turnover and ROE of the arbitrage strategy.

Remediation:

- The `Allocator` and `SleeveRiskGate` must implement exchange-native margin netting formulas. Mutually exclusive outcome exposure must offset capital reservations to reflect true exchange purchasing power.

### Finding P6.4: Lookahead Bias in Exogenous Snapshots (Data)

Current logic:

- The `TennisMatchSnapshot` uses a cron job to fetch external data (Elo ratings, player statistics) at $T_0$.
- Backtesting joins exogenous datasets against market tick data based on matching market IDs and general event windows.

Vulnerability scenario:

1. The external cron runs at 12:00:00. The external API response takes 5 seconds and returns data physically published by the provider at 12:00:05.
2. The backtester joins this snapshot to market data starting at 12:00:01.
3. The model achieves unrealistic PnL because it is training on data that was physically unavailable to the live runner at that exact microsecond.

Remediation:

- Strict enforcement of `receipt_time` (the microsecond the external byte payload hit the local NIC) vs `event_time` (the logical time the data represents).
- The historical replay engine MUST only expose exogenous snapshots to the strategy if `snapshot.receipt_time <= current_market_tick.receipt_time`.

### Finding P6.5: Market Lifecycle & Suspension Awareness (State)

Current logic:

- Normalization handles trade, quote, and generic lifecycle events, but strategies primarily evaluate order books and trades.

Vulnerability scenario:

1. A tennis market enters an "In-Running" pause or suspension due to VAR (Video Assistant Referee) review.
2. The WebSocket BBO remains static at the last traded price.
3. A strategy sees the static BBO, believes a profitable threshold is met, and attempts to execute.
4. The venue rejects the order, causing unnecessary gateway spam, or worse, the strategy widens its own internal quotes assuming the market is merely illiquid rather than physically halted.

Remediation:

- Add explicit `MarketState::Suspended` and `MarketState::InRunning` enums.
- The `SleeveRiskGate` must automatically reject intents for suspended markets.
- Strategies must clear internal signal buffers when a market resumes from a suspension to avoid reacting to pre-suspension stale data.

### Finding P6.6: Self-Matching & Wash Trading Across Sleeves

Current logic:

- The `SleeveRiskGate` evaluates intent validity in isolation based on a specific sleeve's TOML limits. There is no cross-sleeve order book or CEG netting protocol.

Vulnerability scenario:

1. Sleeve A runs a momentum strategy and emits a Buy YES at 50c.
2. Sleeve B runs a mean-reversion strategy and emits a Sell YES at 50c.
3. Both intents pass their respective risk gates and are routed to the exchange.
4. The exchange matches Sleeve A against Sleeve B. We pay double taker fees to effectively pass risk between our own accounts, completely eroding alpha. Furthermore, this can trigger automated wash trading flags from the venue's compliance department.

Remediation:

- The CEG must implement an internal Netting/Crossing engine. Before routing any order to an external venue, the CEG should attempt to internally cross opposing flow from different sleeves at the midpoint, saving the spread and taker fees.
- If crossing isn't supported, the CEG must strictly reject self-matching intents to prevent wash trading violations.

### Finding P6.7: Correlated Pick-Offs (Toxicity Contagion)

Current logic:

- Rate limiters (`governor` crate) apply token bucket algorithms based on raw API request volume to prevent HTTP 429s. Risk limits cap gross exposure per market.

Vulnerability scenario:

1. A massive macroeconomic data release (e.g., unexpected CPI print) instantly shifts fair value across 50 correlated contracts.
2. The local market data feed experiences microsecond jitter, meaning our strategy hasn't updated its fair values or canceled its passive resting limits.
3. A toxic, highly-informed taker sweeps the entire exchange book, filling our passive limits across all 50 markets simultaneously.
4. Each individual fill passes the per-market risk gate, but the portfolio suffers a catastrophic correlation loss because the "toxicity" wasn't caught globally.

Remediation:

- Implement a "Toxicity Circuit Breaker" in the CEG. If `N` resting orders are filled within `T` milliseconds across any set of markets, the CEG must instantly yank (Cancel All) all remaining resting liquidity and block new limit placement until a cooldown period elapses.

### Finding P6.8: Market Impact & Book Sweeping

Current logic:

- Promoted strategies emit IOC limit orders with sizing derived from available capital and arbitrary conviction multipliers, assuming execution at the specific L1 BBO.

Vulnerability scenario:

1. Strategy L1 BBO indicates Kalshi YES at 50c. The strategy calculates edge and fires an IOC Buy for 5,000 contracts at a limit of 52c to ensure execution.
2. The venue order book only has 10 contracts available at 50c, 100 at 51c, and 5000 at 52c.
3. Because the order is a 52c limit, it sweeps the book. The strategy pays a Volume-Weighted Average Price (VWAP) of 51.98c instead of the targeted 50c. The true edge is completely destroyed by market impact.

Remediation:

- Strategies must compute sizing specifically against *displayed depth at the target price level*, not just a conviction multiplier.
- The `IntentSnapshot` must include an explicit `max_sweep_levels` or a `target_vwap`. If the requested size exceeds the displayed L1 liquidity at the required price, the strategy must either emit a smaller size (Sniper logic) or explicitly accept the VWAP degradation in its edge calculation.

### Finding P6.9: Margin Lockup & Opportunity Cost

Current logic:

- Passive resting limit orders correctly reserve capital via the `Allocator` to prevent over-leverage if all resting orders were to fill.

Vulnerability scenario:

1. A market-making strategy posts 500 passive limit orders wide of the spread across various contracts.
2. This reserves 100% of the sleeve's allocated risk capital.
3. A high-conviction momentum or arbitrage signal fires. It requires capital to cross the spread and lock in guaranteed edge.
4. The Risk Gate rejects the high-conviction order for "Insufficient Funds" because the capital is tied up in low-conviction, unfilled passive orders.

Remediation:

- Introduce "Margin Tiering" or Preemption logic in the Risk engine. Orders must carry a `conviction_score` or `priority_tier`.
- If a Tier 1 (Arbitrage) intent arrives and capital is exhausted, the CEG should automatically cancel Tier 3 (Passive) resting orders to free up capital rather than rejecting the Tier 1 alpha.

## Agent Implementation Order

### P0: Preserve Current Green State

Before changing anything:

1. Run `git status --short`.
2. Do not revert unrelated user changes.
3. Run `cargo fmt --all -- --check`, `cargo test --workspace`, `cargo clippy --workspace --all-targets -- -D warnings`.
4. Run `cd python && python -m ruff check src tests && python -m mypy src/eventcontracts tests && python -m pytest tests -q`.
5. Record baseline results.

Acceptance:

- All checks remain green before each phase.
- Any failure caused by the phase is fixed before moving on.

### P1: Make Sleeve Spec Mandatory For Rust Live Submit

Implement:

- Add `--sleeve-spec <path>` to `rust/crates/live-runner/src/main.rs`.
- Add Rust parser for `configs/sleeves/*.toml` or reuse existing spec parsing patterns.
- Build `RiskLimits`, sleeve id, capital cap, and portfolio group metadata from the sleeve file.
- Refuse `--live-submit` if no sleeve spec is provided.
- Allow no sleeve spec only in paper mode or explicit demo-only override.

**Implementation Notes:**
- Utilize the `toml` crate to deserialize directly into a new `SleeveSpec` struct within `rust/crates/contracts/src/specs.rs`.
- Map the parsed `SleeveSpec` bounds directly onto the `eventcontracts_risk::SleeveState::new()` constructor.
- Any CLI flags for risk limits should only be treated as strict overrides (must be tighter than the TOML config, never looser).

Tests:

- Unit test parser against `configs/sleeves/weather-kalshi-paper-a.toml`.
- Live-runner preflight test rejects live submit without sleeve spec.
- Golden parity test verifies Python and Rust parse equivalent risk caps.

### P2: Full Venue Reconciliation

Implement:

- Startup reconciliation must fetch:
  - all open/resting/partially filled orders,
  - recent fills since last local checkpoint,
  - positions,
  - available balance/cash if API exposes it.
- Adopt known client orders into OMS.
- For unknown client orders, either adopt with full metadata or refuse/cancel based on operator policy.
- Seed `KalshiVenueClient` venue-order ID cache during adoption or make cancel path accept venue order ID from OMS.

**Implementation Notes:**
- In `rust/crates/kalshi/src/rest.rs`, implement `list_portfolio_orders` and `list_portfolio_positions` with transparent pagination logic using `futures::stream::try_unfold`.
- Prevent pagination blocking by offloading the HTTP requests to an async `tokio::spawn` task that communicates results back via an `mpsc` channel.
- If a fill occurred while the system was offline, emit it directly onto the NATS `fills.{sleeve_id}` topic to sync strategy states identically to live execution.

Tests:

- Add fixture files under `contracts/replay/kalshi/`.
- Test multi-page open-order adoption.
- Test unknown fill after startup produces halt unless reconciliation can apply it.
- Test adopted order can be canceled without REST lookup fallback.

### P3: Runtime Feedback Events For Strategy State

Implement:

- Add feedback events in both Python and Rust:
  - `IntentRejected`
  - `IntentAccepted`
  - `VenueSubmitUnknown`
  - `VenueAcked`
  - `VenueTerminal`
- Runner/gateway must emit these after every decision.
- Strategies must release or advance local pending state only from feedback.

**Implementation Notes:**
- In `rust/crates/contracts/src/events.rs`, introduce an `IntentFeedbackEvent` enum containing these states.
- The CEG (`ceg-daemon`) must publish to a new NATS topic: `feedback.{sleeve_id}`.
- Strategy context objects (`rust/crates/runner/src/context.rs`) must listen to this topic. Completely eradicate `self.pending_intents.push(intent)` from the emit path; pending intent state is ONLY appended when a `VenueAcked` or `IntentAccepted` message is received over NATS.

Tests:

- For OBI, tennis, and weather arbitrage:
  - emit intent,
  - force risk rejection,
  - assert local pending state is released,
  - emit fresh valid signal and assert it can trade.
- For partial fill:
  - apply 10 percent fill,
  - cancel rest,
  - assert confirmed position is 10 percent, not full size.

### P4: Async Execution Worker

Implement:

- Replace inline `gateway.process_batch` in the WebSocket loop with a bounded execution channel.
- Execution worker owns venue client.
- Market data worker updates an atomic/latest BBO cache.
- Execution worker performs final last-look immediately before submit.
- Add backpressure metrics and drop policy for stale non-cancel intents.

**Implementation Notes:**
- Decouple the monolithic `live-runner` into `ceg-daemon` (execution) and `strategy-worker` (decision). 
- If running in a single binary for local tests, use `flume` or `tokio::sync::mpsc` with a strict `buffer_size` (e.g., 1024). Once the buffer fills, older non-cancel intents MUST be dropped (ShedLoad) and logged.
- The execution worker must perform send-time last-look validation using an `Arc<RwLock<BboMap>>` updated by the ingestor.

Tests:

- Replay high-rate external WS fixture and assert no data-loop stalls while REST submit fixture sleeps.
- Measure end-to-end p50/p95/p99 latency.
- Assert stale intents are dropped before submit under artificial REST delay.

### P5: Live-Promotion Manifest And Parity V2

Implement:

- Add a manifest per promoted live strategy:
  - strategy spec,
  - sleeve spec,
  - required fixtures,
  - parity cases,
  - external replay files,
  - minimum checks.
- Extend parity cases to stateful sequences.
- Compare:
  - decision payloads,
  - priority tier,
  - TTL,
  - audit metadata,
  - risk verdict,
  - gateway verdict,
  - final strategy state.

**Implementation Notes:**
- Create `rust/crates/parity/src/stateful_runner.rs`. Unlike static single-event parity, stateful parity feeds a sequence of `(NormalizedEvent, MockVenueResponse)` pairs, verifying internal state (like open orders and balance) matches between Python and Rust at every step.
- JSON serialization ordering and canonicalization must match exactly to avoid artificial parity drift.

Tests:

- CI fails if a live-promoted strategy has no parity directory.
- CI fails if parity case count is zero.
- CI fails if expected decisions omit TTL/audit fields.

### P6: Spoof-Resilient Microstructure

Implement:

- Add persistence-weighted order book features.
- Add cancel-rate and quote-flicker features.
- Add max spread and min displayed size duration requirements.
- Disable live promotion for pure L1 imbalance strategies until adversarial replay passes.

**Implementation Notes:**
- Implement Exponential Moving Average (EWMA) of book depth in `rust/crates/feature-builder`.
- The EWMA must be *time-weighted* (using nanosecond deltas between ticks) rather than *event-weighted*, so an adversary spamming updates in a single millisecond cannot artificially inflate the depth average.

Tests:

- Fixture: large ask disappears before signal.
- Fixture: top bid spoof creates false imbalance.
- Fixture: quote-stuffing produces sequence gaps.
- Assert no trades fire without persistent executable liquidity.

### P7: Performance Budgeting

Implement:

- Add Criterion benchmarks for:
  - normalization,
  - runtime-hot projection,
  - risk evaluate,
  - gateway last-look,
  - end-to-end external replay.
- Move audit hashing and JSON canonicalization off the critical submit path where possible.
- Remove avoidable `Vec` allocations in ONNX scoring (`rust/crates/model-runtime/src/lib.rs:124`) and feature hashing (`rust/crates/runner/src/lib.rs:1109`) where safe.

**Implementation Notes:**
- Use `cargo-criterion` to define a baseline budget. 
- Eliminate all heap allocations of `String` during event normalization (e.g., `client_order_id`, `market_id`). Use `[u8; 16]` for IDs and `smol_str::SmolStr` for small tickers to bypass global allocator locks.
- Run `cargo flamegraph` on the end-to-end replay path to verify that the `malloc` / `free` cycles are completely removed from the hot path.

Tests:

- CI should run `cargo bench --workspace --no-run`.
- Nightly or scheduled perf job should run real benches and compare against saved budgets.
- Fail if p99 end-to-end replay latency regresses by more than agreed threshold.

### P8: Data Security And Deployment

Implement:

- Add secret scanning in CI.
- Add Docker image build in CI.
- Pin Docker base images by digest.
- Redact or encrypt private raw payload storage for account/order/fill data.
- Add SBOM and vulnerability scan.

**Implementation Notes:**
- Integrate `gitleaks` into the `.github/workflows/quality.yml` pipeline.
- Implement `cargo-audit` to detect CVEs in Rust dependencies.
- Use `gcr.io/distroless/cc` as the base Docker image for Rust services to minimize attack surface area.

Tests:

- CI fails on staged `.env`, key material, PEM, parquet, or `data/`.
- Docker build runs as non-root and starts `eventcontracts-live-runner --help`.
- Private-event storage tests assert sensitive fields are redacted or encrypted.

## External Data Testing Rules

Do not add production-only fake hooks. Use the following pattern:

1. Place real or realistically captured fixtures under `contracts/replay/` or `python/tests/fixtures/`.
2. Feed fixtures through the normal normalizer, runtime-hot projector, runner, risk, and gateway.
3. Assert on observable outputs:
   - emitted decisions,
   - risk verdicts,
   - gateway ACK/error,
   - OMS state,
   - positions,
   - ledger entries,
   - metrics.
4. Avoid strategy-specific "test mode" branches in production modules.
5. If a fake venue is needed, keep it in tests only and make it implement the same public trait/protocol as the real venue.

Minimum efficacy suite after all phases:

```text
cargo fmt --all -- --check
cargo test --workspace
cargo clippy --workspace --all-targets -- -D warnings
cargo run --quiet -p eventcontracts-parity --bin parity_check -- --all-promoted
cargo bench --workspace --no-run
cd python && python -m compileall -q src tests
cd python && python -m ruff check src tests
cd python && python -m mypy src/eventcontracts tests
cd python && python -m pytest tests -q
docker build .
secret scan
external replay suite
```

## Final Live-Readiness Gate

No strategy is live-approved until all are true:

- It emits only bounded limit orders.
- It attaches a fresh, side-correct market snapshot to every order.
- It has a sleeve spec and portfolio group metadata.
- It has stateful parity cases.
- It has external replay fixtures including stale quote, partial fill, reject, cancel, sequence gap, and adverse move cases.
- Its strategy-local state is driven by feedback events, not emit-time assumptions.
- It passes adverse-selection replay metrics after fees and realistic fill assumptions.
- It has a documented operator runbook and rollback/cancel procedure.

---

## Excruciatingly Detailed Issues Log
*Known implementation roadblocks and required technical decisions uncovered during the audit and expansion.*

1. **Parity Drift in Stateful Contexts:**
   - *Detail*: Testing a single tick is straightforward. Testing a sequence where a partial fill modifies cash, which subsequently alters the sizing logic of the next intent, requires a fully stateful parity runner. If Python's `PnLTracker` evaluates fees differently (even by a single basis point) compared to Rust's `Ledger`, stateful parity will rapidly drift and fail downstream tests.
   - *Action*: Ensure exact matching of rounding modes (e.g., Banker's rounding) across both implementations before implementing Stateful Parity V2.

2. **Last-Look BBO Race Conditions:**
   - *Detail*: The time elapsed between `last_look_check` inside the gateway and actual network transmission to the venue could see a BBO change. If we enforce strict IOC limit pricing exactly matching the BBO at send-time, but the network induces a 10ms delay, Kalshi may reject the order as off-market if the book ticked.
   - *Action*: Allow a configurable `slippage_tolerance_ticks` inside the `StrategySpec` that permits the intent to be priced slightly worse than the instantaneous BBO to guarantee execution, while last-look simply validates the BBO hasn't moved *beyond* that tolerance.

3. **ONNX Threading Contention (`ort` & Tokio):**
   - *Detail*: The `ort` crate utilizes Rayon or OpenMP thread pools under the hood for parallel tensor operations. The `live-runner` / `strategy-worker` uses a Tokio multi-threaded runtime. When a market tick triggers an ONNX evaluation, the Tokio worker thread is blocked while `ort` spawns additional threads. This leads to extreme CPU context-switching (thread thrashing) and massive latency spikes.
   - *Action*: Explicitly configure the `ort` `SessionBuilder` to use `intra_threads(1)`. The latency of a small XGBoost/Linear model on a single thread is faster than the overhead of spinning up thread pools for micro-batches.

4. **Memory Leak in Python `PnLTracker` (GC Pauses):**
   - *Detail*: Even after Phase 2 fixes to stop eagerly tracking zero-position instruments, Python's Garbage Collector will periodically freeze the process to clean up discarded `MarketSnapshot` objects. In a high-throughput scenario, these GC pauses violate the microsecond latency budget.
   - *Action*: Python is restricted entirely to offline research, historical replay, and model generation. No Python code is permitted in the live-execution path.

5. **SQLite Locking in CEG Checkpoints:**
   - *Detail*: Using `INSERT OR REPLACE` synchronously on every fill or cash transfer to a SQLite DB (`ceg_state.db`) will block the CEG actor. SQLite table-level locking will stall the risk evaluations of new intents while disk I/O completes.
   - *Action*: Enable WAL (Write-Ahead Logging) mode on the SQLite connection. Furthermore, checkpoint writes must be dispatched to a dedicated background I/O thread via an asynchronous channel, allowing the CEG to immediately process the next event without blocking on `fsync`.

6. **Order Book Ghost Liquidity Recovery:**
   - *Detail*: Phase 1 fixed ghost liquidity by clearing stale price levels on quote updates. However, Kalshi's WebSocket can occasionally drop individual delta messages. If a level is fully consumed but the delta is dropped, the system incorrectly assumes the liquidity still rests until a full snapshot is polled.
   - *Action*: Ensure the sequence gap detector (Task C.1) is aggressively tuned. If any delta implies resting liquidity that contradicts a recent trade tape sweep, immediately suspend trading and invoke `FetchingSnapshot` state.

7. **Margin Offset Inefficiency in Risk Projection:**
   - *Detail*: Local risk tracks gross notional rather than true exchange margin requirements. Taking opposite directional bets (YES and NO) on a binary outcome limits max loss to $0 beyond the initial contract cost, but the local risk gate treats it as $200 of capital usage.
   - *Action*: Re-write the `CapitalAllocator` and `SleeveRiskGate` to use venue-specific mathematical netting logic for mutually exclusive tokens/outcomes.

8. **Floating Point Rounding & Discretization Bias:**
   - *Detail*: Strategies translating a continuous fair-value model probability into a discrete tick size (e.g., Kalshi cents) using naive `.round()` arithmetic suffer systemic adverse selection. A `0.556` probability rounded to a bid of `56c` inherently crosses the spread at a mathematical loss relative to expectation.
   - *Action*: Mandate explicit `floor()` operations for buy limits and `ceil()` operations for sell limits in strategy intent building to strictly preserve statistical edge post-discretization.

9. **Lookahead vs. Receipt-Time Joins in External Data:**
   - *Detail*: Merging exogenous cron snapshots (e.g., ATP Tennis Elo ratings) using logical `event_time` allows lookahead bias if the physical API response experienced network delay. Backtests will observe the external signal before the live system physically could have.
   - *Action*: Audit all Parquet replay data and force inner-joins exclusively against a recorded `receipt_time` (NIC timestamp) to reflect causal reality.

10. **Dynamic Taker Fee Mismatch (Kalshi V2):**
    - *Detail*: Utilizing a flat basis-point execution fee inside the simulated `executable_edge` function misaligns with Kalshi's non-linear taker fee schedule. Strategies targeting low-probability tail events (where relative fees are highest) will systematically overestimate their post-fee expected value.
    - *Action*: Hardcode the specific venue fee curves into the Rust `oms` and `risk` crates, making dynamic fee deduction a mandatory step before any `RiskGate` approval.

11. **Unhandled "In-Running" / Suspended Venue States:**
    - *Detail*: Normalization paths assume markets are active if the WebSocket is connected. Real venues suspend contracts for VAR reviews or administrative halts. A strategy executing against a stale top-of-book during a suspension will fail upstream or corrupt its local signal buffer.
    - *Action*: Expose explicit `MarketState::Suspended` flags. Add mandatory logic to the CEG that drops all intents matching a suspended state, and mandate strategies to reset signal memory upon resumption.

12. **Self-Matching & Wash Trading Across Sleeves:**
    - *Detail*: Without a global netting layer, if Sleeve A buys Kalshi YES at 50c and Sleeve B sells Kalshi YES at 50c, the CEG routes both intents to the venue. The exchange crosses them, charging taker fees on both sides while failing to alter our net position, and potentially triggering compliance wash-trading flags.
    - *Action*: The CEG must implement an internal Netting/Crossing engine or strictly reject self-matching intents before network dispatch.

13. **Correlated Pick-Offs (Toxicity Contagion):**
    - *Detail*: Exogenous news (e.g., CPI) can cause extreme fair-value shifts across dozens of correlated markets simultaneously. Toxic takers will sweep our passive resting limit orders before our strategy BBO maps update. Individual market limits won't catch this.
    - *Action*: Introduce a Global Toxicity Circuit Breaker. If fill velocity exceeds `X` orders within `Y` milliseconds, invoke a global "Cancel All" to pull remaining resting liquidity.

14. **Market Impact & L2 Book Sweeping:**
    - *Detail*: Disabling `OrderType.MARKET` in favor of limits does not prevent sweeping. Firing a 50c limit for 5,000 contracts when L1 depth is only 10 contracts will sweep L2/L3 liquidity up to 52c. The strategy assumes an entry VWAP of 50c but receives 51.98c, destroying the alpha.
    - *Action*: Enforce size limits tied strictly to displayed L1 depth within the strategy logic, or explicitly pass a `target_vwap` / `max_sweep_levels` to the `IntentSnapshot`.

15. **Margin Lockup & Opportunity Cost:**
    - *Detail*: A strategy may post hundreds of wide, passive resting limits, reserving 100% of capital. When a high-conviction arbitrage opportunity appears, the risk gate rejects it for "Insufficient Funds".
    - *Action*: Implement Margin Tiering/Preemption in the CEG. The gateway must be able to cancel low-priority resting orders to free capital for incoming high-priority intents.
# Superseded

This audit is superseded by `docs/v5-audit-and-agent-implementation-spec.md`.
