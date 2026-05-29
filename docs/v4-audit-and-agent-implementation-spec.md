# V4 Hyper-Comprehensive Audit + Agent Implementation Spec

**Superseded:** Use `docs/v5-audit-and-agent-implementation-spec.md` as the
current implementation source of truth. This file is retained for historical
context only.

**Date:** 2026-05-28
**Author:** Audit pass on top of `docs/v3-comprehensive-audit-and-spec.md` and `docs/hyper-comprehensive-codebase-audit-agent-spec.md`.
**Audience:** A single implementation agent. Read §0 first, then work top-to-bottom.

This spec is **the** working document. It does three things:

1. **Verifies** which fixes from the V3 and "hyper-comprehensive" audits actually landed in the current tree, which were partially implemented, and which are still open.
2. **Adds new findings** discovered in this pass — focused on the trader's perspective (fee math, settlement, queue priors, sweep, atomic groups, suspension), on hot-path speed (allocation, blocking REST, ONNX threading), on dev ergonomics (strategy template, registry parity, dry-run), and on data security (raw-event retention, key rotation, supply chain).
3. **Sequences the work** so each phase only depends on prior phases, with file/line citations, exact code changes, and a per-fix verification recipe.

This is intentionally redundant with the prior two specs in places — when a prior finding is still load-bearing it is restated, marked **(carried)**, and verified against the current code so the agent doesn't have to cross-check three documents.

---

## §0. Ground Rules For The Agent

1. **Do not regress green checks.** Before every phase, run the baseline:
   ```bash
   cd C:/QWS/eventcontracts
   cargo fmt --manifest-path rust/Cargo.toml --all -- --check
   cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
   cargo test --manifest-path rust/Cargo.toml --workspace
   cd python && python -m ruff check src tests && python -m mypy src/eventcontracts tests && python -m pytest tests -q && cd ..
   ```
   Record counts (current expected: 135 Rust tests passing, mypy clean ≥205 files, pytest 240 passed/9 skipped). Any drop after a phase means a regression; fix in-phase before moving on.

2. **One PR per phase.** Phases P1…P10 below should land as ten small PRs, not one mega-PR. Each PR title must reference its phase id (e.g. `P3: feedback events for strategy state`).

3. **Never bypass the gateway.** Strategy code must continue to return `DecisionPayload` values; never call a venue client directly. If you find yourself wanting to, the gateway is what needs to change.

4. **Single source of truth = `DryRunGateway.sleeve_state`** for risk/cash/positions in Rust, and `SleeveRiskGate + PnLTracker` in Python. Do not introduce mirror state.

5. **No production-only test hooks.** Use fixtures under `contracts/replay/` or `python/tests/fixtures/`. Anything that branches on `cfg(test)` or `if testing:` for live behavior is rejected.

6. **Add a regression test for every fix.** If a fix has no failing-then-passing test, it didn't happen.

7. **Dual-language fixes need dual-language coverage.** When you change a Python invariant, also change Rust (and vice versa) unless the spec explicitly says "Python-only" or "Rust-only". Parity cases in `contracts/parity/` are the cross-language contract.

8. **Confirm before destructive action.** Anything that touches `data/`, secrets, demo accounts, or the live venue requires an explicit operator-approved override.

---

## §1. State Of The Tree Today

### Implemented and verified by inspection of the current code

| Capability | Where | Verified |
|---|---|---|
| YES/NO outcome-side accounting in Rust | `oms/src/lib.rs:59`, `gateway/src/lib.rs:909`, `risk/src/lib.rs:272` | ✅ |
| Side-specific BBO last-look | `risk/src/lib.rs:65/69/280`, `gateway/src/lib.rs:1045/1051` | ✅ |
| `SubmitUnknown` non-terminal order state on transport error | `oms/src/lib.rs:79/197`, `gateway/src/lib.rs:826` | ✅ |
| Live submit requires `--reconcile-on-start` or `--cancel-orphans-on-start` | `live-runner/src/main.rs:744` | ✅ |
| Unknown private events halt live + bulk cancel | `live-runner/src/main.rs:534/954` | ✅ |
| Promoted Python strategies converted to IOC limit + market snapshot | strategies updated, lint at `python/tests/test_strategy_promotion_guards.py` | ✅ |
| `.gitignore` covers `data/`, `*.parquet`, `*.pem`, `*.key`, `demokey.txt`, `ecmodel.txt`, `.env.*` | `.gitignore:1-50` | ✅ |
| `cargo fmt --check` in CI | `Makefile:27`, `.github/workflows/quality.yml` | ✅ |
| Fill-velocity toxicity circuit breaker | `gateway/src/lib.rs:427/1118`, `live-runner/src/main.rs:601` | ✅ |
| L1 displayed-depth guard | `risk/src/lib.rs:390`, `gateway/src/lib.rs:1348`, `python/.../risk/limits.py:170-183` | ✅ |
| Portfolio guard projects post-intent exposure (sells close) | `gateway/src/lib.rs:604/1347` | ✅ |
| Adoption validates against projected risk + portfolio policy, can engage kill switch | `risk/src/lib.rs:157`, `gateway/src/lib.rs:1076/1088` | ✅ |
| Self-matching rejection (same-instrument same-side crossing own resting) | `gateway/src/lib.rs:58/1149` | ✅ |
| Last-look requires executable BBO + L1 depth in live | `gateway/src/lib.rs:408/1334/1348`, `live-runner/src/main.rs:390` | ✅ |
| Python Kalshi fee model = `ceil(100 * 0.07 * p * (1-p) * qty)` (the real V2 curve) | `python/.../adapters/venues/kalshi/fees.py:33-53` | ✅ |
| `IntentRejected` feedback event into Rust strategies | `runner/src/lib.rs:389/1632/1765`, `live-runner/src/main.rs:727` | ✅ (rust-only, see P3) |
| Sleeve spec required for `--live-submit` | `live-runner/src/main.rs:96/383/892/969` | ✅ |

These are **load-bearing assumptions for downstream phases**. Do not regress them.

### Partially implemented, still open

| Item | Where | Gap |
|---|---|---|
| Feedback events for strategy state (V3 A4, hyper P3.1) | Rust runner only | Python strategies still pre-mark `_open_buy_orders`/`_active_notional`/`pending_client_order_id` at emit time. See `python/.../plugins/strategies/microstructure_obi_scalper.py:152`, `weather_temperature_arbitrage.py:253`, `sports_tennis_xgboost.py:129`. |
| Reconciliation completeness | `live-runner/src/main.rs:336-440`, `kalshi/src/rest.rs:420` | Open-orders paginated, but **no position fetch, no balance fetch, no fills-since-checkpoint, no daily-realized-loss restoration** (see P2 below). |
| Adoption refreshes runtime state | `gateway/src/lib.rs::adopt_resting_order` ~1145 | Does **not** seed `sleeve_state.last_quote_epoch_secs[instrument]`, so the first cancel/intent after restart fails `MissingMarketData` until a fresh quote arrives. |
| `OnnxScorer` threading | `model-runtime/src/lib.rs:67-75` | `parking_lot::Mutex<Session>` serializes inference; `intra_op_num_threads` never set, so ort spawns Rayon pool that fights the Tokio scheduler. |
| Blocking REST submit on data loop | `kalshi/src/venue_client.rs:62/95/115/135`, `live-runner/src/main.rs:678` | Uses `block_in_place` from the WS loop; submits and cancels stall ingestion. |

### Not implemented at all (carried from prior audits)

* **`DecisionPayload::ReplaceOrder`** — confirmed absent. Re-quote = cancel + new. (V3 A3.)
* **Partial-fill `remaining` exposed to strategies** — `StrategyEvent::OwnFill` / `HotOwnFill` have no `remaining` field. (V3 A4.)
* **Market `Suspended`/`Halted` state** — no enum, no risk gate, no auto-cancel. (Hyper P6.5.)
* **Atomic cross-venue execution groups** — `arbitrage_cross_venue.py:108-130` still legs independently. (Hyper P6.1.)
* **Exact Kalshi fee curve in Rust** — Rust treats `fill.fee` as a venue-returned string and has no pre-trade fee model. Strategies cannot compute `executable_edge = fair - ask - fee` in Rust. (Hyper P6.2 partial.)
* **Margin netting for YES+NO** — local risk treats both as gross consumption; venue offsets are not modelled. (Hyper P6.3.)
* **Receipt-time vs event-time strict enforcement for external snapshots** — backtest `_event_time()` reads `received_at`/`exchange_ts` interchangeably. (Hyper P6.4.)
* **Margin tiering / cancel-low-priority-to-make-room** — risk rejects on `available_cash` regardless of conviction. (Hyper P6.9.)
* **Discretization bias** — strategies emit prices via `round()` without enforcing `floor()` for buy / `ceil()` for sell. (Hyper #8.)
* **Prometheus metrics endpoint** — only exit-time JSON dump. (V3 H1.)
* **State snapshot for crash recovery** — OMS/sleeve/ledger live in process memory only. (V3 H4.)
* **Strategy skeleton/template doc** — researchers must reverse-engineer from 26 plugins. (V3 D4.)
* **`OnnxQuoteStrategy` not in `default_registry()`** — only live-runner builds it. (V3 D2.)
* **Spec param validation against typos** — `parameters.get("foo", default)` silently swallows typos. (V3 D3.)
* **Demo Kalshi integration test in CI** — none. (V3 G3.)
* **Criterion benchmarks** — declared but no implementations land yet. (V3 G4.)
* **Runbooks for the common failure modes** — partial. (V3 H3.)
* **Raw private-event redaction / TTL** — `parquet_store.py:166` writes `payload_json` verbatim for `fill` and `order` channels. (V3 E1.)
* **`KALSHI_PRIVATE_KEY_PEM` inline alternative** — only `_PATH` supported. (V3 E2.)
* **Python dep pinning + `pip-audit` in CI** — `requirements.txt` uses `>=` only; CI runs `cargo-audit` but not `pip-audit`. (New finding, this pass.)
* **Branch protection / signed commits / SBOM** — none observed. (New finding, this pass.)

---

## §2. New Findings From This Pass

Findings here are either NOT covered by V3 or "hyper", or they refine a prior finding with specific lines and a concrete remediation. Every finding has a severity, a citation, an impact, a fix, and a test.

Severity scale: **C** critical (capital-loss risk now), **H** high (wrong numbers now), **M** medium (operationally fragile), **L** low (cleanup / future-friction).

### N1. [H] Rust `gateway` has no pre-trade fee model — `executable_edge` is approximate

**Where:** `rust/crates/oms/src/lib.rs:186` (`fee: String`), `gateway/src/lib.rs:1070` (parses `fill.fee` *post-fill*), `risk/src/lib.rs` (no fee field on intent path).

**Observed:** The fee on a `Fill` is the venue-returned string, taken as truth after the fill. The strategy's pre-trade `executable_edge` calculation has nothing to subtract because Rust has no `kalshi_fee_for(price_ticks, qty)` function. Compare Python `python/.../adapters/venues/kalshi/fees.py:45-48` which has the exact `ceil(100 * 0.07 * p * (1-p) * qty)` formula.

**Impact:** A Rust strategy that thinks it has `fair - ask = 1¢` of edge actually has 1¢ minus 7%·p·(1-p)·qty fee, which on a 50¢ market is 1.75¢ per contract. Edge is consistently overestimated, especially near 50¢. Strategies built in Python and ported to Rust will look identical on paper and degrade in live.

**Fix:**
1. Add `rust/crates/risk/src/fees.rs`:
   ```rust
   /// price_ticks in 1e4 dollar scale, qty in whole contracts (i64).
   /// Returns fee in 1e4 dollar ticks rounded UP to the nearest cent (100 ticks).
   pub fn kalshi_taker_fee_ticks(price_ticks: i64, qty: i64, rate_bps: u32) -> i64 { ... }
   ```
   Default `rate_bps = 700` (0.07).
2. Plumb a `FeeModel` into `RiskGate::evaluate_intent` so the projected edge subtracts fees before approving.
3. Mirror Python's maker-rebate-implicit branch.

**Test:** `kalshi_fee_matches_python_at_known_points` — for `(price=$0.50, qty=1)` expect 200 ticks ($0.02), for `(price=$0.10, qty=1)` expect 100 ticks ($0.01) per the ceil rule; cross-check by calling Python from a separate parity case.

**Effort:** 4h.

---

### N2. [C] Adoption does not refresh `last_quote_epoch_secs` — adopted instruments are dead until first quote

**Where:** `rust/crates/gateway/src/lib.rs::adopt_resting_order` ~1145 (and Python `oms/state.py`).

**Observed:** Adoption inserts position + reservation but does not seed `state.last_quote_epoch_secs[instrument_id]`. The risk gate's freshness check then treats those instruments as quote-less (age = `now - 0`), which exceeds `max_market_data_age_ms` by definition.

**Impact:** After a crash + `--reconcile-on-start`, every intent for an adopted instrument is rejected as `stale_market_data` until a fresh quote arrives. For a low-volume Kalshi market that's minutes; the strategy effectively can't manage its adopted exposure for that window. Cancels happen to skip risk (good), but place-intents — including the strategy's intended "I want to exit this adopted position" intents — are dead-on-arrival.

**Fix:** In `adopt_resting_order`, set `state.last_quote_epoch_secs[instrument_id] = now_epoch_secs` and tag with metric `eventcontracts_adoption_quote_seeded_total{instrument=...}` so operators can see seed events. Return the set of newly-adopted instrument IDs from the function; live-runner must add each to the WS subscription list before resuming the main loop.

**Test:** `adopted_order_does_not_block_intent_for_stale_quote`. Adopt order on `kalshi:X`. Without sending a quote, emit a place intent on `kalshi:X`. Assert risk approves (or rejects for an unrelated reason, but never `stale_market_data`).

**Effort:** 3h.

---

### N3. [H] Reconciliation does not restore `daily_realized_loss` — daily loss cap circumventable via crash+restart

**Where:** `rust/crates/live-runner/src/main.rs:336-440` and `rust/crates/gateway/src/lib.rs::adopt_resting_order`.

**Observed:** Adoption restores open orders. It does not fetch fills since UTC midnight and does not restore `state.daily_realized_loss` or `daily_loss_day_utc`. A restart resets both to zero.

**Impact:** A strategy can hit `max_daily_loss = $500`, get gated, the operator kills + restarts (or the process crashes), and the new process grants the strategy a fresh $500 budget. Worst case the daily loss cap is effectively `N × max_daily_loss` for `N` restarts in a day. This is the kind of bug that only fires under stress.

**Fix:**
1. Extend `KalshiRest` with `list_fills_since(epoch_secs)` (paginated like `list_open_orders`).
2. In live-runner reconcile path, call `list_fills_since(midnight_utc_today)`. Sum realized PnL contributions (using actual fill prices and outcome side) and seed `state.daily_realized_loss` accordingly.
3. Persist `last_reconcile_checkpoint_epoch` so a second restart in the same day doesn't double-count.

**Test:** `daily_loss_restored_across_restart`. Mock REST returns one fill that locks in $100 loss today. Reconcile. Assert `daily_realized_loss >= $100`. Submit intent that would push past `max_daily_loss=$500`. Assert reject reason `max_daily_loss`.

**Effort:** 1 day.

---

### N4. [H] Python strategy `_active_notional` is incremented on emit but only decremented on settlement

**Where:** `python/src/eventcontracts/plugins/strategies/weather_temperature_arbitrage.py:82, 253, 351`.

**Observed:** `_active_notional += notional` on emit (line 253). Decrement is only on settlement (line 351). Cancels and risk-rejects leave the budget permanently consumed.

**Impact:** A strategy that gets a few risk rejects in a row exhausts its self-imposed budget and stops trading even though no exposure exists. This is the most user-visible symptom of "emit means pending"; it survives the V3+hyper audits because it's specific to one strategy's bespoke bookkeeping.

**Fix:** Part of P3 (feedback events). Strategy releases `_active_notional` on `IntentRejected`, `VenueRejected`, `OwnOrderCanceled`. The clean refactor is to drop strategy-local notional bookkeeping entirely and read it from `ctx.exposure(sleeve_id)` instead, but the minimal fix is: subscribe to feedback events and release.

**Test:** `weather_arb_releases_notional_on_risk_reject`. Emit intent, force risk to reject (max_spread breach). Assert `_active_notional == 0` after feedback.

**Effort:** 2h after P3 lands.

---

### N5. [H] OBI scalper cleanup-on-emit races concurrent cancels

**Where:** `python/src/eventcontracts/plugins/strategies/microstructure_obi_scalper.py:152` (set) / `:171` (pop).

**Observed:** `_open_buy_orders[instrument] = coid` is written on the place-emit path; pop happens on the cancel-emit path. If two cancel events fire close together for the same instrument the second `pop()` raises `KeyError` (or with `pop(..., None)` silently does nothing while the gateway processes both cancels).

**Impact:** Sporadic strategy-level exception under burst, or silent double-cancel of a non-existent ID into the gateway (which currently logs and discards). Not capital-loss but degrades observability.

**Fix:** Move the pop to the feedback-event handler — `OwnOrderTerminal` / `IntentRejected` only. Part of P3.

**Test:** `obi_scalper_handles_concurrent_cancel_decisions`. Inject two cancels in the same `on_event` tick; assert no exception, exactly one in-flight cancel.

**Effort:** 1h after P3.

---

### N6. [H] No suspension / halted-market gate

**Where:** Risk gate (`risk/limits.py`, `risk/src/lib.rs`), gateway (`gateway/src/lib.rs`), live-runner WS handler (`live-runner/src/main.rs:550-610`).

**Observed:** There is no `MarketState::Suspended` event variant in `HotEvent`, no flag on `SleeveState.market_state`, no risk gate that rejects suspended markets, and no automatic cancel-all on resting orders when a market suspends.

**Impact:** Kalshi can pause a market (e.g. court order, postponement, settlement disputes). The BBO freezes at last quote; the strategy keeps treating it as live and either fires intents the venue silently rejects or, worse, holds passive resting orders that fill the moment the market resumes (potentially after material new information).

**Fix:**
1. Add `HotEvent::MarketState { instrument_id, state: MarketState }` where `MarketState ∈ {Active, Suspended, Halted, Settled, Closed}`.
2. Map Kalshi's market-status messages in `kalshi/src/normalize.rs`.
3. On `Suspended|Halted|Closed`, gateway cancels all own resting orders on that instrument and risk gate rejects new place intents with reason `market_suspended` until `Active` returns.
4. Strategies clear instrument-local signal buffers on `Suspended` and re-warm on `Active`.

**Test:** `suspended_market_rejects_new_intents_and_cancels_resting`. Pre-state: 1 resting order. Inject `MarketState::Suspended`. Assert: 1 cancel issued, next place intent on the same instrument rejected with `market_suspended`.

**Effort:** 1 day.

---

### N7. [H] Cross-venue arbitrage has no atomic execution group

**Where:** `python/src/eventcontracts/plugins/strategies/arbitrage_cross_venue.py:108-130` and Rust gateway has no `LinkedIntents` type.

**Observed:** The strategy emits independent intents for two venues. If Kalshi fills and Polymarket rejects (network, nonce collision, book moves), the position is net long Kalshi YES with no offsetting NO — naked directional delta where the strategy thought it locked in a fixed spread.

**Impact:** The "arbitrage" risk profile (very small max loss assumed) doesn't reflect actual unhedged-leg risk. The sleeve can blow past `max_position_notional` because both legs were sized as if the spread was locked.

**Fix:**
1. Add `AtomicExecutionGroup` to `domain/decisions.py`: `group_id: UUID`, `legs: tuple[PlaceOrder, ...]`, `legging_risk_cap_notional: Decimal`.
2. Risk gate reserves `legging_risk_cap_notional` against the sleeve for the duration of the group.
3. Gateway tracks group state: emit leg 1, on ack emit leg 2; on leg 2 failure within `cancel_window_secs`, auto-cancel leg 1.
4. Strategy `arbitrage_cross_venue.py` constructs an `AtomicExecutionGroup` instead of two free intents.

**Test:** `arb_legging_cancels_leg_one_when_leg_two_rejects`. Mock venue A accepts leg 1; venue B rejects leg 2; assert leg 1 cancel emitted within `cancel_window_secs` and risk reserves released.

**Effort:** 1.5 days.

---

### N8. [H] Discretization bias — strategies round prices without side-aware rounding

**Where:** All 26 plugins; the convention should live in `domain/decisions.py` or a `strategy/pricing.py` helper.

**Observed:** A model produces continuous fair value, e.g. `0.456`. Strategies that round to nearest tick produce `46c` on a buy bid, which is one tick **above** what the math says — adverse selection. Symmetrically a sell of `0.554` rounded to `55c` is one tick **below** fair. Spot check `weather_temperature_arbitrage.py:141` uses `mid` directly with no explicit floor/ceil.

**Impact:** Systematic ~½-tick bias per round trip on any model strategy. On 1¢-tick markets that's 1% drag.

**Fix:** Add `strategy/pricing.py` helpers:
```python
def floor_to_tick(price: Decimal, tick: Decimal) -> Decimal: ...
def ceil_to_tick(price: Decimal, tick: Decimal) -> Decimal: ...
def buy_limit_from_fair(fair: Decimal, tick: Decimal) -> Decimal:
    return floor_to_tick(fair, tick)
def sell_limit_from_fair(fair: Decimal, tick: Decimal) -> Decimal:
    return ceil_to_tick(fair, tick)
```
Refactor every model-driven strategy (CPI, NFP, Fed, Tennis, Golf, Box Office, etc.) to use these. The OBI scalper and other passive strategies use BBO directly; they don't need this. Mirror in Rust as `runner/src/pricing.rs`.

Add a lint test: any strategy module that imports `Decimal` and emits a price computed from a model must use `buy_limit_from_fair` / `sell_limit_from_fair` (use an AST check, like the existing `test_strategy_promotion_guards.py`).

**Test:** `model_buy_floors_to_tick` / `model_sell_ceils_to_tick`. Property tests with random fairs.

**Effort:** 6h.

---

### N9. [H] Live-paper CLI has no sleeve risk loading

**Where:** `python/src/eventcontracts/cli/live_paper.py` (verified via the agent walk — no `--sleeve` flag and no risk-gate composition).

**Observed:** Backtest CLI (`cli/backtest.py:186-200`) loads sleeve TOML and builds a risk gate. Live-paper CLI does not. The "paper" mode is effectively running strategies with strategy-level limits only and no portfolio guard.

**Impact:** Paper runs do not catch portfolio-cap or daily-loss breaches that the live runner would catch, so a strategy that backtests fine and passes paper can still blow up in live. Worse, this asymmetry confuses promotion: paper green ≠ live safe.

**Fix:** Add `--sleeve` (required) to live-paper. Build `SleeveRiskGate` the same way backtest does. Refuse to start without it (allow `--allow-no-sleeve` only for the smallest smoke tests, with a stderr warning).

**Test:** `live_paper_requires_sleeve_spec`. Invoke without `--sleeve` → exit non-zero.

**Effort:** 3h.

---

### N10. [H] Tokio worker blocked in `block_in_place` on every venue submit

**Where:** `rust/crates/kalshi/src/venue_client.rs:62, 95, 115, 135`. `block_in_place` is invoked inside `VenueClient::submit/cancel/cancel_all`, which is itself called from the gateway's `process_batch`, which is in turn called inline from the live-runner WS loop (`live-runner/src/main.rs:678`).

**Observed:** Every submit ties up one Tokio worker for the full REST round-trip (~100-500ms). Ingestion (WS messages, BBO updates) waits.

**Impact:** Under a burst — multiple strategies firing simultaneously after a quote — the system stops ingesting market data for the duration of the burst's submits. By the time the worker is free again, the BBO snapshot used by the *next* decision is stale.

**Fix:**
1. Replace `block_in_place` with a real async `VenueClient` trait (`async-trait`).
2. Split live-runner into three loops:
   - **Ingestor**: WS read, normalize, project to `HotEvent`, push to a bounded `tokio::sync::mpsc<HotEvent>`. Update an `Arc<ArcSwap<BboMap>>` atomically.
   - **Decider**: drains the channel, runs strategies + risk + portfolio + last-look, pushes accepted `IntentEnvelope` to a bounded `mpsc<Intent>`.
   - **Executor**: drains the intent channel, performs send-time last-look against `Arc<ArcSwap<BboMap>>` (so the price-reference is the freshest available, not the decider's snapshot), submits.
3. Apply backpressure: if intent channel is full, drop oldest non-cancel intents and emit a metric `eventcontracts_intent_shed_total`. Cancels are never shed.

**Test:** Replay a high-rate WS fixture with a venue submit fixture that sleeps 200ms. Assert WS ingestion lag stays under 5ms over the run.

**Effort:** 3 days (the biggest architectural change in this spec).

---

### N11. [M] `OnnxScorer` serializes inference via `parking_lot::Mutex<Session>` and never sets `intra_op_num_threads`

**Where:** `rust/crates/model-runtime/src/lib.rs:67-75` and ONNX session construction.

**Observed:** ort 2.x `Session::run` requires `&mut self`. The code wraps in a mutex. Two strategy workers each holding `Arc<OnnxScorer>` serialize. Additionally, ort's default is to spawn a Rayon pool sized to logical cores for intra-op parallelism, which on small models (linear, XGBoost-style tree) costs more than it saves and fights the Tokio runtime.

**Impact:** Tail-latency spike per inference; head-of-line blocking under concurrency.

**Fix:**
1. Configure session with `SessionBuilder::with_intra_threads(1)`.
2. Add `OnnxScorerPool` (V3 C1 design): `Vec<Mutex<Session>>` plus `AtomicUsize` round-robin counter.
3. Bench single vs pool-of-4 under 8 concurrent calls; pool should be ≥2× faster.

**Test:** `criterion` bench in `model-runtime/benches/scorer.rs`.

**Effort:** 4h.

---

### N12. [M] No reconcile-budget kill — WS reconnect loop trades forever on stale data

**Where:** `rust/crates/live-runner/src/main.rs::reconnect_ws` (around line 458 per agent walk).

**Observed:** Reconnect attempts increment but there's no hard cap that engages the kill switch. A long network partition leaves the runner cycling reconnects while strategies act on increasingly stale state.

**Impact:** Silent stale-state trading.

**Fix:** After `N` consecutive reconnect attempts without a successful WS message (default 10), engage `sleeve_state.kill_switch_engaged = true`, attempt `cancel_all`, and exit non-zero. Emit `eventcontracts_reconnect_budget_exhausted_total`.

**Test:** `reconnect_budget_exhausted_engages_kill_switch`. Mock WS that always fails connect. Assert kill_switch toggled and exit after N attempts.

**Effort:** 2h.

---

### N13. [M] Reconnect-budget exhausted is silent (carried L from V3 B5, now upgraded to M)

Combined with N12.

---

### N14. [M] `OnnxQuoteStrategy` not in `runner::default_registry()`

**Where:** `rust/crates/runner/src/registry.rs::default_registry` (omits `onnx_quote`); `rust/crates/live-runner/src/main.rs:301` registers it manually.

**Impact:** Parity tests and other consumers can't find `onnx_quote` by name.

**Fix:** Move `build_onnx_quote_from_spec` factory into a new `runner-onnx` crate (or into `model-runtime` since it already depends on ort). Register from `default_registry()`. Live-runner deletes its bespoke registration.

**Test:** `default_registry_includes_onnx_quote`.

**Effort:** 2h.

---

### N15. [M] Spec param typos silently swallowed

**Where:** All strategy plugins call `spec.parameters.get("key", default)`.

**Impact:** A typo `imbalance_threshhold` (typo: extra h) silently uses the default while looking correct in TOML.

**Fix:** In `StrategyBase.__init__` (Python) and `StrategyRuntime::from_spec` (Rust), validate that every key the strategy *consumed* from `spec.parameters` is matched by exactly one key in the TOML — and that no key is present in the TOML that wasn't consumed. The pattern is "declare expected, validate at construction".

Python implementation:
```python
class StrategyBase:
    expected_params: ClassVar[frozenset[str]] = frozenset()
    def __init__(self, spec):
        unknown = set(spec.parameters) - self.expected_params
        if unknown:
            raise SpecError(f"unknown spec keys: {sorted(unknown)}")
        ...
```

**Test:** `strategy_spec_with_unknown_param_raises`.

**Effort:** 3h (mostly mechanical updates per strategy).

---

### N16. [M] No `--dry-run-against-fixture` for live-runner

**Where:** `rust/crates/live-runner/src/main.rs`.

**Observed:** Either `--live-submit` (real venue calls) or omit-it (no submit). No middle ground that exercises the full code path but writes intents to a JSONL file and validates wire-format.

**Fix:** `--dry-run-fixture <path>` flag. Loads a parquet/JSONL of recorded BBO + lifecycle events, plays them through ingestor → decider → executor with a `RecordingVenueClient` that validates Kalshi REST schema and writes outbound payloads to `<out>/intents.jsonl`. Operator can diff this output before flipping to `--live-submit`.

**Test:** `dry_run_records_intents_without_venue_call`. Mock fixture with a known intent-producing scenario; assert outbound JSONL has the expected `client_order_id`.

**Effort:** 1 day.

---

### N17. [M] No Prometheus metrics — operators flying blind during a run

**Where:** `rust/crates/live-runner/src/main.rs:1414-1472` writes a JSON file at exit.

**Fix:** Add `prometheus` crate. Expose `/metrics` on `--metrics-port` via a tiny `axum` listener. Histograms (microseconds):
- `eventcontracts_gateway_process_one_us`
- `eventcontracts_last_look_age_secs`
- `eventcontracts_scorer_predict_us`
- `eventcontracts_ws_lag_us` (now - last successful WS read epoch)

Counters:
- `eventcontracts_intents_emitted_total{strategy,decision}`
- `eventcontracts_intents_rejected_total{reason}`
- `eventcontracts_fills_total{instrument,side}`
- `eventcontracts_kill_switch_engaged_total`
- `eventcontracts_reconnects_total`

Gauges:
- `eventcontracts_open_orders`, `eventcontracts_daily_realized_loss_cents`

**Test:** `metrics_endpoint_serves_known_counters`. Spin up runner, hit `/metrics`, assert known counters present.

**Effort:** 6h.

---

### N18. [M] Audit chain has no persistent backing store

**Where:** `python/src/eventcontracts/audit.py:73-86` defines `AuditTrail` protocol; no concrete store under `python/src/eventcontracts/audit/`.

**Impact:** `record_prediction` and intent emission compute `canonical_sha256` correctly, but the lineage edges (event → prediction → intent → fill) aren't persisted. Incident replay can't reconstruct "what did the strategy see when it decided X."

**Fix:** Implement `JsonlAuditStore` writing to `data/audit/<date>/audit-<run_id>.jsonl` with one line per stamp. Add Rust-side mirror in `runner` (the gateway already computes the hashes). Add a tool `eventcontracts audit-trace <intent_id>` that walks the chain.

**Test:** `audit_chain_traversal_returns_full_lineage`.

**Effort:** 1 day.

---

### N19. [M] No state snapshot for crash recovery (carried V3 H4)

**Where:** Nothing persists `OMS.orders`, `sleeve_state.positions`, `sleeve_state.daily_realized_loss`, `IdempotencyStore.seen`, or the audit chain in memory beyond process lifetime.

**Fix:** `--snapshot-interval-secs <N>` writes a compact JSON snapshot to `--snapshot-path`. On startup, if a snapshot is fresh (<5min), load it before reconcile-on-start; reconcile then validates rather than replacing. Snapshot version field for forward compat.

**Test:** `snapshot_round_trip_preserves_state`.

**Effort:** 1 day.

---

### N20. [M] Raw private events (own fill, own order) stored unencrypted with no TTL

**Where:** `python/src/eventcontracts/storage/parquet_store.py:166` writes `payload_json` verbatim. Live subscriptions include `fill` and `order` channels.

**Impact:** `data/raw/venue=kalshi/source=ws/date=YYYY-MM-DD/*.parquet` accumulates account-bound data. No retention. No encryption at rest. Anyone who gains read access to `data/` gets a complete account-trace.

**Fix:**
1. `RedactionPolicy` per (venue, channel). For `kalshi-ws/fill` and `kalshi-ws/order`, store SHA-256 of `payload_json` + a stable subset (no account_id, no client_order_id-to-trader mapping) instead of the full payload.
2. TTL: `data/raw/<date>/` older than `--raw-retention-days` (default 30) gets `tar+age`-encrypted and moved to `data/archive/`. Hard-delete after `--archive-retention-days` (default 365).
3. Document in `docs/runbooks/data-retention.md`.

**Test:** `parquet_store_redacts_private_payloads`. Write a synthetic fill envelope; reread; assert sensitive fields are hashed.

**Effort:** 1 day.

---

### N21. [M] `KALSHI_PRIVATE_KEY_PATH` only — no `KALSHI_PRIVATE_KEY_PEM` inline option

**Where:** `python/src/eventcontracts/adapters/venues/kalshi/client.py:71`, `rust/crates/kalshi/src/auth.rs:115`.

**Impact:** Prod typically wants the secret manager to inject PEM bytes directly into the env, not write to disk. Disk-resident keys have a larger attack surface (process listings, container layer cache, swap).

**Fix:** Accept `KALSHI_PRIVATE_KEY_PEM` (PEM bytes) as an alternative; precedence: `_PEM` over `_PATH`. Reject if both set with non-equal contents. Document in `docs/runbooks/credential-rotation.md`.

**Test:** `kalshi_auth_loads_from_pem_env_var`.

**Effort:** 2h.

---

### N22. [M] Python deps unpinned; no `pip-audit` in CI

**Where:** `python/requirements.txt` uses `>=` constraints throughout. `.github/workflows/quality.yml` runs `cargo-audit` (Rust) but no `pip-audit` (Python).

**Impact:** Two reproducibility/security gaps:
- A `pip install` today can pull a different `pydantic` patch than the last CI green run.
- A CVE in `cryptography` or `httpx` won't fire any alert.

**Fix:**
1. Replace `>=` with `==` in `requirements.txt`. Generate from a constraint resolver (`pip-compile` from `pip-tools`) so it's reproducible.
2. Add `pip-audit -r python/requirements.txt --strict` to `quality.yml`.
3. Add `--check-hashes` in CI install step.

**Test:** Push a PR pinning to a known-CVE version; assert CI fails.

**Effort:** 3h.

---

### N23. [M] No live-promotion manifest linking strategy ↔ sleeve ↔ parity ↔ external replay

**Where:** Nothing today. `configs/strategies/*.toml` and `configs/sleeves/*.toml` are loose-related by file naming.

**Impact:** A researcher promoting a Python strategy to Rust must manually find: the sleeve spec, the parity fixtures, the external replay fixtures, the runbook. Easy to miss one.

**Fix:** `configs/promotion/<strategy_name>.toml`:
```toml
strategy_spec = "configs/strategies/sports-tennis-xgboost.toml"
sleeve_spec = "configs/sleeves/sports-tennis-kalshi-paper-a.toml"
parity_dir = "contracts/parity/sports_tennis_xgboost"
external_replay = ["contracts/replay/tennis/match_001.parquet"]
runbook = "docs/runbooks/sports-tennis.md"
required_fixtures = ["normal_quote", "partial_fill", "risk_reject", "suspension", "sequence_gap"]
adversarial_fixtures = ["spoof_cancel", "quote_stuffing"]
```
CI gate: every spec in `configs/strategies/` that's listed in `configs/promotion/registry.toml` must have a manifest, and that manifest's referenced files must exist.

**Test:** `promotion_manifest_referenced_files_exist`.

**Effort:** 1 day.

---

### N24. [M] No criterion benches in repo (carried V3 G4)

Add at least:
- `gateway::process_one` end-to-end (place_order approved).
- `risk::evaluate` for 100-instrument state.
- `runtime_hot::project_event` for quote, trade, book.
- `OnnxScorer::predict` for a 20-feature input.
- `canonical_sha256` for a 7-field intent envelope.

CI runs `cargo bench --workspace --no-run` (compile only) on every PR; nightly job runs the real benches and compares against `benches/baseline.json`. Fail nightly job on >25% p99 regression.

**Effort:** 1 day.

---

### N25. [M] No Kalshi demo integration smoke test in CI (carried V3 G3)

Opt-in `cargo test --features kalshi-demo-integration` that places + cancels one IOC order at $0.01 on demo. Requires `KALSHI_DEMO_KEY_ID` + `KALSHI_DEMO_PRIVATE_KEY_PEM` in CI secrets. Run weekly.

**Effort:** 1 day.

---

### N26. [L] Dual `FixedPrice` scales (1e6 vs 1e4) — confusing but not buggy

Carried from V3 F3 and the agent walk. Document in the type's rustdoc; add `#[must_use]` on conversion functions. Long-term: unify on 1e4 (Kalshi cent ticks are 1e2; 1e4 supports ¼-cent and is plenty); this is multi-week. Defer.

---

### N27. [L] `bus` and `allocator` crates compiled but unused in prod

Move under `dev-dependencies` of `runner` or document in their `lib.rs` headers that they're forward-looking. Either way, the build cost is non-zero. Defer to a later cleanup PR.

---

### N28. [L] Per-quote `String` clone for `last_quote_epoch_secs` key (carried V3 C6)

Use `entry().or_insert_with` + `as_str()` lookup, or change the map key to `SmolStr`. Real win at sustained 5kHz+ quote rates.

---

### N29. [L] Repeated `epoch_seconds(now)` per intent (carried V3 C7)

Parse once at the top of `process_one`, plumb `now_epoch_secs: i64` downstream.

---

### N30. [L] `format!("c-onnx-yes-{:08}", n)` per decision (carried V3 C4)

Acceptable as-is.

---

### N31. [L] No branch protection / signed commits / SBOM

`gh api repos/.../branches/main/protection` is empty. No `cyclonedx`/`syft` step in CI. Configure: require PR review for main, require signed commits, generate SBOM in release builds and upload as an artifact. Operational, not blocking.

---

## §3. Phased Implementation Plan

Each phase has its own preflight and validation. **Always run the §0 baseline before and after.** Phases assume the prior one landed and is green on `main`.

### Phase **P1** — Adoption / freshness / daily-loss correctness (Day 1-2) — **C / H**

Items: **N2**, **N3**, V3 **B2**.

Order within phase:
1. `gateway::adopt_resting_order` seeds `last_quote_epoch_secs` and returns `Vec<InstrumentId>` of newly-adopted instruments (N2).
2. `live-runner::reconcile_on_start` extends the WS subscription with the returned set before resuming (V3 B2).
3. `KalshiRest::list_fills_since(epoch_secs)` with pagination (N3).
4. `live-runner::reconcile_on_start` fetches today's fills, seeds `daily_realized_loss` (N3).

Tests:
- `adopted_order_does_not_block_intent_for_stale_quote`
- `adoption_subscribes_runner_to_adopted_instruments`
- `daily_loss_restored_across_restart`

Verify: full §0 baseline + manually run `--reconcile-on-start` against demo with one fresh fill; observe `daily_realized_loss_cents` gauge shows the expected value.

### Phase **P2** — Pre-trade fee model in Rust (Day 3) — **H**

Items: **N1**.

Order:
1. New `risk/src/fees.rs::kalshi_taker_fee_ticks`.
2. Wire into `RiskGate::evaluate_intent` so the projected edge is `fair - ask - fee` for buys, `bid - fair - fee` for sells (where `fair` comes from the strategy's `market_snapshot`).
3. Parity case: known `(price, qty)` → Python fee == Rust fee. Add to `contracts/parity/fees/`.

Verify: parity green, Rust strategy tests still pass, new bench `kalshi_taker_fee_ticks` runs.

### Phase **P3** — Feedback events for strategy state (Day 4-5) — **H**

Items: **N4**, **N5**, hyper P1.2/P3.1 (Python side).

Order:
1. Python `runner` emits one of `{IntentAccepted, IntentRejected, VenueAcked, VenueRejected, OwnFill, OwnOrderTerminal}` to each strategy after every decision.
2. `StrategyBase` exposes an `on_feedback(event)` hook (no-op by default).
3. Refactor `microstructure_obi_scalper`, `weather_temperature_arbitrage`, `sports_tennis_xgboost` to release local pending state on feedback only.
4. Mirror Rust callback structure exists; ensure all Rust strategies subscribe (`tennis`, `onnx_quote`).

Tests:
- `weather_arb_releases_notional_on_risk_reject`
- `obi_scalper_handles_concurrent_cancel_decisions`
- `tennis_clears_pending_on_intent_reject` (already present per hyper §I.P1.2 verification — preserve it)
- Parity: stateful sequence including a risk reject.

Verify: full §0 baseline; specifically `pytest tests -q` and `cargo test --workspace` go up by the count of new tests.

### Phase **P4** — Suspension / lifecycle gate (Day 6) — **H**

Items: **N6**.

Order:
1. `HotEvent::MarketState`; `runtime-hot::project_event` handles new variant.
2. `kalshi::normalize` maps Kalshi market-status messages.
3. `gateway::process_one` rejects `PlaceOrder` with `market_suspended` when state ≠ Active.
4. `gateway::on_market_state_change` auto-cancels resting orders on `Suspended|Halted|Closed`.
5. Strategies invalidate signal buffers on `Suspended` (a new `on_market_state(state)` hook).

Tests: `suspended_market_rejects_new_intents_and_cancels_resting`.

Verify: baseline + the new test.

### Phase **P5** — Discretization bias + price helpers (Day 7) — **H**

Items: **N8**.

Order:
1. `strategy/pricing.py` with `floor_to_tick`, `ceil_to_tick`, `buy_limit_from_fair`, `sell_limit_from_fair`.
2. Refactor every model-driven Python strategy to call these.
3. Mirror in `rust/crates/runner/src/pricing.rs`.
4. AST-style lint in `test_strategy_promotion_guards.py`: any model strategy that converts a `Decimal`/`f64` to a price must call the helpers.

Tests: `model_buy_floors_to_tick`, `model_sell_ceils_to_tick`, and the lint.

### Phase **P6** — Replace, partial-fill remaining, self-cross belt-and-suspenders (Day 8-9) — **H**

Items: V3 **A3, A4, A7**.

Order:
1. `DecisionPayload::ReplaceOrder { client_order_id, new_price, new_quantity }` plus Kalshi REST adapter; gateway emulates as atomic cancel-then-new when venue doesn't expose replace.
2. Extend `HotOwnFill::remaining`, `StrategyEvent::OwnFill { quantity, remaining, ... }`; populate from OMS post-fill state.
3. Strategies use `remaining` to decide re-price vs cancel.
4. Gateway already rejects self-cross on same-instrument same-side (verified §1); add belt-and-suspenders on **opposite** side same-instrument crossing — reject as `self_cross_opposite_side`.

Tests:
- `replace_emulates_cancel_then_new_atomically`
- `replace_aborts_new_if_fill_arrives_during_cancel_window`
- `partial_fill_exposes_remaining_to_strategy`
- `gateway_rejects_self_crossing_buy_against_own_sell`

### Phase **P7** — Async execution worker (Day 10-12) — **H**

Items: **N10**.

This is the largest change. Do it after Phases P1-P6 land so you have a green baseline and you're not stacking risk.

Order:
1. Convert `VenueClient` to `#[async_trait]`.
2. Split `live-runner::main` into `ingest_loop`, `decide_loop`, `execute_loop`.
3. `Arc<arc_swap::ArcSwap<BboMap>>` for live BBO; ingestor `store`s, executor `load`s pre-submit.
4. Bounded `tokio::sync::mpsc<HotEvent>` between ingest and decide; bounded `mpsc<IntentEnvelope>` between decide and execute. Channel capacities behind CLI flags; defaults `1024`.
5. Backpressure: on full intent channel, drop oldest non-cancel; emit `intent_shed_total`.
6. Send-time last-look in executor uses `BboMap.load()` (the freshest BBO, not the decider's snapshot).

Tests:
- `executor_drops_oldest_non_cancel_under_backpressure`
- `executor_send_time_last_look_uses_latest_bbo`
- Replay throughput: a fixture with 1000 events/s and synthetic 200ms REST delay produces no WS lag > 5ms.

Verify: bench p50/p95/p99 latency, compare against baseline; submit a parity-equivalent test against the parity suite.

### Phase **P8** — ONNX threading + pool (Day 13) — **M**

Items: **N11**.

Order:
1. `SessionBuilder::with_intra_threads(1)`.
2. `OnnxScorerPool { sessions: Vec<Mutex<Session>>, next: AtomicUsize }`.
3. Bench single vs pool-of-4.

### Phase **P9** — Atomic cross-venue execution group + margin tier (Day 14-16) — **H / M**

Items: **N7**, hyper **P6.9**.

Order:
1. `domain/decisions.py::AtomicExecutionGroup`; gateway state-machine for group commit.
2. Risk reserves `legging_risk_cap_notional` for group lifetime.
3. `arbitrage_cross_venue.py` constructs a group instead of two free intents.
4. Per-intent `priority_tier ∈ {Arbitrage, Momentum, Passive}`; when `available_cash` would block a higher-tier intent, gateway cancels the lowest-tier resting order in the same sleeve and retries once.

Tests:
- `arb_legging_cancels_leg_one_when_leg_two_rejects`
- `arbitrage_intent_preempts_passive_resting`

### Phase **P10** — Promotion manifest, parity v2, demo smoke (Day 17-18) — **M**

Items: **N23**, **N25**, V3 **G2**, hyper **P5**.

Order:
1. `configs/promotion/<strategy>.toml` schema + CI check that referenced files exist.
2. Stateful parity runner: sequence of `(NormalizedEvent, MockVenueResponse)` pairs; compare decision payload, priority tier, TTL, audit metadata, risk verdict, gateway verdict, strategy state at each step.
3. Demo Kalshi smoke (`cargo test --features kalshi-demo-integration`), weekly schedule.

### Phase **P11** — Observability / snapshot / hot-path perf (Day 19-21) — **M / L**

Items: **N12, N13, N14, N15, N16, N17, N18, N19, N24, N28, N29**.

Order:
1. `--metrics-port` Prometheus endpoint (N17).
2. Reconnect-budget kill switch (N12/N13).
3. `--snapshot-interval-secs` (N19).
4. `JsonlAuditStore` + `eventcontracts audit-trace` CLI (N18).
5. Spec param validation in `StrategyBase` + Rust `from_spec` (N15).
6. `--dry-run-fixture` flag (N16).
7. `OnnxQuoteStrategy` in `default_registry` (N14).
8. Criterion benches (N24).
9. Hot-path string/epoch micro-opts (N28/N29).

### Phase **P12** — Data security / supply chain (Day 22-23) — **M / L**

Items: **N20, N21, N22, N31**.

Order:
1. `KALSHI_PRIVATE_KEY_PEM` alternative (N21).
2. `RedactionPolicy` + TTL on raw partitions (N20).
3. Pin Python deps + `pip-audit` (N22).
4. Branch protection + SBOM (N31).

---

## §4. Per-Fix Test Efficacy Recipes

For each item, this is the exact answer to "how do I know my fix worked." `<repo>` = `C:/QWS/eventcontracts`.

### N1 — Rust fee model
```bash
cd <repo> && cargo test -p eventcontracts-risk --test fees -- --nocapture
cargo run -p eventcontracts-parity --bin parity_check -- --cases contracts/parity/fees
```
Manual: in a Python REPL compute `KalshiFeeModel().estimate(...)` for known `(p, q)`; rerun the Rust unit test for the same `(p, q)` — must match within 1 tick (rounding).

### N2 — Adoption seeds quote epoch
```bash
cargo test -p eventcontracts-gateway adoption -- --nocapture
```
Manual: leave one resting order on demo. Restart with `--reconcile-on-start`. Look for `eventcontracts_adoption_quote_seeded_total > 0`. Emit a place intent on the adopted instrument before the first quote — observe `intents_rejected_total{reason="stale_market_data"}` does NOT increment.

### N3 — Daily loss restored
```bash
cargo test -p eventcontracts-live-runner daily_loss -- --nocapture
```
Manual: trigger a synthetic fill that locks in 90% of `max_daily_loss`. Kill process. Restart with `--reconcile-on-start`. Emit an intent that would push 12% past the cap. Expect reject `max_daily_loss`.

### N4/N5 — Feedback releases strategy state
```bash
cd python && python -m pytest tests/test_strategy_runner.py::test_weather_arb_releases_notional_on_risk_reject -xvs
python -m pytest tests/test_strategy_runner.py::test_obi_scalper_handles_concurrent_cancel_decisions -xvs
```

### N6 — Suspension gate
```bash
cargo test -p eventcontracts-gateway market_state -- --nocapture
```
Manual: replay a Kalshi suspension fixture; observe `intents_rejected_total{reason="market_suspended"}` and `orders_canceled_total{reason="market_suspended"}` increment.

### N7 — Atomic execution group
```bash
cd python && python -m pytest tests/test_arbitrage_cross_venue.py -xvs
```

### N8 — Discretization
```bash
cd python && python -m pytest tests/test_strategy_pricing.py -xvs
python -m pytest tests/test_strategy_promotion_guards.py -xvs
```

### N9 — Live-paper requires sleeve
```bash
cd python && python -m pytest tests/test_cli.py::test_live_paper_requires_sleeve -xvs
```

### N10 — Async executor
```bash
cargo test -p eventcontracts-live-runner async_executor -- --nocapture
cargo bench -p eventcontracts-live-runner ws_lag_under_load
```
Watch `ws_lag_us` p99 < 5ms over the bench.

### N11 — ONNX pool
```bash
cargo bench -p eventcontracts-model-runtime scorer_serial_vs_pool
```
Expect pool-of-4 ≥ 2× single under 8 concurrent calls.

### N12/N13 — Reconnect kill
```bash
cargo test -p eventcontracts-live-runner reconnect_budget -- --nocapture
```

### N14 — OnnxQuote in registry
```bash
cargo test -p eventcontracts-runner default_registry_includes_onnx_quote
```

### N15 — Param typos
```bash
cd python && python -m pytest tests/test_strategy_specs.py::test_unknown_param_raises -xvs
```

### N16 — Dry-run fixture
```bash
cargo run -p eventcontracts-live-runner -- --dry-run-fixture contracts/replay/kalshi/sample.parquet --strategy-spec configs/strategies/example-threshold.toml --sleeve-spec configs/sleeves/example-kalshi-paper.toml --out /tmp/dry-run
diff <(jq -S . /tmp/dry-run/intents.jsonl) <(jq -S . contracts/replay/kalshi/sample.expected.jsonl)
```

### N17 — Prometheus
```bash
cargo run -p eventcontracts-live-runner -- --metrics-port 9090 ... &
sleep 2 && curl -s http://localhost:9090/metrics | grep eventcontracts_gateway_process_one_us
```

### N18 — Audit chain
```bash
cd python && python -m pytest tests/test_audit.py::test_chain_traversal -xvs
eventcontracts audit-trace <intent_id> --audit-dir data/audit/
```

### N19 — Snapshot
```bash
cargo run -p eventcontracts-live-runner -- --snapshot-interval-secs 2 --snapshot-path /tmp/snap.json ... &
sleep 5 && kill -9 $!
cargo run -p eventcontracts-live-runner -- --resume-snapshot /tmp/snap.json --reconcile-on-start ...
```
Assert OMS open count and daily_realized_loss restored.

### N20 — Redaction + TTL
```bash
cd python && python -m pytest tests/test_parquet_store.py::test_redacts_private_payload -xvs
```
Manual: inspect `data/raw/.../part-*.parquet` for a fill; assert no `account_id`.

### N21 — PEM env
```bash
cd python && python -m pytest tests/test_kalshi_auth.py::test_loads_from_pem_env -xvs
```

### N22 — Python deps pinned + pip-audit
```bash
pip-audit -r python/requirements.txt --strict
```
CI must run this and fail on advisory.

### N23 — Promotion manifest
```bash
cd python && python -m pytest tests/test_promotion_manifest.py -xvs
```

### N24 — Benches
```bash
cargo bench --workspace --no-run
cargo bench -p eventcontracts-gateway process_one
```

### N25 — Demo smoke
```bash
KALSHI_DEMO_KEY_ID=... KALSHI_DEMO_PRIVATE_KEY_PEM=... cargo test --features kalshi-demo-integration --test kalshi_live_smoke -- --nocapture
```

### Full minimum-efficacy suite (after every phase)
```bash
cd C:/QWS/eventcontracts
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace
cargo run --manifest-path rust/Cargo.toml -p eventcontracts-parity --bin parity_check -- --all-promoted
cargo bench --manifest-path rust/Cargo.toml --workspace --no-run
cd python && python -m compileall -q src tests
python -m ruff check src tests
python -m mypy src/eventcontracts tests
python -m pytest tests -q
cd ..
docker build -t eventcontracts:local .
pip-audit -r python/requirements.txt --strict
gitleaks detect --no-banner --redact
```

---

## §5. Final Live-Readiness Gate

No strategy is live-approved unless:

1. It emits only bounded IOC/GTC limit orders (V3 §F-confirmed via `test_strategy_promotion_guards.py`).
2. Every emitted order carries a fresh, side-correct `MarketSnapshot` (verified in risk).
3. It has a `configs/sleeves/*.toml` AND a `configs/promotion/<name>.toml` manifest (P10).
4. It has stateful parity cases that include: normal quote, partial fill, risk reject, suspension, sequence gap, spoof-cancel adversarial (P10).
5. It does not pre-mark local pending state — only feedback events advance it (P3).
6. Its model edge is `executable_edge = fair - ask - fee` not `fair - ask` (P2 / N1).
7. Its model prices buys with `floor_to_tick` and sells with `ceil_to_tick` (P5 / N8).
8. It honors `MarketState::Suspended` (P4 / N6).
9. It has a runbook in `docs/runbooks/<strategy>.md` with: symptoms of common failures, diagnostic commands, recovery steps, escalation contact.
10. The runner exposes Prometheus and is wired to an alerting webhook for `kill_switch_engaged_total > 0`, `last_look_reject_rate > 0.2 over 5min`, `reconnect_budget_exhausted_total > 0`.

---

## §6. What Is Explicitly Out Of Scope For V4

- **Polymarket live submit.** The framework supports it via the `VenueClient` trait but no production-grade Polymarket gateway exists yet (EIP-712 signing, nonce management, on-chain key custody). Out of scope.
- **WASM strategy sandbox** — only first-party strategy code is assumed.
- **Multi-sleeve sharing a single OMS/gateway process** — current arch is one sleeve per runner. Multi-sleeve in one process is a multi-week change; track separately.
- **Full async-trait `VenueClient` refactor across all crates.** P7 narrowly converts Kalshi; broader trait change can wait for a second venue.
- **Unifying `FixedPrice` scales (1e6 ↔ 1e4)** — risk/reward unfavorable until/unless a clear bug forces it.

---

## §7. Appendix — File Cheatsheet (current tree, this audit)

| Path | Role |
|---|---|
| `rust/crates/runtime-hot` | Hot-path `FixedPrice`/`Qty`/`HotEvent` projections |
| `rust/crates/oms` | Order state machine (rust_decimal) |
| `rust/crates/risk` | Stateful pre-trade risk + side-specific BBO |
| `rust/crates/gateway` | Single venue I/O choke point + portfolio + last-look + adoption |
| `rust/crates/kalshi` | Kalshi-specific WS/REST/VenueClient/auth/normalize |
| `rust/crates/runner` | Strategies + `StrategyRuntime` trait + `build_intent_envelope` |
| `rust/crates/feature-builder` | `Scorer` trait + quote-feature extractors |
| `rust/crates/model-runtime` | ONNX `OnnxScorer` (single mutex today) |
| `rust/crates/parity` | Parity case loader + runner |
| `rust/crates/live-runner` | Production binary (WS + reconcile + run loop) |
| `rust/crates/contracts` | Cross-language wire types + `canonical_sha256` |
| `rust/crates/bus` | Forward-looking IPC abstraction (not in prod use) |
| `rust/crates/allocator` | Forward-looking capital allocator (not in prod use) |
| `python/src/eventcontracts/domain` | Closed sum types, fees, fills, decisions, validation |
| `python/src/eventcontracts/strategy` | `StrategyBase`, context, registry |
| `python/src/eventcontracts/plugins/strategies` | 26 strategy plugins |
| `python/src/eventcontracts/risk` | Limits, policy, daily-loss ledger, state |
| `python/src/eventcontracts/execution` | Paper engine, market simulator, PnL, queue |
| `python/src/eventcontracts/gateway/base.py` | Python dry-run gateway base |
| `python/src/eventcontracts/oms/state.py` | Python OMS state |
| `python/src/eventcontracts/normalization` | Kalshi normalize + pipeline |
| `python/src/eventcontracts/replay/order_book.py` | Deterministic replay |
| `python/src/eventcontracts/storage/parquet_store.py` | Raw + normalized parquet store |
| `python/src/eventcontracts/adapters/venues/kalshi/{client,fees,auth}.py` | Kalshi REST/auth/fee |
| `python/src/eventcontracts/cli/{backtest,live_paper,sweep,rank,weather}.py` | CLIs |
| `python/src/eventcontracts/observability/telemetry.py` | Logger/metrics/tracing protocols |
| `python/src/eventcontracts/audit.py` | Audit stamp + trail protocol |
| `configs/strategies/*.toml` | Strategy specs |
| `configs/sleeves/*.toml` | Sleeve risk specs |
| `configs/venues/*.toml` | Venue configs |
| `contracts/parity/<strategy>` | Cross-language parity fixtures |
| `contracts/replay/<venue>` | Replay fixtures |
| `docs/v3-comprehensive-audit-and-spec.md` | V3 audit (still authoritative for findings not superseded here) |
| `docs/hyper-comprehensive-codebase-audit-agent-spec.md` | Hyper audit (still authoritative for findings not superseded here) |
| `docs/v4-audit-and-agent-implementation-spec.md` | **This document — the working spec** |

---

# §8. V4.1 Supplement — Deep-Layer Leakage Audit

This supplement is a second pass that walked the layers v4 only glanced at: domain types, time/numbers, serialization, audit chain, storage/parquet, replay, normalization, capture, every strategy plugin (all 26), allocator, ledger, OMS state, bus, gateway base, observability, artifacts, models, runner, configs, tests, CI/CD, Docker. Findings are numbered **N32-N86** continuing the v4 sequence.

The verification recipe (§4), live-readiness gate (§5), out-of-scope set (§6), and file cheatsheet (§7) of v4 still apply. New phases **P13-P16** are appended at the end of this supplement; phases P1-P12 from v4 are unchanged.

## §8.1 New findings — Foundations (domain, time, IDs, serialization, audit)

### N32. [H] `decision_kind()` and `event_kind()` use match-case with **no fallback** — silent `None` on new variants
**Where:** `python/src/eventcontracts/domain/decisions.py:143-155`, `domain/events.py:205-227`.
**Observed:** Both functions are exhaustive over today's variants but neither has `case _:`. If a new variant is added (or one is renamed and a consumer is on an older import), the function falls through and Python returns `None`. Downstream logging, metrics labels, and routers then either `KeyError` or label-cardinality-leak.
**Fix:** Add `case _: raise AssertionError(f"unhandled variant: {type(decision).__name__}")` to both functions. Or convert to an enum lookup table built at import time, indexed by `type(...)`, that fails noisily on KeyError.
**Test:** `decision_kind_raises_on_unknown_variant` — define a stub `class _NewVariant(StrategyDecision): pass` in the test, call `decision_kind(_NewVariant())`, assert raises.
**Effort:** 1h. **Phase: P11.**

---

### N33. [H] `IntentEnvelope.emitted_at` is non-optional but validated by `require_optional_aware_datetime`
**Where:** `python/src/eventcontracts/domain/decisions.py:130, 139`.
**Observed:** Field declared `emitted_at: datetime` (no `| None`) but `__post_init__` calls `require_optional_aware_datetime(self.emitted_at, ...)`. A `None` slips past validation; only the type annotation prevents it — and pydantic's `dataclass.replace` can set it to None without re-validating.
**Fix:** Use `require_aware_datetime` (strict). Add an explicit assertion that `emitted_at is not None`.
**Test:** `intent_envelope_rejects_none_emitted_at`.
**Effort:** 30min. **Phase: P11.**

---

### N34. [H] Python `Decimal` trailing-zero formatting diverges from Rust `format_decimal` after `.normalize()`
**Where:** `rust/crates/oms/src/lib.rs:404` (calls `.normalize()`), `python/src/eventcontracts/domain/serialization.py:26` (uses `str(Decimal)`).
**Observed:** `Decimal("1.50")` formats as `"1.5"` in Rust after normalize, but stays `"1.50"` in Python. `canonical_sha256` over identical-meaning records produces different bytes → different hashes → audit chain breaks across the Python↔Rust boundary.
**Fix:** Pick one canonical decimal-string format (recommend: explicit, no trailing zeros, no exponent — `format!("{}", d.normalize())` in Rust, `format(d.normalize(), 'f')` with a custom strip in Python). Add a parity test that hashes 50 representative `(Decimal, Decimal)` pairs and asserts byte-identical hashes Python ↔ Rust.
**Test:** `canonical_sha256_parity_decimals.toml` parity case.
**Effort:** 4h. **Phase: P10 (parity scope) or P2 (fee parity already touches this).**

---

### N35. [H] Float features escape range/NaN validation
**Where:** `python/src/eventcontracts/domain/features.py:65-77, 96`.
**Observed:** `FeatureVector.values: tuple[float, ...]` is validated with `isfinite(v)` only. No range check; no `[0,1]` clamp where the schema implies a probability; no NaN-safe serialization in `canonical_sha256` (which uses `allow_nan=False` — that fires *at serialize time*, not at construction time). A malformed feature pipeline can store 2.0 or -5.0 silently for hours; the first `canonical_sha256` call later crashes far from the bug.
**Fix:** Each `FeatureField` should carry an optional `(min, max)` range; `FeatureVector.__post_init__` validates each value against the schema and rejects NaN/Inf/out-of-range at construction with the field name in the error.
**Test:** `feature_vector_rejects_out_of_range_value`.
**Effort:** 3h. **Phase: P11.**

---

### N36. [M] `Decimal("0")` quantity passes `require_positive_decimal` in Position but is nonsensical
**Where:** `python/src/eventcontracts/domain/validation.py:27-29`, `domain/positions.py`.
**Observed:** `Position.quantity = 0` with a non-zero `average_price` is allowed and propagates into PnL aggregations as a 0×avg term. Zero-qty records also bloat `positions()` listings unless callers filter (which they generally do, but not uniformly).
**Fix:** `Position.__post_init__` should normalize `(quantity == 0)` → `(quantity == 0, average_price == 0, realized_pnl preserved)`. Or strictly reject `quantity == 0` and require explicit deletion.
**Test:** `position_zero_qty_normalizes_avg_price`.
**Effort:** 2h. **Phase: P11.**

---

### N37. [M] `dataclasses.replace()` bypasses `__post_init__` validation
**Where:** All frozen domain dataclasses (`Order`, `Fill`, `PlaceOrder`, `Position`, `IntentEnvelope`, …).
**Observed:** Python language behavior — `replace()` does call `__post_init__` for plain dataclasses, **but** when subclasses override `__init__` or fields use `init=False`, behavior gets subtle. Spot check: `IntentEnvelope` has `metadata: Mapping[str, str] = field(default_factory=FrozenMap)` plus `object.__setattr__(self, "metadata", freeze_mapping(...))` in `__post_init__`. A `replace(env, metadata={...})` re-runs `__post_init__`, so this one is OK — but the broader risk is real if anyone marks a field `init=False`.
**Fix:** Add a unit test for each frozen dataclass that confirms `replace()` with an invalid value raises. This is a tripwire, not a code change.
**Test:** `frozen_dataclasses_revalidate_on_replace`.
**Effort:** 2h. **Phase: P11.**

---

### N38. [M] `AuditStamp.produced_at` monotonicity unchecked
**Where:** `python/src/eventcontracts/audit.py:46`.
**Observed:** Each stamp validates RFC3339-aware; nothing enforces that a child stamp's `produced_at >= parent.produced_at`. A misbehaving producer or clock-skew event can break causal ordering of the audit chain.
**Fix:** `AuditTrailValidator` walks the chain and asserts `child.produced_at >= parent.produced_at - skew_tolerance`. Tolerance default: 50ms.
**Test:** `audit_chain_rejects_non_monotonic_stamps`.
**Effort:** 2h. **Phase: P11 (alongside N18 chain backing store).**

---

### N39. [M] `AuditTrailValidator` does not detect orphan grandparents
**Where:** `python/src/eventcontracts/audit.py:174-179`.
**Observed:** Validation only checks immediate parent exists. A chain `A → B → C` with B-stamp missing leaves C linking to a phantom B; A is orphaned. No traversal asserts the full chain reaches a known root.
**Fix:** `walk(stamp)` should follow `parent_id` recursively until either (a) root reached, (b) cycle detected, or (c) missing parent — and report (c) as a chain integrity error.
**Test:** `audit_validator_detects_missing_grandparent`.
**Effort:** 2h. **Phase: P11.**

---

### N40. [M] In-memory audit / OMS / allocator stores are not thread-safe
**Where:** `audit.py:76-80, 115-137`; `gateway/base.py::InMemoryIdempotencyStore`; `allocation/capital.py::PortfolioRiskAllocator.reserve`.
**Observed:** Plain `dict`/`list` mutation under the implicit assumption of single-threaded execution. Today's runner is single-threaded so this is fine, but the moment P7 (async executor) lands, the GIL-vs-asyncio guarantee weakens and any `await` between a check and a write becomes a TOCTOU window.
**Fix:** Either: (a) document "this runner is single-threaded; do not call X concurrently"; or (b) for any module touched by P7 / async refactor, wrap mutations in `asyncio.Lock` / `threading.RLock`.
**Test:** `pytest-asyncio` concurrent-callers test for any wrapped store.
**Effort:** 4h, mostly mechanical. **Phase: P7 (do as part of executor refactor).**

---

### N41. [M] OMS `open_count: u32` overflow risk
**Where:** `rust/crates/oms/src/lib.rs:237-250`.
**Observed:** `open_count: u32` increments on new orders, decrements on terminal. A pathological process lifetime — sustained 1k orders/sec for 50 days — wraps `u32::MAX = ~4.3B`. Unlikely but possible in a long-running market-maker.
**Fix:** `u64`, with explicit `saturating_add` and a metric `eventcontracts_oms_open_count_saturated_total`.
**Test:** `open_count_saturates_not_overflows`.
**Effort:** 1h. **Phase: P11.**

---

### N42. [M] `ClientOrderId` format/uniqueness is unenforced
**Where:** `python/src/eventcontracts/domain/decisions.py:64`, `orders.py:93`. Validation = `require_non_empty(str(self.client_order_id))`.
**Observed:** Any non-empty string passes. Two sleeves can independently generate `"order-1"` and collide in the OMS / idempotency store. Today the format strings happen to embed the strategy prefix, but nothing enforces it.
**Fix:** A `ClientOrderId` constructor that enforces `<prefix>-<uuid7>` where `<prefix>` is the strategy id. Centralize generation in a `strategy/ids.py::next_client_order_id(strategy_id, counter_or_uuid)` helper; lint-forbid `format!` / `f"..."` construction of `ClientOrderId` outside that helper.
**Test:** `client_order_id_rejects_bare_string` + AST lint for direct format.
**Effort:** 4h. **Phase: P5 (alongside pricing helpers; same lint pattern).**

---

### N43. [M] `SettlementEvent.settled_at` not explicitly UTC; daily-loss-day boundary unguarded
**Where:** `python/src/eventcontracts/domain/lifecycle.py:56-64`, `risk/state.py::DailyLossLedger`.
**Observed:** `settled_at` is aware-datetime but no enforcement of UTC. `DailyLossLedger.record_realized_pnl(at)` keys by `at.date()` — if `at` is in venue-local time (e.g. America/New_York), midnight rolls over five hours later than UTC midnight and the loss bucket misaligns with the runner's reset logic.
**Fix:** Convert to UTC at the boundary (normalization) and assert UTC at construction. `DailyLossLedger` keys explicitly by `at.astimezone(UTC).date()`.
**Test:** `daily_loss_ledger_bucketed_in_utc_regardless_of_input_tz`.
**Effort:** 2h. **Phase: P1 (the same area touches daily-loss restore).**

---

### N44. [M] TOML config `Decimal` precision loss
**Where:** `python/src/eventcontracts/config.py:126-127`.
**Observed:** Pydantic loads TOML floats into `Decimal` via `Decimal(str(float))`. `0.1 + 0.2` floats parse to `Decimal("0.30000000000000004")`. For `max_daily_loss = 0.1` in TOML the value is silently sub-cent precision; rounding behavior downstream is venue-dependent.
**Fix:** Read TOML values as strings (toml supports quoted-decimal form: `max_daily_loss = "0.10"`) and convert via `Decimal(str_value)`. Reject TOML float scalars in money fields.
**Test:** `config_rejects_float_money_field`.
**Effort:** 3h. **Phase: P11.**

---

### N45. [M] Probability boundary cases (0 / 1) accepted but not end-to-end tested
**Where:** `python/src/eventcontracts/domain/validation.py:37-39`.
**Observed:** `require_probability_decimal` allows 0 and 1. Kalshi market behavior at extreme price ticks is degenerate (the 1¢ and 99¢ tick lines). No fixture proves the system survives a quote of exactly 0¢ or exactly 99¢ ask end-to-end.
**Fix:** Replay fixture `contracts/replay/kalshi/extreme_prices.parquet` with quotes at 1¢/99¢ and trades that touch them. Assert strategies refuse to emit naked buy at 99¢ ask or naked sell at 1¢ bid by default (a `min_executable_edge_bps` check, which is itself a P2/N1 follow-on).
**Test:** `extreme_price_fixture_does_not_blow_up_runner`.
**Effort:** 4h. **Phase: P10 (with parity v2).**

---

## §8.2 New findings — Storage / replay / normalization / capture

### N46. [C] Parquet has no schema_version column; partitions can mix schemas silently
**Where:** `python/src/eventcontracts/storage/parquet_store.py:78-119, 348`.
**Observed:** `RAW_SCHEMA`, `NORMALIZED_SCHEMA`, `REJECT_SCHEMA` are hardcoded. A schema change (add column, rename) produces files unreadable by old code or readable-with-wrong-meaning by new code. By-partition writes can land mixed-schema rows in the same directory.
**Fix:**
1. Add `_schema_version: int` column to every schema (constant per writer).
2. Write the version into parquet **metadata** (`pyarrow.parquet.write_table(metadata={"schema_version": ...})`) AND as a column for resilience.
3. Reader: pick the per-file version, refuse to merge across versions silently; provide a `parquet-migrate` CLI.
4. Document the schema-migration policy in `docs/runbooks/parquet-schema-versions.md`.
**Test:** `mixed_schema_partition_merge_raises`.
**Effort:** 1 day. **Phase: P12 (data security & data hygiene).**

---

### N47. [H] Receipt-time vs event-time clamping not enforced at ingest
**Where:** `python/src/eventcontracts/storage/parquet_store.py:164`, `replay/*`.
**Observed:** No code asserts `received_at <= now` at write time. A malformed payload with `ts = 9999-01-01` persists and replays. The earlier hyper-comprehensive audit (P6.4) flagged this for *external* signals; this finding is broader — it applies to **every** envelope on ingest.
**Fix:** A `require_not_future_datetime(received_at, now, slack=60s)` validator at `EventEnvelope.__post_init__` and a second check in `parquet_store._envelope_to_row`. Slack tolerates small clock skew (NTP drift) but rejects clearly bogus values.
**Test:** `envelope_rejects_future_received_at`.
**Effort:** 3h. **Phase: P12.**

---

### N48. [H] External provider snapshots sorted by `received_at`, not `exchange_ts` — replay-time lookahead bias
**Where:** `python/src/eventcontracts/storage/sorting.py:59-60`, `replay` engine, `weather/`, `sports/`, any `ExternalSignalEvent`.
**Observed:** Replay orders envelopes by `received_at`. If an external snapshot is published at T0 but received at T0+5s (network), it lands in the replay stream at T0+5s — but the snapshot's `exchange_ts = T0` is what the strategy uses, effectively granting the strategy 5s of lookahead. Mirror finding from hyper P6.4 but with a concrete sort-key fix.
**Fix:** For external events, sort by `received_at` (the only physically-true causal time). Make strategies always reference `received_at` for feature timestamps; `exchange_ts` is metadata only. Lint: any strategy that calls `event.exchange_ts` in feature math fails the promotion guard.
**Test:** `external_signal_replay_uses_receipt_time_strictly`.
**Effort:** 1 day. **Phase: P11 (alongside discretization lint).**

---

### N49. [H] In-process caches with no eviction — long-running runner memory grows unbounded
**Where:**
- `adapters/venues/kalshi/client.py:340` — `_last_seq_by_sid`.
- `replay/order_book.py:89` — `_states` dict per instrument.
- Idempotency store, audit chain in memory, feature-builder per-instrument state, dedup window in capture.
- Rust gateway `idempotency_store.seen` — no TTL by default.

**Observed:** Each cache grows with workload, no eviction, no `max_size`. A multi-day live run on a venue with churn (markets created/settled daily) leaks memory linearly.

**Fix:** Each cache gets one of:
- A bounded LRU (`OrderedDict` + `popitem(last=False)` in Python).
- TTL eviction (drop entries older than `N` seconds).
- A periodic `gc()` task on a timer (every 60s).

Add `eventcontracts_cache_size{name=...}` gauge metrics so an operator can see growth.

**Test:** `idempotency_store_evicts_on_ttl`, `order_book_state_evicts_on_market_settle`.
**Effort:** 1 day. **Phase: P11.**

---

### N50. [M] Python WS lacks idle ping/pong — silent disconnect detection deferred to OS TCP timeout
**Where:** `python/src/eventcontracts/adapters/venues/kalshi/client.py` WS stream loop (no idle handler observed); Rust `kalshi/src/ws.rs:82, 125` has 20s idle ping/pong.
**Observed:** Live runs use Rust live-runner so this is mostly a capture-tool concern, but Python `capture` CLI WS sessions can hang on silent connection drops until the OS times out the TCP socket — minutes.
**Fix:** Wrap the `websockets.connect` with `ping_interval=20`, `ping_timeout=10`. Reconnect on `ConnectionClosed`.
**Test:** `python_ws_reconnects_on_idle_timeout`.
**Effort:** 3h. **Phase: P12.**

---

### N51. [M] REST polling drift — sleep-based instead of deadline-based
**Where:** `python/src/eventcontracts/cli/capture.py:266` (sleep `poll_interval_seconds` between polls).
**Observed:** If a poll takes 4s and the interval is 10s, the next poll is 14s later — drift compounds. Over a long capture, sample timing skews and downstream replay times are mis-aligned with venue cadence.
**Fix:** Compute `next_deadline = previous_deadline + interval`, `sleep_until(next_deadline)`. If a poll overruns, log a warning and skip rather than letting drift accumulate.
**Test:** `capture_loop_polls_on_deadline_not_offset`.
**Effort:** 3h. **Phase: P11.**

---

### N52. [M] Python tags sequence gaps in metadata; Rust normalizer rejects them — replay vs live divergence
**Where:** Python `client.py:419-426`, Rust `normalize.rs:134-154`.
**Observed:** Same raw input can produce different normalized event streams between Python (tagged, kept) and Rust (rejected with `NormalizeError::SequenceGap`). Backtest results based on Python-captured data are not guaranteed to replay identically in Rust.
**Fix:** Make the two policies consistent: either both reject gaps (preferred, with a configurable `allow_gaps_for_backtest = true` for research data), or both tag-and-keep with the same marker. Add a Python normalizer that runs the Rust rule.
**Test:** `python_rust_normalizer_agree_on_sequence_gap`.
**Effort:** 6h. **Phase: P10 (parity).**

---

### N53. [M] Markets catalog static — no refresh, no ticker-rename detection
**Where:** `python/src/eventcontracts/markets/detection.py:72, 98`.
**Observed:** `InMemoryMarketCatalog` is built once. If Kalshi renames `KXTEMP_NYC_HIGH → KXTEMP_NEWYORK_HIGH` mid-run, the subscription pattern may still match but position records are keyed by the old ticker — orphan positions.
**Fix:** A `MarketCatalog` refresh on a timer (every 60s) + an explicit `on_ticker_rename(old, new)` hook that re-maps OMS/ledger keys and emits a metric.
**Test:** `ticker_rename_remaps_oms_and_positions`.
**Effort:** 1 day. **Phase: P11.**

---

## §8.3 New findings — Strategy plugins (per-plugin)

Each finding extends or sharpens v4 P3 (feedback events for state). Most strategies need the same minor refactor — drop emit-time state mutation, listen to feedback events. The list below is the per-strategy punch list so the agent doesn't miss one.

### N54-N63. [C/H] Strategy-plugin state leakage punch list

For each item: file, line, what to remove or move under `on_feedback`. All fixes land in **Phase P3**.

| # | Strategy | Line | State field set on emit | Action |
|---|---|---|---|---|
| **N54 [C]** | `microstructure_obi_scalper.py` | 159 | `_open_buy_orders[instrument]` | Move to `on_feedback(VenueAcked)`; clear on `IntentRejected` / `OwnOrderTerminal`. |
| **N55 [H]** | `microstructure_obi_scalper.py` | 127 | `_pending_cancel_orders` | Move to `on_feedback(VenueAcked)` for the cancel intent. |
| **N56 [C]** | `sports_tennis_xgboost.py` | 46, 160 | `_MarketState.completed` (one-shot lock forever) | Add `cooldown_secs` config; unlock after `OwnOrderTerminal + cooldown`. |
| **N57 [H]** | `sports_tennis_xgboost.py` | 129 | `pending_client_order_id` | Move to `on_feedback`. |
| **N58 [H]** | `sports_hole_by_hole_pin.py` | 167 | `_fired_for_hole` set inside `on_drive` before `PlaceOrder` | Move to `on_feedback(VenueAcked)`; on `IntentRejected`, clear. |
| **N59 [H]** | `politics_legislative_cascade.py` | 107-113 | `protective_yes_coid` never cleared after cancel succeeds | Add `on_feedback(VenueAcked)` for the cancel; clear coid. |
| **N60 [M]** | `politics_legislative_cascade.py` | 69-100 | `_cumulative_score` per-senator dict unbounded | Add TTL eviction (`max_age_secs` default 24h); add `eventcontracts_strategy_state_size` gauge. |
| **N61 [H]** | `politics_primary_momentum.py` | 117 | Snapshot keyed by `(market_id, side)` — wrong snapshot attached if mid moves between snapshot capture and order emit | Capture a fresh snapshot at emit time, not from the stored map. |
| **N62 [H]** | `weather_temperature_arbitrage.py` | 253, 351 | `_active_notional` incremented on emit; only decremented on settlement — leaks on cancel/reject | Drop strategy-local notional bookkeeping; read from `ctx.exposure(sleeve_id)`. |
| **N63 [M]** | `sports_player_cut_lgbm.py` | 325 | `exp(strokes_to_cut / 2.0)` no overflow guard | Clamp argument to `[-50, 50]` before `exp`. |

Per-strategy regression test (template): emit a synthetic event, force `IntentRejected`, assert strategy-local state is exactly the same as before the emit. Run for each row.

### N64. [L] `court_docket_timing.py:77` assumes $1 YES payout

Use `SettlementEvent.payout_per_contract` instead of `Decimal("1")`. Effort: 1h. **Phase: P11.**

---

## §8.4 New findings — Allocator / ledger / OMS / gateway base / artifacts

### N65. [H] `PortfolioRiskAllocator.reserve()` has no release-on-reject hook
**Where:** `python/src/eventcontracts/allocation/capital.py:80, 112-113, 156-157`.
**Observed:** Reservation added on intent emit. Release path is explicit `.release(client_order_id)`. Nothing connects gateway-reject / risk-reject / venue-reject to release. Allocations leak across rejected orders. Over a session of frequent rejects, the allocator falsely reports capital exhaustion.
**Fix:** Add an `on_intent_outcome(client_order_id, outcome)` hook on the allocator; runner calls it from every terminal verdict path. `outcome ∈ {Filled, Canceled, Rejected, Expired}`. For `Rejected | Expired`, fully release. For `Canceled` of partially-filled, release the unfilled remainder.
**Test:** `allocator_releases_reservation_on_risk_reject`, `allocator_releases_unfilled_remainder_on_cancel`.
**Effort:** 1 day. **Phase: P3 (alongside feedback events — these are the same plumbing).**

---

### N66. [H] `SettlementAccounting` and `LedgerStore` are abstract-only — no Python implementation
**Where:** `python/src/eventcontracts/ledger/accounting.py:56-63` and siblings.
**Observed:** Interfaces exist, no concrete `InMemorySettlementAccounting` or `JsonlLedgerStore`. Backtests use `execution/pnl.py` as a stand-in. Live sleeve was supposed to swap in a real implementation; none exists.
**Fix:** Implement `InMemorySettlementAccounting` and `JsonlLedgerStore` for paper-mode. Mirror Rust gateway's `ledger entries` exactly so cross-language settlement accounting is reproducible. Add `docs/runbooks/ledger.md`.
**Test:** `settlement_round_trip_credits_winner_zero_to_loser`. Property test: random sequence of fills + settlement → ledger nets to `Σ realized_pnl − Σ fees`.
**Effort:** 2 days. **Phase: P9 (capital + atomic groups area).**

---

### N67. [M] `risk/compliance.py::EligibilityPolicy.is_eligible` raises `NotImplementedError`
**Where:** `python/src/eventcontracts/risk/compliance.py:17-21`.
**Observed:** It's an unimplemented placeholder. If any callsite invokes it (today or after a refactor), it explodes; if skipped, no compliance auditing happens at all (wash trade, frequency, position limit aren't enforced).
**Fix:** Either (a) delete the placeholder and document compliance as out-of-scope until a real policy ships, or (b) implement a minimum policy (no orders during a specified blackout window; max-orders-per-minute per instrument; wash-trade refused) plus a `NoOpEligibilityPolicy` default with an explicit `noop=true` flag the operator must set.
**Test:** `eligibility_blackout_window_rejects` if implemented.
**Effort:** half a day. **Phase: P11.**

---

### N68. [M] OMS `OrderStateMachine` has no `fill_id` / `settlement_id` dedup key
**Where:** `python/src/eventcontracts/oms/state.py:30-108`.
**Observed:** State updates are keyed by `venue_order_id`. If the same fill arrives twice on the bus or via WS reconnect, OMS would double-apply unless callers dedup externally. Rust gateway carries its own dedup but Python OMS does not.
**Fix:** Each fill / settlement event must carry an `external_id`; the OMS rejects duplicates by `external_id` (keep an LRU of seen IDs sized by venue throughput). Same for cancel/reject acks.
**Test:** `oms_rejects_duplicate_fill_by_external_id`.
**Effort:** 4h. **Phase: P3.**

---

### N69. [M] `ArtifactBundleLoader.load()` doesn't verify the bundle is promoted
**Where:** `python/src/eventcontracts/artifacts/bundle.py:220-268`.
**Observed:** Hash validation is correct (good — see existing tests). But the loader will load **any** valid bundle, promoted or not. A researcher building a bundle for testing can accidentally load it into a paper-live runner that thinks "this is the promoted artifact."
**Fix:** Add a `promoted: bool` field to the manifest (signed alongside the rest). Loader's `load()` defaults to `require_promoted=True`; research/test paths can opt out with `require_promoted=False`.
**Test:** `bundle_loader_rejects_unpromoted_when_required`.
**Effort:** 3h. **Phase: P10.**

---

### N70. [M] Bundle model reference is `(name, version)`, not content-hash — silent drift after retrain
**Where:** `python/src/eventcontracts/artifacts/bundle.py:469-491`.
**Observed:** Bundle records `model.sha256` for the file ON DISK at bundle creation. Loader looks up the *current* registry entry for `(name, version)`, which may have been re-trained and re-registered with the same `(name, version)` but a different content. Drift goes undetected.
**Fix:** Loader compares the registry entry's current `sha256` against the bundle's recorded `sha256` and refuses to load on mismatch.
**Test:** `bundle_load_fails_when_registered_model_sha_changed`.
**Effort:** 4h. **Phase: P10.**

---

### N71. [L] `DryRunVenueGateway` reject reason is a comma-joined string — weak audit
**Where:** `python/src/eventcontracts/gateway/base.py:399-407`.
**Observed:** `OrderReject.reason` is `",".join(reasons)`. Downstream histograms `intents_rejected_total{reason=...}` get high-cardinality "a,b,c" labels.
**Fix:** Emit one metric increment per reason (`for r in reasons: metric.inc(reason=r)`), and persist the full tuple in audit JSON.
**Test:** `dry_run_reject_emits_one_metric_per_reason`.
**Effort:** 1h. **Phase: P11.**

---

## §8.5 New findings — Configs / tests / CI / Docker

### N72. [C] 16 promoted strategies have **zero** parity cases; only `weather_threshold` placeholder exists
**Where:** `contracts/parity/` contains only `weather_threshold/` (and a `README.md` that says it's a placeholder pending Phase 1-4 completion).
**Observed:** Promotion gate is effectively un-enforced. A strategy can be live-promoted in Rust today with no cross-language behavioral check at all. v3 G2 flagged this, hyper P5 sketched parity v2, neither has landed.
**Fix:** Phase P10 already creates the manifest + parity v2; tighten the CI gate to **fail merge if** `configs/promotion/<strategy>.toml` references parity directory missing or with zero cases. Acceptance: every promoted strategy needs ≥1 parity case before merge of any change to its Rust impl.
**Test:** `promotion_manifest_requires_nonempty_parity` CI gate.
**Effort:** already P10. Budget +0.5 day for the missing-case authoring of the most-promoted strategies (weather, OBI scalper, tennis xgboost).

---

### N73. [H] 10 strategy implementations have no TOML config
**Where:** Code without TOML: `court_docket_timing`, `crop_drought_yield_reversion`, `earnings_guidance_language`, `energy_storage_weather_spread`, `flu_hospitalization_surge`, `liquidity_tail_risk_insurance`, `shipping_chokepoint_delay`, `spaceweather_grid_disruption`, `tariff_headline_gap_fader`, `wildfire_smoke_event_cancel`.
**Observed:** Strategies importable and registered but un-runnable from a CLI without a spec. They're essentially dead code from an operations standpoint.
**Fix:** Either (a) author placeholder TOMLs (`configs/strategies/<name>.toml`) with documented defaults and a `[meta] status = "research"` flag the CLI checks before allowing live execution, or (b) move these 10 modules to `python/src/eventcontracts/plugins/strategies/research/` so the line between live-eligible and research-only is structural.
**Test:** `every_live_eligible_strategy_has_toml_config`.
**Effort:** 4h. **Phase: P10.**

---

### N74. [H] TOML drift in existing configs — strategy reads keys that TOML doesn't set (silent defaults)
**Where:** Weather arb (`weather_temperature_arbitrage.py:50-76` reads ~17 params; the TOML lists ~14); macro CPI (`order_ttl_ms` missing); OBI scalper (model-mode params missing).
**Observed:** Code falls back to defaults silently. Operators looking at a TOML do not see the full operative configuration.
**Fix:** Add a CI test `tests/test_strategy_spec_completeness.py` that, for each registered strategy:
1. Loads `configs/strategies/<name>.toml`.
2. Constructs the strategy.
3. Asserts every spec key the strategy reads is *explicitly present* in the TOML (no silent defaults), OR is documented in a `[meta] optional_keys = [...]` block of the TOML.

Same lint as N15 (spec param typo) but for the reverse direction — code reads a key, TOML doesn't set it.
**Test:** see above.
**Effort:** half a day to write the gate + populate the missing keys. **Phase: P10.**

---

### N75. [M] Several sleeves have implausible `max_daily_loss : max_order_notional` ratios
**Where:** `configs/sleeves/`:
- `example-kalshi-paper.toml`: `max_order = 100`, `max_daily_loss = 50` — **inverted** (one full order could lose more than the daily cap).
- `sports-hole-by-hole-polymarket-paper-a.toml`: `max_order = 100`, `max_daily_loss = 150` — only 1.5× headroom.
- `macro-cpi-kalshi-paper-a.toml`: `max_order = 600`, `max_daily_loss = 600` — one full max-loss order blows the day.

**Observed:** These sleeves do not respect the conventional invariant `max_daily_loss >= 5 * max_order_notional` (a single order should not be able to exhaust more than 20% of the daily budget).
**Fix:** Add a CI gate in `tests/test_sleeve_consistency.py` that asserts every sleeve obeys `max_daily_loss >= 5 * max_order_notional`. Where a sleeve legitimately wants tight discipline, allow an explicit `[risk] daily_loss_acknowledged_tight = true` override.
**Test:** `every_sleeve_has_sane_daily_loss_buffer`.
**Effort:** 2h to add the gate + fix or annotate each offending sleeve. **Phase: P10.**

---

### N76. [M] Tests skewed toward smoke / import-only — many functions covered, few values asserted
**Where:** `python/tests/test_framework_imports.py` (28 lines, no value assertions); `python/tests/test_config_loaders.py` smoke-loads TOMLs without validating parsed values; `python/tests/test_strategy_specs.py` asserts "at least one non-NoAction decision" without checking which decision.
**Observed:** Pytest count looks healthy (240+ tests) but a fraction is import smoke. Mutation testing would reveal weakness.
**Fix:**
1. For every smoke test, add an "assert it produces the *right* answer for a known input" companion.
2. Add `mutmut` or `cosmic-ray` mutation testing to a nightly CI job; track the survival rate as a quality metric in `docs/quality-dashboard.md`.
**Effort:** 1-2 days iterative. **Phase: P11.**

---

### N77. [M] GitHub Actions and Docker base images pinned by tag, not SHA digest
**Where:** `.github/workflows/quality.yml` uses `actions/checkout@v4`, `actions/setup-python@v5`, `gitleaks/gitleaks-action@v2`; `Dockerfile` uses `rust:1.83-bookworm`, `debian:bookworm-slim`.
**Observed:** Tag-based references are mutable; a malicious or buggy update to the tag changes CI behavior without a code change.
**Fix:** Pin to commit SHA (`actions/checkout@<40-hex>`) and image digests (`rust@sha256:<digest>`). Add a `dependabot.yml` to bump digests automatically with PR review.
**Test:** Manual review of CI logs after pinning; verify image digests survive a `docker pull` round-trip.
**Effort:** 3h. **Phase: P12.**

---

### N78. [M] `cargo audit` lacks an advisory cutoff date / known-suppress list
**Where:** `.github/workflows/quality.yml::cargo audit` step.
**Observed:** Any new advisory in `rustsec/advisory-db` instantly fails CI, even if it doesn't apply (e.g. a path you don't use). This makes CI brittle and trains operators to ignore audit failures.
**Fix:** Maintain `audit.toml` with explicit `[advisories] ignore = ["RUSTSEC-XXXX-YYYY"]` for known-not-applicable advisories, with a comment documenting why each is ignored and a revisit date.
**Effort:** 1h ongoing. **Phase: P12.**

---

### N79. [L] `demokey.txt` / `ecmodel.txt` are on disk and untracked — fine, but undocumented
**Where:** Repo root. Verified `git ls-files` empty for both; `.gitignore` covers them; `.dockerignore` covers them.
**Observed:** No bug. Operator artifact. But there's no `docs/runbooks/credential-rotation.md` explaining how to rotate, no `README` note saying these are demo-only.
**Fix:** Write `docs/runbooks/credential-rotation.md` covering: (a) where keys live, (b) how to rotate Kalshi keys via their dashboard, (c) how to inject `KALSHI_PRIVATE_KEY_PEM` via secret manager in prod, (d) how to verify a fresh key with `eventcontracts capture --venue kalshi --dry-run`.
**Effort:** 2h. **Phase: P12.**

---

## §8.6 Aggregate severity counts (this supplement)

| Severity | Count | Items |
|---|---|---|
| **C** | 4 | N46, N54, N56, N72 |
| **H** | 13 | N32, N33, N34, N35, N47, N48, N49, N55, N57, N58, N59, N61, N62, N65, N66, N73, N74 |
| **M** | 21 | N36-N45, N50-N53, N60, N63, N67-N71, N75-N78 |
| **L** | 4 | N64, N71-twin, N79, plus N40 (situational) |

*Counts approximate; some findings could plausibly slide one rank depending on operator risk tolerance.*

---

## §8.7 New / amended phases

The original v4 phase plan (P1-P12) absorbs most of these. Two micro-phases are added at the end for surface-area items that don't fit cleanly elsewhere.

### Phase **P13** — Domain hardening & invariants (Day 24-25) — **H/M**
Items: **N32-N45**.
Order:
1. Match-case exhaustiveness asserts for `decision_kind`, `event_kind` (N32).
2. `IntentEnvelope.emitted_at` strict validator (N33).
3. Decimal canonicalization parity Python↔Rust (N34), reuse parity case from N1/P2.
4. Feature schema range/NaN validation (N35).
5. Position-zero normalization (N36).
6. `dataclasses.replace` tripwire tests (N37).
7. Audit chain monotonicity and orphan detection (N38, N39).
8. UTC enforcement on `SettlementEvent` and `DailyLossLedger` (N43).
9. TOML decimal precision lock (N44).
10. Probability boundary fixture (N45).
11. OMS `open_count` to `u64` (N41).
12. `ClientOrderId` constructor helper + lint (N42 — also reuses the P5 lint pattern).

Verification: full §0 baseline; new parity case `decimals.parquet` passes both langs.

### Phase **P14** — Strategy plugin punch list (Day 26-27) — **C/H**
Items: **N54-N64**.
This phase is the systematic strategy refactor. Use the table in §8.3 as a tracker. Per row: drop the emit-time mutation, listen to feedback (relies on P3), add a regression test that injects a risk-reject and asserts state is identical to pre-emit.
Verification: every row has a passing test; pytest count grows by exactly the row count.

### Phase **P15** — Configs / sleeves / promotion gate (Day 28) — **H/M**
Items: **N72-N76**.
Order:
1. Author placeholder TOMLs for the 10 missing strategies, or move them under `research/` (N73).
2. Test `test_strategy_spec_completeness.py` (N74).
3. Test `test_sleeve_consistency.py` with `max_daily_loss >= 5×max_order` rule (N75).
4. Promotion manifest CI gate fails on empty parity directory (N72).
5. Mutation-testing nightly job (N76).

### Phase **P16** — Storage + capture + supply-chain (Day 29-30) — **C/H/M**
Items: **N46-N53, N77, N78, N79**.
Order:
1. Parquet schema_version column + metadata + migrate CLI (N46).
2. Receipt-time clamping validator (N47).
3. External-signal sort-by-receipt-time + lint (N48).
4. Cache eviction across all in-memory stores + gauges (N49).
5. Python WS idle ping/pong (N50).
6. Deadline-based REST capture (N51).
7. Python/Rust sequence-gap policy alignment (N52).
8. Markets catalog refresh + ticker-rename remap (N53).
9. CI: pin actions to SHA, Docker base to digest (N77).
10. `audit.toml` cargo-audit suppress list (N78).
11. `docs/runbooks/credential-rotation.md` (N79).

---

## §8.8 Additional per-fix verification recipes

These extend §4 in the original v4 spec.

### N32 — exhaustive match
```bash
cd python && python -m pytest tests/test_domain_contracts.py::test_decision_kind_raises_on_unknown -xvs
```

### N34 — Python↔Rust decimal parity
```bash
cd rust && cargo run -p eventcontracts-parity --bin parity_check -- --cases contracts/parity/decimals
cd python && python -m pytest tests/test_canonical_sha256.py::test_decimal_parity -xvs
```

### N46 — Parquet schema version
```bash
cd python && python -m pytest tests/test_parquet_store.py::test_mixed_schema_raises -xvs
eventcontracts parquet-migrate data/raw/2026-05-28 --from-version 1 --to-version 2
```

### N49 — Cache eviction
```bash
curl http://localhost:9090/metrics | grep eventcontracts_cache_size
# Counts should plateau, not grow forever, over a long session.
```

### N54-N62 — Strategy state regression suite
```bash
cd python && python -m pytest tests/test_strategy_feedback_regression.py -xvs
```
For each strategy named in §8.3, the test:
1. Snapshots strategy internal state.
2. Drives a synthetic `QuoteEvent` that should produce an intent.
3. Forces `IntentRejected` from a stub risk gate.
4. Asserts strategy internal state == pre-emit snapshot.

### N65 — Allocator release on reject
```bash
cd python && python -m pytest tests/test_allocator.py::test_releases_on_reject -xvs
```

### N66 — Settlement accounting parity
```bash
cd python && python -m pytest tests/test_ledger.py::test_settlement_round_trip -xvs
cd rust && cargo test -p eventcontracts-gateway settlement_round_trip -- --nocapture
```

### N72/N73/N74/N75 — Config completeness + parity
```bash
cd python && python -m pytest tests/test_strategy_spec_completeness.py tests/test_sleeve_consistency.py tests/test_promotion_manifest.py -xvs
```

### N77/N78 — Pinned SHAs
```bash
git grep -E 'actions/[a-z-]+@v[0-9]' .github/workflows  # Should return nothing post-fix.
docker pull rust@sha256:<digest>                        # Should match Dockerfile pin.
```

---

## §8.9 What this supplement does NOT cover

Honest catalog of remaining unknowns:

1. **Polymarket adapter** — not deeply walked. If it ships, it needs its own audit (signing, nonce, on-chain key custody).
2. **NATS / JetStream bus** — only abstract interfaces exist in repo. When a real broker lands, audit retention, ordering, backpressure, DLQ.
3. **Latency budgets in microseconds** — no actual numbers yet. Criterion benches (N24) produce the baseline; "fast" remains a target until measured.
4. **Multi-process / multi-host coordination** — single-process today. Out of scope.
5. **Live audit of Rust `model-runtime` ONNX path under load** — N11 fix addresses the obvious mutex; whether the ort/Tokio interplay has subtler issues needs a real load test.
6. **Compliance** — N67 fix is binary "implement or document out-of-scope"; a real compliance regime (wash-trade rule, FINRA-style reporting) is its own multi-week stream.
7. **Disaster recovery / chaos** — no test today proves the runner survives `kill -9` mid-batch. Snapshot work in N19 + audit chain backing store in N18 are prerequisites for a real chaos test.

---

End of V4.1 supplement. Total combined findings across v3 + hyper + v4 + v4.1 = ~110. Phases P1-P16 sequence the work into ~30 days of focused implementation.
# Superseded

This audit is superseded by `docs/v5-audit-and-agent-implementation-spec.md`.
