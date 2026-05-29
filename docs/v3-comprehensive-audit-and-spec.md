# V3 Comprehensive Audit + Implementation Spec

**Superseded:** Use `docs/v5-audit-and-agent-implementation-spec.md` as the
current implementation source of truth. This file is retained for historical
context only.

**Date:** Post-remediation snapshot. 118 Rust tests pass, mypy clean (204 files), Python 237 passed/9 skipped, clippy `--all-targets -D warnings` clean.

**Scope:** Audit covers assumptions, data security, redundancy, speed, strategy integration, and trading logic. This document is **the spec an engineering agent should reference**. Every finding has: a code citation (file:line), an impact rating, a concrete remediation (function names, type changes), edge cases, and a test plan.

**Reading order:**
1. §1 (top-level state) — what's actually fixed
2. §2 (residual findings, grouped by category) — what's not
3. §3 (implementation order) — what to do first, second, etc.
4. §4 (test efficacy) — how to know each fix worked
5. §5 (out-of-scope / multi-week follow-ons)

---

## 1. Where the System Is Today

The previous remediation passes closed major gaps. To prevent repeating those fixes, this section documents what is **already correct** so the agent doesn't waste cycles re-investigating.

### Already done (DO NOT redo)

| Finding | Status | Location |
|---|---|---|
| WS reconnect with backoff + stable-streak reset | ✅ | `live-runner/src/main.rs` ~378-385, ~440 |
| Sequence-gap detection forces reconnect | ✅ | `kalshi/src/normalize.rs::check_sequence` |
| Kill switch (file + Ctrl-C) | ✅ | `live-runner/src/main.rs` ~430-445 |
| Bulk cancel on shutdown | ✅ | `live-runner/src/main.rs` ~625 |
| Adopt orphan orders on startup | ✅ | `gateway/src/lib.rs::adopt_resting_order:950` |
| Real `canonical_sha256` (single-pass hasher) | ✅ | `contracts/src/lib.rs` |
| OMS exact decimal (rust_decimal) | ✅ | `oms/src/lib.rs:300-309` |
| `daily_realized_loss` folds in real P&L | ✅ | `gateway/src/lib.rs::apply_fill ~895-905` |
| Position basis uses fill price (VWAP) | ✅ | `gateway/src/lib.rs::update_position_for_fill` |
| Acked deferred until WS confirms | ✅ | `gateway/src/lib.rs:829-834` (comment + behavior) |
| `last_quote_epoch_secs` ages naturally via risk eval | ✅ | `risk/src/lib.rs::evaluate` |
| Cancels honor kill-switch | ✅ | `live-runner/src/main.rs:555` |
| Mark-price refused on absurd spreads | ✅ | `live-runner/src/main.rs::HALF_DOLLAR_RUNNER_TICKS` |
| Send-time last-look (mark/age/movement) | ✅ | `gateway/src/lib.rs::last_look_check:977` |
| Priority/TTL flows from spec → envelope → gateway | ✅ | `runner::build_intent_envelope` + `gateway::process_one:737-763` |
| Portfolio guard enforced pre-submit | ✅ | `gateway/src/lib.rs:791` |
| Reconciliation REST endpoint + adopt path | ✅ | `kalshi/src/rest.rs::list_open_orders`, `gateway::adopt_resting_order` |
| Docker hardening (non-root, tini, healthcheck, expanded `.dockerignore`) | ✅ | `Dockerfile`, `.dockerignore` |
| Idempotency expire by epoch comparison | ✅ | `gateway/src/lib.rs::expire_older_than` |
| Prediction audit chain links to source event | ✅ | `runner::record_prediction` |
| OMS `open_count()` O(1) | ✅ | `oms/src/lib.rs` |
| Strategy duplicate-pipeline / FixedPrice duplication / TennisOnnxModel duplication | ✅ | Earlier slim rounds |

### Architecture-of-record (what to build *with*, not *against*)

- **Single source of truth for sleeve state:** the `DryRunGateway`. Runners write quotes directly into `gateway.sleeve_state`, never to a local mirror. *(Don't reintroduce the dual-state pattern.)*
- **Hot-path projection boundary:** `runtime_hot::project_event` runs once at the bus subscriber, downstream code is `String`/`f64`-free for market-data. *(Strategies that need to add new event types must add a `HotEvent` variant, not parse JSON.)*
- **Cross-language wire format:** `contracts::canonical_sha256` is the audit anchor. Every new audit stamp must use it. Don't add new `"0".repeat(64)` placeholders.
- **Gateway is the only thing that talks to the venue.** Strategies emit `DecisionPayload`s; the gateway alone runs last-look, idempotency, rate limiting, portfolio check, and submit. *(Don't add a venue call to strategy code.)*

---

## 2. Residual Findings

### Severity scale
- **C (Critical):** Capital-loss risk in current state; fix before next live run.
- **H (High):** Correctness gap that produces wrong numbers but not necessarily a loss; fix before scaling capital.
- **M (Medium):** Operationally important; fix before unattended overnight runs.
- **L (Low):** Quality / future-friction; backlog.

### Category A — Trading Logic Flaws

#### A1 [C] Portfolio guard treats sells as additive to gross

**Where:** `gateway/src/lib.rs::PortfolioGuard::evaluate` ~566-600 and `gateway/src/lib.rs::current_portfolio_gross` (helper around line 1138/1163).

**Observed:**
```rust
let intent_notional = mul_ticks(price.abs(), quantity.abs());
total = total.saturating_add(intent_notional);
```
The guard *always* adds the intent's notional to the running portfolio gross, regardless of whether the intent is a Buy that opens/extends a position or a Sell that closes one. The `IntentSnapshot.side` field is ignored.

**Impact:** A Sell intent that would reduce gross exposure is checked against the cap as if it grew exposure. Closing trades get rejected at the gross limit. In a tight portfolio configuration the system is unable to unwind. Capital can become stuck.

**Remediation:**
1. In `evaluate`, look up existing `state.positions[intent.instrument_id]`. Compute signed projected position quantity = current_signed_qty + (intent_signed_qty).
2. Compute projected per-instrument notional = `|projected_qty| × intent_price_ticks / SCALE`.
3. Compute the *delta* vs current per-instrument notional. Add that delta (not the gross intent notional) to portfolio total.
4. Same for group totals — recompute by-instrument-by-group from positions + reservations, then delta-apply.
5. Reservations for the new order are still gross-add (until the order fills, full intent quantity is at risk).

**Test plan:**
- Unit: `portfolio_allows_sell_to_close_when_buy_would_exceed_cap`. Set policy cap = $500. Set position = +1000 contracts @ $0.50 (notional $500, at cap). Submit Buy 1 @ $0.50 → rejected (gross would be $500.50). Submit Sell 1 @ $0.50 → approved (closing).
- Unit: `portfolio_sell_into_short_increments_gross`. Position = -100 @ $0.40 (notional $40, |qty|=100 short). Submit Sell 100 @ $0.40 → projected position = -200, projected notional = $80. Should be added to gross.

**Effort:** 2-3 hours including tests.

---

#### A2 [H] Adoption bypasses every safety check

**Where:** `gateway/src/lib.rs::adopt_resting_order:950-972`.

**Observed:** The function pushes a venue-resting order straight into the OMS and recomputes open_orders + portfolio reservations. It does NOT:
- Run risk against the implied position
- Check the adopted instrument is in scope (configured tickers, no kill-switch overrides)
- Compare adopted state vs configured policy limits

**Impact:** A previous process lifetime may have left positions/orders that VIOLATE the current process's risk policy (tighter limits, different sleeves, different group rules). The new process adopts them silently and starts trading; the next intent gets rejected by risk *with the adopted exposure in the denominator* — operator can't tell why intents are failing.

**Remediation:**
1. After all adoption, recompute `sleeve_state.positions` from the adopted orders' `filled_quantity` (treat each adopted partial as a fill that already happened) — this requires synthesizing pseudo-`Fill`s or extending `adopt_resting_order` to also accept a position-snapshot input.
2. Run `RiskGate::evaluate_state_only(&state)` — a new method on `RiskGate` that checks current state against configured limits (no intent overlay). Returns a list of which limits are currently breached.
3. If any limit breached, log a structured warning AND set `state.kill_switch_engaged = true`. Operator must manually clear to trade.

**Test plan:**
- Unit: `adopting_order_that_violates_max_position_notional_engages_kill_switch`. Configure max_position_notional = $50. Adopt resting order with quantity 200 @ $0.50 (200 × 0.50 = $100). After adoption, assert `gateway.sleeve_state.kill_switch_engaged == true`.
- Unit: `adopting_order_within_limits_does_not_engage_kill_switch`. Symmetric.

**Effort:** 4 hours.

---

#### A3 [H] No order modification (replace) — every re-price is cancel + new

**Where:** `gateway/src/lib.rs::DecisionPayload` has only `PlaceOrder` and `CancelOrder`.

**Impact:** A market-making strategy needs to re-quote ~constantly. Currently that's: cancel old → wait for confirm → place new. Two round-trips per re-quote. During the gap, the strategy has zero presence — adverse-selection target. Also two idempotency-store entries per re-quote, doubling memory.

**Remediation:**
1. Add `DecisionPayload::ReplaceOrder { client_order_id, new_price, new_quantity }`.
2. Gateway maps to venue: if Kalshi has a replace endpoint, use it; otherwise emulate as atomic-from-strategy-perspective cancel-then-new (gateway holds replacement payload, executes cancel, waits for cancel-ack with timeout, then places). The atomicity contract: strategy treats it as one intent for idempotency.
3. New `client_order_id` for the replacement to maintain Kalshi's submit-side idempotency.

**Edge case:** Cancel races with fill — venue cancels too late, original fills, replacement places anyway. Gateway must detect post-cancel fill notification within a short window (default 2s) and abort the replacement.

**Test plan:**
- Unit: `replace_emulates_cancel_then_new_atomically`. Mock venue accepts cancel and place; assert sequence and that the OMS shows one canceled + one new.
- Unit: `replace_aborts_new_if_fill_arrives_during_cancel_window`. Inject fill after cancel-ack but before new-place; assert new is not placed.

**Effort:** 1-2 days. Defer to the next sprint if Kalshi doesn't natively support replace (depends on venue capability).

---

#### A4 [H] Partial-fill re-pricing is not exposed to strategies

**Where:** `runner/src/lib.rs::StrategyEvent::OwnFill` and `OnnxQuoteStrategy::on_event` ~924-955.

**Observed:** `OwnFill` carries `quantity` (the filled amount) but not `remaining_quantity` or the order's original quantity. Strategies see "a fill happened" but can't reason about "how much is left resting."

**Impact:** Partial-fill scenarios become a strategy blindspot. If 30% of a 10-lot fills, the remaining 7 sit at the original price until the order expires or is canceled. No re-pricing, no joining the new BBO. Real edge giveaway.

**Remediation:**
1. Extend `runtime_hot::HotOwnFill` with `remaining: Qty`.
2. Extend `runner::StrategyEvent::OwnFill` with `remaining: u32`.
3. Compute `remaining` in `gateway::apply_fill` from the OMS's post-fill state, pass it through to the strategy via the live-runner event loop.
4. Strategies use it for "if remaining > 0 after a fill, decide whether to cancel-and-rejoin BBO."

**Test plan:**
- Unit: `partial_fill_exposes_remaining_to_strategy`. Fixture: place order qty=10, partial fill qty=3. Assert strategy sees `OwnFill { quantity: 3, remaining: 7 }`.
- Unit: `final_fill_exposes_remaining_zero`. Same order, second fill of 7. `remaining: 0`.

**Effort:** 3-4 hours.

---

#### A5 [M] Tennis prediction has no staleness check

**Where:** `runner/src/lib.rs::TennisXgboostStrategy::on_event`. Predictions come in via `StrategyEvent::TennisPrediction`. Strategy stores latest probability and uses it indefinitely.

**Impact:** A prediction emitted 4 hours before match time has no timestamp on the strategy side. If the runner crashes and restarts after a delay, the stored probability is treated as fresh. Worst case: trade on a probability whose underlying state (player injury, match cancellation) has changed.

**Remediation:**
1. `StrategyEvent::TennisPrediction` already comes from a `NormalizedEventRecord` with `audit.produced_at`. Plumb it through into a `produced_at: String` field on the event.
2. `TennisXgboostStrategy` stores `predicted_at_epoch: i64` alongside the probability.
3. In `maybe_decide`, if `ctx.now_epoch - state.predicted_at_epoch > max_prediction_age_secs` (default 3600), refuse to act (and log).
4. Configurable per spec via `parameters.max_prediction_age_secs`.

**Test plan:**
- Unit: `tennis_strategy_refuses_when_prediction_stale`. Inject prediction at T=0, then a quote at T=4000 with max_age=3600. Assert no decision.

**Effort:** 2 hours.

---

#### A6 [M] One-shot-per-market strategy pattern is brittle

**Where:** `runner/src/lib.rs::TennisXgboostStrategy`, `OnnxQuoteStrategy` — both use the `OrderTracker` to lock a market after one fill.

**Observed:** After a fill, the market is locked forever. Even if the strategy's edge regenerates (price moves back, prediction updates), no further trades happen.

**Impact:** Research strategies map cleanly to "one trade per signal." Production strategies need to manage exit, scale, re-enter. Current strategies cannot.

**Remediation:** Add a strategy-level `cooldown_secs: Option<u64>` parameter. After a market goes `Filled`, the tracker unlocks after `cooldown_secs`. Default `None` keeps current behavior.

**Edge case:** Don't unlock while a fill chain is still resolving (partial fill in progress). Only unlock when the order is fully terminal.

**Test plan:**
- Unit: `onnx_quote_strategy_unlocks_market_after_cooldown`. Set cooldown=10. Fill at T=0. Quote at T=5 → still locked. Quote at T=15 → tracker unlocked, fresh decision allowed.

**Effort:** 3 hours.

---

#### A7 [M] No self-cross protection

**Where:** Strategy emits `Buy` at $X while it has an open `Sell` resting at $X-ε on the same instrument.

**Observed:** Nothing checks for this. Strategy doesn't see its own resting orders aggregated in a usable form.

**Impact:** Self-cross fills your own order, paying fees on both sides, no net position change. Pure cost.

**Remediation:**
1. Extend `runner::OrderTracker` to track resting orders' (side, price). Expose `would_self_cross(side, price) -> bool`.
2. Strategies call it before emitting a contra-side decision. If would_self_cross, log + skip.
3. Alternative: gateway checks via OMS — for each PlaceOrder, scan open orders on the same instrument; if any opposite side with crossable price, reject with new `GatewayError::SelfCross`.

The gateway approach is more robust (catches strategies that forget). Use both for defense-in-depth.

**Test plan:**
- Unit: `gateway_rejects_self_crossing_buy`. Place Sell @ $0.50 (gets to Acked). Then Place Buy @ $0.50 same instrument. Assert rejection.

**Effort:** 4 hours.

---

#### A8 [L] Last-look collar is one-size-fits-all

**Where:** `gateway/src/lib.rs::LastLookConfig::max_mark_movement_ticks: i64` (default 200 = 2¢).

**Observed:** A single absolute-ticks threshold across all instruments and price levels. For Kalshi binary contracts where prices range $0.01–$0.99, 2¢ is generous for $0.50-mid markets but loose for $0.05/$0.95 markets (40% relative).

**Remediation:** Add `max_mark_movement_bps: u32` (basis points of mark price) as an *additional* check (whichever is more restrictive fires). Default 500bps (5%).

**Test plan:**
- Unit: `last_look_uses_relative_threshold_for_low_priced_markets`.

**Effort:** 1 hour.

---

#### A9 [L] No clock-skew detection between strategy and venue

**Where:** Strategy emits intent at local `now_rfc3339`. Gateway computes intent age from local now vs `envelope.emitted_at`. If local clock drifts vs venue clock, age calculation diverges from venue's reality.

**Impact:** TTL-based behavior (especially `fast` tier with 5s budget) is wrong if local clock has drifted. Either too aggressive (stale-drops legitimate intents) or too lax (lets stale intents through).

**Remediation:** Track exchange_ts on incoming WS messages; periodically (every 60s) emit a metric `clock_skew_secs = local_now - latest_ws_exchange_ts`. Alert if > 1s.

**Effort:** 2 hours.

---

### Category B — Assumptions

#### B1 [H] Adopt path assumes `to_resting_snapshot` is total

**Where:** `live-runner/src/main.rs:351-357`.

**Observed:** Whole startup aborts on a single un-parseable venue order:
```rust
let snapshot = order.to_resting_snapshot(&now).map_err(|e| {
    format!("reconcile-on-start: cannot adopt venue order ... ;
            rerun with --cancel-orphans-on-start to clear venue truth")
})?;
gateway.adopt_resting_order(snapshot)?;
```

**Impact:** Kalshi changes a field name in a response → entire runner refuses to start. Operator stuck choosing between "cancel-all" and "downgrade to old binary."

**Remediation:** Per-order try; on failure, log + cancel that specific order (or skip with metric) but proceed with others.

```rust
let mut failed_adoptions = Vec::new();
for order in &resting {
    match order.to_resting_snapshot(&now) {
        Ok(snap) => gateway.adopt_resting_order(snap)?,
        Err(e) => failed_adoptions.push((order.order_id.clone(), e)),
    }
}
if !failed_adoptions.is_empty() {
    eprintln!("could not adopt {} orders; cancelling them venue-side:", failed_adoptions.len());
    // Issue per-order cancels for the failures; bulk-cancel-all is too blunt.
}
```

**Effort:** 1 hour.

---

#### B2 [H] Adoption doesn't refresh `last_quote_epoch_secs` for adopted instruments

**Where:** `gateway::adopt_resting_order` does not touch `sleeve_state.last_quote_epoch_secs`.

**Impact:** After adoption, the runner now has positions on `kalshi:X` but `last_quote_epoch_secs["kalshi:X"]` is absent. The next time a cancel intent is processed on `kalshi:X`, risk evaluation may fire `MissingMarketData`. Cancel cannot be re-evaluated → orphan persists.

Actually: cancels skip risk (only kill_switch). But place orders need data. The strategy might emit a "manage adopted position" intent that becomes a kill-switch-rejected ghost.

**Remediation:** Make adoption a "subscribe trigger" — `adopt_resting_order` returns `Vec<InstrumentId>` of newly-adopted instruments. The caller (live-runner) must subscribe to these on the WS before resuming the main loop.

**Test plan:**
- Integration (mock WS): adopt an order on a non-subscribed market; verify the subscribe list grows.

**Effort:** 2-3 hours.

---

#### B3 [M] `current_portfolio_gross` is recomputed from scratch on every evaluate

**Where:** `gateway/src/lib.rs` (helper around line 1138-1170).

**Observed:** Each PlaceOrder intent walks the full positions map + reservations map. O(N+M) where N=instruments and M=open orders. Each instrument's group lookup hits two HashMaps and a Vec scan.

**Impact:** At 100 markets × 50 open orders, ~150 lookups + map walks per intent. ~10-30µs. Not catastrophic.

**Remediation (only if profiling justifies):**
- Cache `current_portfolio_gross` after each `sync_open_orders`. Invalidate on fill/cancel/adopt.
- Maintain `current_portfolio_gross_ticks: i128` directly, update incrementally on fill / open_order / state-transition.

**Test plan:** Property test — incremental update result matches recomputed result over 1000-step fixture.

**Effort:** 2 hours.

---

#### B4 [M] Synthetic mid as the only mark — no last-trade fallback

**Where:** `live-runner/src/main.rs` writes `mark_price_ticks = (bid + ask) / 2`. No alternative.

**Observed:** When the book widens (one-sided posting, low-liquidity), the synthetic mid becomes meaningless. We refuse to write a mark when spread > $0.50 (good!) — but for spreads $0.10-$0.50 the mark is loose.

**Remediation:** Prefer **last-trade price** as mark when:
- Last trade was within `mark_trade_max_age_secs` (default 5s)
- Last trade is between current bid and ask

Else fall back to mid. Implement as a small `MarkPolicy` enum in the gateway state.

**Effort:** 4 hours.

---

#### B5 [L] Reconnect attempt budget hard-cap is silent

**Where:** `live-runner/src/main.rs::reconnect_ws`.

**Observed:** Reconnect attempts capped at some number (look for hardcoded). After cap, returns error → loop breaks → runner exits. No metric "exhausted reconnect budget."

**Remediation:** Emit `metrics.reconnect_budget_exhausted += 1` on the final failure; log the elapsed time before giving up.

**Effort:** 30 minutes.

---

### Category C — Speed / Hot-Path

#### C1 [M] `OnnxScorer::session` is `parking_lot::Mutex<Session>` — serializes inference

**Where:** `model-runtime/src/lib.rs:67-75`. ort 2.x's `Session::run` requires `&mut self`.

**Observed:** Two strategy workers sharing one `Arc<OnnxScorer>` block each other on the mutex.

**Remediation (real path):** Session pool — N independent `Session` instances loaded from the same model file, picked round-robin via a counter. Avoids contention at the cost of N× memory. For typical sub-100MB ONNX models, N=2-4 is fine.

```rust
pub struct OnnxScorerPool {
    sessions: Vec<Mutex<Session>>,
    next: AtomicUsize,
    input_name: String,
    output_name: String,
    input_width: usize,
    output_select: OutputSelect,
}
```

Pick a slot with `next.fetch_add(1) % sessions.len()`. Lock that slot, run, release.

**Test plan:**
- Bench (`criterion`): single scorer vs 4-pool under 8 concurrent calls. Pool should be 2-3× faster.

**Effort:** 4 hours including bench.

---

#### C2 [M] Per-intent JSON parse — gateway re-parses `decision_json`

**Where:** `gateway/src/lib.rs::process_one:732-733` parses the `decision_json` field, even though `enqueue` already parsed it.

**Observed:** `enqueue` parses to validate, then forgets. `process_one` parses again.

**Remediation:** Store the parsed `DecisionPayload` alongside the envelope in the scheduler's deques. Make the scheduler entries `(IntentEnvelopeRecord, DecisionPayload)`. Net: one parse per intent.

**Effort:** 2 hours.

---

#### C3 [M] `canonical_sha256` per audit stamp serializes the whole record-payload

**Where:** Anywhere `canonical_sha256(&serde_json::json!({...}))` is called. Each call: `serde_json::to_value` (allocates), then walks the value to write into hasher.

**Observed:** Two allocations per audit (the `json!` macro builds a `Value`, then the hasher walk does string conversions).

**Remediation:** For audit records with a fixed shape (most of them are), bypass `serde_json::Value` and build a custom `Hash`-style trait that writes field-by-field in sorted order directly into `Sha256`. ~5× speedup for the common 7-field intent envelope hash.

This is a real micro-opt. Only do if profiling shows audit overhead.

**Effort:** 4-6 hours.

---

#### C4 [L] Strategy `format!("c-onnx-yes-{:08}", n)` per decision

**Where:** Strategies generate client_order_ids via `format!`. Heap-allocates a new String.

**Remediation:** `SmolStr::new_inline(...)` won't help (output > 23 bytes). Acceptable as-is. Note for the record.

---

#### C5 [L] Risk evaluate clones `Position` on lookup

**Where:** `risk/src/lib.rs::evaluate` calls `state.positions.get(&id).cloned().unwrap_or_default()`.

**Observed:** `Position { quantity: i64, avg_price_ticks: i64 }` is `Copy`. The `.cloned()` is a memcpy of 16 bytes, not a heap clone. Acceptable.

---

#### C6 [M] Per-quote `last_quote_epoch_secs.insert(...)` allocates a `String` key clone

**Where:** `live-runner/src/main.rs` quote-handling block.

**Observed:** Every quote does `last_quote_epoch_secs.insert(instrument.clone(), now_epoch)`. The instrument is a `String` (from `StrategyEvent::Quote { instrument: String }`). Heap allocation per quote per instrument.

**Remediation:** Two options:
- Change `SleeveState.last_quote_epoch_secs` key type to `SmolStr`. Likely already inline for `kalshi:X`-style ids.
- Use entry API: `if let Some(existing) = map.get_mut(instrument.as_str()) { *existing = now_epoch; } else { map.insert(instrument.clone(), now_epoch); }`.

Net at high event rates: meaningful.

**Effort:** 1 hour.

---

#### C7 [M] Repeated `epoch_seconds(now)` parse per intent

**Where:** `gateway/src/lib.rs::process_one` and helpers compute `epoch_seconds(now)` multiple times per intent (TTL check, risk evaluate, last-look, idempotency).

**Remediation:** Parse once at the top of `process_one`, pass `now_epoch: i64` through the helper chain.

**Effort:** 1 hour.

---

### Category D — Strategy Integration / Developer UX

#### D1 [H] Strategy registration is asymmetric Python ↔ Rust

**Where:** `runner/src/registry.rs::default_registry`. Lists: `weather_threshold`, `example_threshold`, `sports_tennis_xgboost`, `onnx_quote`. Python has many more (per the prior audit's claim of "many registered").

**Impact:** A researcher promoting a Python strategy must:
1. Hand-port to Rust
2. Add to `default_registry()`
3. Add `FromSpec` impl
4. Match Python's behavior byte-for-byte

No automated check that the port matches.

**Remediation:**
1. **Strategy promotion checklist** — markdown file in `docs/strategy-promotion.md` (probably exists) describing exact steps.
2. **Required parity case** — every strategy with a Rust impl MUST have at least one `contracts/parity/<name>/*.json` case. A CI test (the `parity` crate now has `JsonParityCaseLoader` + `StrategyParityRunner`) loads them and asserts pass.
3. **CI gate**: `cargo test -p eventcontracts-parity --test parity_cases` runs and must pass before merge.

**Effort:** 1 day (writing cases + CI wiring).

---

#### D2 [M] No `FromSpec` for `OnnxQuoteStrategy` in the registry

**Where:** `runner/src/registry.rs::default_registry` does not list `onnx_quote`. Live-runner registers it manually at line 301. Other call sites (parity tests, etc.) won't find it.

**Remediation:** Move the `build_onnx_quote_from_spec` factory out of `live-runner/src/main.rs` and into `model-runtime` (where the ONNX dep already lives) or a new `runner-onnx` crate. Register it in `default_registry()` so all consumers find it.

**Effort:** 2 hours.

---

#### D3 [M] Spec discovery via `param_str_or` swallows typos

**Where:** `runner::spec::StrategySpecArtifact::param_str_or` returns the default on a missing key. If a researcher writes `parameters.model_paht` (typo), the strategy silently uses the empty default.

**Remediation:** Strategies should explicitly enumerate expected params at construction. Add a `validate_known_params(&self, expected: &[&str]) -> Result<(), SpecError>` that fails if any spec key isn't in `expected`. Call from each `FromSpec` impl.

**Effort:** 3 hours.

---

#### D4 [L] No first-class strategy template / skeleton

**Where:** Researchers wanting to add a strategy must read the existing implementations.

**Remediation:** Add `docs/strategy-template.rs.md` with a copy-pasteable minimal strategy + its spec.toml + parity case. Reduces ramp time.

**Effort:** 2 hours.

---

### Category E — Data Security

#### E1 [H] Raw event storage retains payloads indefinitely

**Where:** Python `parquet_store.py` (per audit, line ~159) writes raw `payload_json` directly.

**Impact:** External feeds + venue messages may carry account-adjacent metadata over time. No redaction layer. No TTL.

**Remediation:**
1. Add `RedactionPolicy` configurable per source/channel. Initially:
   - `kalshi-ws/fill`, `kalshi-ws/order` → drop unless `--retain-private-events` flag.
   - `kalshi-rest/portfolio/*` → never persist.
2. TTL on raw partitions — `data/raw/<date>/` older than N days auto-archived/deleted.
3. Persist hashes-only for private events (so audit chain stays intact, raw not retained).

**Effort:** 1 day.

---

#### E2 [M] Live keys mounted via path env var

**Where:** `KALSHI_PRIVATE_KEY_PATH` points to a file. Anyone with read access to the runner's filesystem reads the key.

**Remediation:** Add `KALSHI_PRIVATE_KEY_PEM` as an alternative (key inline in env, not file). Document that prod should pass the PEM bytes via the secret manager directly, not a file path. Keep `KALSHI_PRIVATE_KEY_PATH` for local dev.

**Effort:** 1 hour.

---

#### E3 [L] `.env.demo` is committed in `.gitignore` but exists in the repo root

**Where:** `.env.demo` was created during demo testing and contains demo credentials. Audit history shows it.

**Remediation:** Verify `.gitignore` covers `.env.*`. Rotate the demo key in case the file leaked before being gitignored. Document the rotation in `docs/runbooks/credential-rotation.md`.

**Effort:** 30 minutes.

---

### Category F — Redundancy / Slim

#### F1 [L] `ParityRunner` has both `StrategyParityRunner<S>` and `DynStrategyParityRunner`

**Where:** `parity/src/lib.rs:74-94`. Two near-identical types: one generic, one dyn.

**Observed:** Both call into the same `run_strategy_case` helper. Different constructors. The shared helper deduplicates well; the duplicate types just add API surface.

**Remediation:** Keep `DynStrategyParityRunner` (more useful at the test-CI boundary), document `StrategyParityRunner` as "use when you want zero dyn overhead in benchmarks."

No action needed; well-managed.

---

#### F2 [L] `ParityCase.feature_vector` is unused

**Where:** `parity/src/lib.rs:13`. The `run_strategy_case` helper never reads it.

**Remediation:** Either remove the field, or document its purpose (e.g., "for future feature-builder parity tests"). Pick one.

**Effort:** 30 minutes.

---

#### F3 [L] `runner::FixedPrice` still distinct from `runtime_hot::FixedPrice`

**Where:** Two scales (10⁶ and 10⁴) coexist with a documented ×100 conversion.

**Observed:** Explicitly deferred in S2. Cost of unification is high (strategy implementations + tests touch ticks directly), value is clarity. Re-deferred.

---

### Category G — Testing / Deployment Accuracy

#### G1 [C] No CI runs `cargo test`

**Where:** CI config (search `.github/workflows/`).

**Observed:** Per the audit, CI runs `cargo check` but not `cargo test`. Per-commit safety net is absent for Rust.

**Remediation:** GitHub Actions job:
```yaml
test-rust:
  runs-on: ubuntu-latest
  steps:
    - uses: actions/checkout@v4
    - uses: dtolnay/rust-toolchain@stable
    - run: cargo test --manifest-path rust/Cargo.toml --workspace --all-targets
    - run: cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
```

**Effort:** 1 hour.

---

#### G2 [C] No CI runs parity cases

**Where:** `parity` crate has runner machinery + JSON loader. No CI invocation.

**Remediation:** Add a `parity_cases` integration test in `rust/crates/parity/tests/`. The test:
1. Walks `contracts/parity/`
2. Loads each `<strategy_name>/case_*.json`
3. Instantiates the strategy via `default_registry()`
4. Runs through `DynStrategyParityRunner::run_case`
5. Asserts pass

Add at least one fixture per registered strategy. Without fixtures, the test passes trivially — that's OK at first; the requirement becomes "to register a strategy, add a fixture."

**Effort:** 4-6 hours including fixtures for `weather_threshold` and `sports_tennis_xgboost`.

---

#### G3 [H] No live integration test against Kalshi demo

**Where:** All unit tests use mocks. There's no automated test that places + cancels a real order on demo Kalshi.

**Impact:** Field name changes in Kalshi's API can land in prod undetected.

**Remediation:** A `cargo test --features kalshi-demo-integration` opt-in test:
1. Requires `KALSHI_DEMO_KEY_ID` + `KALSHI_DEMO_PRIVATE_KEY_PATH` env vars.
2. Connects to demo, places a `--max-live-orders 1` IOC order at $0.01 yes on any available market.
3. Confirms order shows up in `list_open_orders` OR confirms IOC reject reason.
4. Cancels (if open).
5. Tears down.

Run weekly via scheduled GitHub Action. Catches Kalshi API regressions early.

**Effort:** 1 day.

---

#### G4 [M] No benchmarks — speed claims unverifiable

**Where:** No `cargo bench` targets exist.

**Remediation:** `cargo bench` with `criterion`. Bench at minimum:
- `canonical_sha256` for a 7-field intent envelope
- `gateway::process_one` start-to-finish for an approved place_order
- `OnnxScorer::predict` for a 20-feature input (tennis-shaped)
- `RollingQuoteState::push` after warmup
- `risk::evaluate` for a 100-instrument state

CI tracks regressions (initially manual; later automated).

**Effort:** 1 day.

---

#### G5 [M] No `--dry-run` for the live-runner that simulates submit

**Where:** Live-runner has `--live-submit` (real) or omits it (records nothing or RecordingVenueClient). No middle ground that pretends to submit but validates the wire format against a recorded fixture.

**Remediation:** Add `--dry-run-against-fixture <PATH>` flag: pulls recorded BBO data from a parquet fixture, plays it through the runner with a `MockVenueClient` that asserts payload shape matches.

**Effort:** 1 day.

---

### Category H — Operational / Production Readiness

#### H1 [H] No structured metrics export (just JSON-at-exit)

**Where:** `live-runner/src/main.rs` writes a JSON file at process exit. No live Prometheus endpoint.

**Impact:** Operator can't see "what's the rate of risk rejections in the last 5 minutes?" without restarting.

**Remediation:** `prometheus` crate + a tiny `axum` HTTP listener on `--metrics-port`. Expose every counter + histogram in real time.

Histograms: `gateway_process_one_latency_us`, `last_look_age_secs_at_submit`, `scorer_predict_latency_us`.

**Effort:** 6 hours.

---

#### H2 [H] No alerting on systemic failures

**Where:** Risk rejections, last-look rejects, sequence gaps — all increment counters but emit no alerts.

**Remediation:** A simple `--alert-webhook <URL>` flag. When configured, the runner POSTs a JSON payload on:
- Reconnect budget exhausted
- `>10` consecutive risk-rejections of any single reason
- Last-look reject rate `>20%`
- Sequence gap detected
- Kill switch engaged

Slack/Discord/PagerDuty integration is downstream.

**Effort:** 4 hours.

---

#### H3 [M] No runbook for the most common failure modes

**Where:** `docs/runbooks/` may or may not exist.

**Remediation:** Write runbooks for:
- WS disconnect → reconnect budget exhausted
- Kill switch engaged unexpectedly
- All intents rejected by risk
- Last-look rejecting too often
- Adopt-on-start failed
- Live submit hit `--max-live-orders` cap

Each: symptoms, diagnostic commands, remediation steps, escalation.

**Effort:** 1 day.

---

#### H4 [M] No state snapshot for crash recovery

**Where:** OMS, sleeve state, ledger live entirely in process memory.

**Remediation:** Periodic (`--snapshot-interval-secs 60`) write of:
- `oms.orders` → SQLite or JSON
- `sleeve_state.positions`, `daily_realized_loss`
- `idempotency_store.seen` (last 1h)
- `ledger`

On startup, if snapshot exists and is fresh (<5 min old), load it. Reconciliation still runs after.

**Effort:** 1 day.

---

## 3. Implementation Order

The order matters. Each item is sequenced so its prerequisites are already done.

### Phase α — Trading-correctness critical (Days 1-2)

Items where current behavior is silently wrong.

1. **A1 portfolio guard sells** — broken math; capital can become stuck.
2. **A2 adoption ignores risk** — silently violates policy on restart.
3. **G1 cargo test in CI** — wraps everything else in a safety net.

### Phase β — Trading-correctness high (Days 3-5)

4. **A4 partial-fill remaining exposed** — strategies need this to manage exits.
5. **A7 self-cross protection** — straightforward cost saver.
6. **B1 adoption per-order resilience** — operator usability.
7. **B2 adoption refreshes quote subscription** — completeness of adopt path.

### Phase γ — Testing/deployment accuracy (Week 2)

8. **G2 parity CI** — codifies promotion contract.
9. **G3 Kalshi demo integration test** — catches API drift early.
10. **D1 strategy promotion checklist** + parity-case requirement.
11. **D2 OnnxQuoteStrategy in default_registry** — symmetric with other strategies.
12. **D3 spec param validation** — catches researcher typos.

### Phase δ — Operational / production (Week 3)

13. **H1 Prometheus metrics** — observability foundation.
14. **H2 alert webhook** — incident response.
15. **H3 runbooks** — incident self-service.
16. **H4 state snapshot** — fast recovery.
17. **E1 raw-event redaction** — compliance hardening.

### Phase ε — Speed / polish (Week 4)

18. **C1 OnnxScorerPool** — parallelism for multi-strategy.
19. **C2 cache parsed `DecisionPayload`** — small per-intent win.
20. **C6 SmolStr keys in sleeve_state** — quote-rate win.
21. **C7 single epoch parse per intent** — micro-opt.
22. **G4 criterion benchmarks** — speed-claim verification.

### Out of scope here (multi-week)

- A3 ReplaceOrder support (depends on Kalshi API capability).
- A5 tennis prediction staleness (depends on prediction-source schema work).
- A6 strategy cooldown (depends on consensus on UX).
- B4 last-trade as mark (depends on book-feature work).

---

## 4. Test Efficacy

For each fix, this section says **exactly how to know it worked**.

### Universal pre/post

Before each fix:
```bash
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cd python && python -m mypy && cd ..
cd python && python -m pytest tests -q && cd ..
```
After each fix, the same. Pre-existing counts: **118 Rust tests, mypy 204/0, pytest 237 passed/9 skipped**.

### Per-fix specifics

#### A1 — portfolio guard sells

New tests in `gateway/src/lib.rs` test module:
- `portfolio_allows_sell_to_close_when_buy_would_exceed_cap`
- `portfolio_sell_into_short_increments_gross`
- `portfolio_flat_position_buy_increments_gross`

Manual: configure `--portfolio-max-gross 10`, run live-paper with a strategy that ends up at exactly cap, then have it emit a Sell. Observe the Sell is approved.

#### A2 — adoption checks policy

New test: `adopting_position_that_exceeds_max_position_notional_engages_kill_switch`. Configure `max_position_notional = 50`, simulate adoption of qty=200 @ $0.50 = $100. Assert `kill_switch_engaged == true`.

Manual: leave a resting order on demo Kalshi for an account configured with a tighter `--portfolio-max-gross`, restart with `--reconcile-on-start`, observe stderr says "adopted state violates portfolio policy; kill switch engaged."

#### A4 — partial fill remaining

Test: `partial_fill_exposes_remaining_to_strategy`. Mock OMS with a 10-lot order; apply a 3-lot fill; assert the StrategyEvent::OwnFill the strategy receives has `remaining: 7`. Then a 7-lot fill; assert `remaining: 0`.

#### A7 — self-cross

Test: `gateway_rejects_self_crossing_buy`. Place a `Sell @ $0.50` (manually advance OMS to Acked). Submit a `Buy @ $0.50` same instrument. Assert `Err(GatewayError::SelfCross)`. Symmetric for crossable prices (Sell @ $0.51, Buy @ $0.52).

#### B1 — adoption per-order resilience

Test: `adoption_continues_after_single_unparseable_order`. Mock REST returns 3 orders, the 2nd missing a required field. Assert the runner adopts 1+3, attempts cancel on 2, and proceeds.

#### B2 — adoption refreshes subscriptions

Integration test (mock WS): adopt an order on `kalshi:NEW`, verify the subscribed-tickers list grew to include `kalshi:NEW` before the main loop starts pulling messages.

#### G1 — cargo test in CI

Push a PR that intentionally breaks a test. Confirm CI rejects it. Revert.

#### G2 — parity CI

Add a `weather_threshold` parity case. Run `cargo test -p eventcontracts-parity --test parity_cases`. Assert pass. Mutate the strategy locally (change a threshold). Assert the parity test now fails.

#### G3 — Kalshi demo integration

Set `KALSHI_DEMO_KEY_ID` + `KALSHI_DEMO_PRIVATE_KEY_PATH`. Run `cargo test --features kalshi-demo-integration --test kalshi_live_smoke -- --nocapture`. Observe a real order on demo Kalshi, the test assertion, and cleanup.

#### H1 — Prometheus

Run `live-runner --metrics-port 9090`. `curl http://localhost:9090/metrics`. Confirm:
- `eventcontracts_gateway_acks_total` exists and is a Counter
- `eventcontracts_last_look_rejects_total{reason="quote_stale"}` exists
- `eventcontracts_gateway_process_one_latency_us` exists as a Histogram

#### H4 — state snapshot

1. Run `live-runner --snapshot-interval-secs 5 --snapshot-path /tmp/snap.db` for 30s with simulated activity.
2. Kill -9 the process.
3. Restart with `--reconcile-on-start --resume-snapshot /tmp/snap.db`.
4. Confirm restored state matches expected (OMS open count, positions, ledger entries within last 5s lost).

#### C1 — OnnxScorerPool

Criterion bench: `scorer_serial` (single Mutex) vs `scorer_pool_4` (4-session pool) under 8 concurrent calls. Pool ≥2× faster.

#### C2 — cached DecisionPayload

Bench: `process_one_with_recached_payload` should be 2-5µs faster than baseline.

### Manual end-to-end after Phase γ

The "before live capital" smoke:

```bash
KALSHI_ENV=demo eventcontracts-live-runner \
  --env-file .env.demo \
  --pattern "KX" --max-markets 1 \
  --strategy-spec configs/strategies/example_threshold.toml \
  --live-submit --max-live-orders 1 \
  --reconcile-on-start \
  --portfolio-max-gross 5 \
  --metrics-port 9090 \
  --metrics-json /tmp/run.json \
  --duration-secs 120
```

Expected (depending on strategy / market state):
- Connects, discovers markets, subscribes to ticker/trade/orderbook/fill/order channels.
- If demo had resting orders: adopts them, logs the count.
- Either emits one PlaceOrder (counter reaches 1) and exits, or `--duration-secs 120` elapses cleanly.
- Final report shows `last_look` rejection counts, risk rejection counts by reason.
- `/tmp/run.json` has every metric.

Then verify in the Kalshi demo UI that:
- Any orders the runner placed are visible/canceled as expected.
- No unexplained orders are open (the `cancel_all` on shutdown ran).

---

## 5. Out of Scope / Multi-Week Follow-ons

Tracked here for visibility, not for this delivery:

- **Async gateway submit path.** Currently `KalshiVenueClient::submit` blocks via `tokio::task::block_in_place`. Real path is to make `VenueClient::submit` async fn (via `async-trait`) and propagate `async`/`await` through `process_one`. Multi-day refactor.
- **Bus crate as production NATS publisher.** Currently the `bus` crate has traits + `InMemoryBus`. Real multi-process operation needs NATS JetStream. Multi-day.
- **Polymarket adapter.** Parallel `VenueClient` impl, EIP-712 signing, on-chain key mgmt. Multi-day.
- **WASM strategy sandbox.** For untrusted strategy code. Out-of-scope for first-party deployment.
- **Multi-sleeve runner** — current architecture is single-sleeve-per-process. Multi-sleeve sharing one OMS/gateway is a real architectural change.

---

## Appendix A — File Paths Cheatsheet

| Crate | Role |
|---|---|
| `rust/crates/contracts` | Cross-language wire types + `canonical_sha256` |
| `rust/crates/runtime-hot` | Hot-path `FixedPrice`/`Qty`/`HotEvent` projections |
| `rust/crates/oms` | Order state machine (rust_decimal) |
| `rust/crates/risk` | Stateful pre-trade risk |
| `rust/crates/gateway` | Single venue I/O choke point + portfolio + last-look |
| `rust/crates/kalshi` | Kalshi-specific WS/REST/VenueClient |
| `rust/crates/runner` | Strategies + `StrategyRuntime` trait + `build_intent_envelope` |
| `rust/crates/feature-builder` | `Scorer` trait + quote-feature extractors |
| `rust/crates/model-runtime` | ONNX `OnnxScorer` |
| `rust/crates/parity` | Parity case loader + runner |
| `rust/crates/live-runner` | Production binary |
| `rust/crates/bus` | Forward-looking IPC abstraction (not in prod use) |
| `rust/crates/allocator` | Forward-looking capital allocator (not in prod use) |

## Appendix B — Severities Table (this audit)

| Item | Severity |
|---|---|
| A1 portfolio sells additive | C |
| A2 adoption skips policy | H |
| A3 no replace | H |
| A4 partial-fill remaining hidden | H |
| A5 tennis prediction staleness | M |
| A6 strategy cooldown | M |
| A7 self-cross | M |
| A8 last-look relative collar | L |
| A9 clock skew | L |
| B1 adoption per-order resilience | H |
| B2 adoption subscribes | H |
| B3 portfolio gross perf | M |
| B4 last-trade mark | M |
| B5 reconnect budget metric | L |
| C1 ScorerPool | M |
| C2 cached payload | M |
| C3 single-pass audit hash | M |
| C6 SmolStr keys | M |
| C7 single epoch parse | M |
| D1 strategy registration asymmetry | H |
| D2 OnnxQuote in registry | M |
| D3 spec param validation | M |
| D4 strategy template doc | L |
| E1 raw payload retention | H |
| E2 key file path | M |
| E3 .env.demo hygiene | L |
| F1, F2 parity cleanup | L |
| F3 FixedPrice unify | deferred |
| G1 cargo test CI | C |
| G2 parity CI | C |
| G3 demo integration test | H |
| G4 criterion benches | M |
| G5 dry-run fixture | M |
| H1 Prometheus | H |
| H2 alert webhook | H |
| H3 runbooks | M |
| H4 state snapshot | M |

**Counts:** C×3, H×11, M×17, L×6 (+ 2 cleanup-tagged L, + 1 deferred).

The C-class items are the gates. None of the remaining H-class items individually risks immediate capital loss, but together they describe an operationally fragile system.

---

*End of spec.*
# Superseded

This audit is superseded by `docs/v5-audit-and-agent-implementation-spec.md`.
