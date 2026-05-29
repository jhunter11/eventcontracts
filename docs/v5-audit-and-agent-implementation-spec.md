# V5 Hyper-Comprehensive Audit + Agent Implementation Spec

**Date:** 2026-05-29
**Builds on:** `docs/v4-audit-and-agent-implementation-spec.md`, `docs/hyper-comprehensive-codebase-audit-agent-spec.md`, `docs/v3-comprehensive-audit-and-spec.md`, `docs/logic-audit-report.md`.
**Audience:** A single implementation agent. Read §0, then work top-to-bottom.

This is a *fresh* pass over the current working tree (not a re-statement of V4). It does three things:

1. **Verifies** which V4 fixes actually landed (most did — see §1).
2. **Reports new, code-verified findings** with exact `file:line` citations, viewed from every angle the brief named: assumptions, data security, redundancy, speed, strategy integration, and — most importantly — the trader's perspective.
3. **Sequences** the work into phases that only depend on prior phases, each with an exact change and a failing-then-passing test recipe.

Every finding below was confirmed by reading the cited code in this tree on 2026-05-29. Where a fix is "wired but dead," that is stated explicitly because it is the most dangerous failure mode: a check that exists, passes review, and never fires.

---

## §0. Ground Rules For The Agent

1. **Baseline before every phase.** Record counts; a drop is a regression to fix in-phase:
   ```bash
   cd C:/QWS/eventcontracts
   cargo fmt --manifest-path rust/Cargo.toml --all -- --check
   cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
   cargo test --manifest-path rust/Cargo.toml --workspace
   cd python && python -m ruff check src tests && python -m mypy src/eventcontracts tests && python -m pytest tests -q && cd ..
   ```
2. **One PR per phase (P1…P9).** Title references the phase id.
3. **A fix without a failing-then-passing test did not happen.**
4. **Dual-language invariants need dual-language coverage.** Parity cases in `contracts/parity/` are the cross-language contract.
5. **Never bypass the gateway / never mutate framework state from a strategy.** Strategies return decisions only.
6. **Single source of truth:** `DryRunGateway.sleeve_state` (Rust) and `SleeveRiskGate + PnLTracker` (Python). No mirror state.

---

## §1. State Of The Tree Today (V4 verification)

Confirmed **landed** by inspection:

- Rust YES/NO outcome-aware exposure; side-specific last-look; `SubmitUnknown` on transport failure (gateway).
- **Async execution worker** — `process_batch_async` / `submit_async` exist; live-runner drives the async path so a slow REST submit no longer blocks the WS reader (`gateway/src/lib.rs:1485`, `live-runner/src/main.rs:594`).
- **Reconcile-on-start restores `daily_realized_loss`** from fills since UTC midnight, and **adopts resting orders + subscribes** to their markets (`live-runner/src/main.rs:484`, `:513`, `:522`).
- **Market-suspension / lifecycle gate** with optional strict `require_market_state` (`gateway/src/lib.rs:1343`).
- **Toxicity circuit breaker**, **portfolio gross/group caps**, **rate budget**, **idempotency reservation**, **priority scheduler with cancel-jump-the-line + non-cancel shedding** (`gateway/src/lib.rs:181`, `:925`, `:1060`, `:1306`).
- ONNX session sets `with_intra_op_spinning(false)` (`model-runtime/src/lib.rs:106`).
- Secret hygiene: `.env`, `.env.*`, `*.pem`, `*.key`, `*.p8`, `demokey.txt`, `data/`, `*.parquet` are all gitignored and **no secrets are tracked** (`git ls-files` clean).
- Kalshi NO-side trade price inversion fixed (`normalization/kalshi.py:378` `_trade_price`).

Still **partial / open** — addressed by the findings below.

---

## §2. New Findings (code-verified, this pass)

Severity: **C**ritical / **H**igh / **M**edium / **L**ow. Tag in `[brackets]` is the audit angle.

### T1 [H] [trading-logic] Kalshi `close_date_updated` is mis-mapped to `CLOSED`, falsely halting live markets

`normalization/kalshi.py:416` maps the lifecycle event type `close_date_updated` to `MarketLifecycleKind.CLOSED`:
```python
"close_date_updated": MarketLifecycleKind.CLOSED,
```
But `close_date_updated` only means the market's scheduled close *timestamp changed* — the market is still open. Downstream this is catastrophic:
- The paper simulator treats `CLOSED` as terminal and **cancels every resting order** for that instrument (`execution/market_simulator.py:282-291`, `_cancel_all_for`).
- The Rust gateway market-state gate treats a non-tradable state as an **unconditional reject** of new placements (`gateway/src/lib.rs:1343`).

So a routine close-time reschedule (common on Kalshi for weather/sports) yanks all our quotes and blocks re-entry until a fresh `activated`/snapshot arrives. **Fix:** map `close_date_updated` to a *metadata* lifecycle (no state change) — e.g. `MarketLifecycleKind.LISTED` is also wrong (it can downgrade an open market); add a dedicated non-state-changing kind or route it to `metadata_updated` semantics that leave `MarketState` untouched. Only `deactivated`/explicit close events should pause/close.

### T2 [H] [trading-logic / assumptions] The fee-adjusted edge gate is wired but **dead in production**

`risk/src/lib.rs:301-313` implements exactly the right pre-trade check: reject if `(fair_price − price) − taker_fee_per_contract < min_executable_edge_ticks`. It is **gated on `intent.fair_price.is_some()`**.

But the only producer of `IntentSnapshot` in the live path, `gateway::prepare_one`, hardcodes:
```rust
fair_price: None,
min_executable_edge_ticks: None,
fee_rate_bps: None,   // gateway/src/lib.rs:1325-1327
```
and `DecisionPayload::PlaceOrder` (`gateway/src/lib.rs:91-100`) has **no `fair_price` field** — so a strategy that knows its fair value has no channel to carry it to risk. Result: the fee/edge gate never fires outside unit tests (`risk/src/lib.rs:851`, `:871`), and **negative-edge-after-fee orders pass risk unimpeded.** This is the single most important trading-safety gap in the tree, because it looks covered.

**Fix:** add optional `fair_price`, `min_executable_edge_ticks`, `fee_rate_bps` to `DecisionPayload::PlaceOrder` (serde `default`, backward-compatible); have the Rust strategy runtimes emit `fair_price` when they have one; thread them into the `IntentSnapshot` in `prepare_one`. Mirror in Python: `PlaceOrder` decision already may carry metadata — ensure `SleeveRiskGate` runs the same fee-edge check when `fair_price` metadata is present.

### T3 [H] [trading-logic / risk] Daily-loss kill switch is **realized-only** — held-to-expiry drawdown never trips it

`DailyLossLedger` (`risk/state.py:18-41`) accumulates **only realized PnL**: it is fed by closing fills, buy fees, and settlement (`execution/pnl.py:100-111`, `:166`). `PnLTracker.on_event` updates `mark_price` on quotes but **never feeds unrealized PnL to the ledger** (`execution/pnl.py:115-129`). Most strategies here are held-to-expiry event contracts, so positions don't realize until settlement — a sleeve can bleed unbounded unrealized losses across many open markets and the intraday daily-loss cap will read `loss_for(...) == 0` the entire time.

**Fix:** add a mark-to-market drawdown feed. Compute portfolio unrealized PnL on each quote/timer tick (using liquidation marks — see T4) and have the risk gate trip the kill switch on `realized_loss + max(0, −unrealized) ≥ daily_loss_limit`. Keep realized and unrealized buckets separate for audit. Mirror the same aggregate in the Rust `SleeveState` drawdown check.

### T4 [M] [trading-logic / assumptions] Unrealized PnL marks to **mid**, overstating liquidation value

`PnLTracker` marks open inventory at `(bid+ask)/2` (`execution/pnl.py:124`). On a binary book you exit a long by hitting the **bid** and crossing the spread; mid-marking systematically overstates equity by half-spread × size, which on wide event-contract books (5–10¢) is material. This understates risk and feeds an optimistic number into T3.

**Fix:** introduce a `mark_mode` (`mid` for reporting, `liquidation`/`bid` for risk). Use the liquidation mark for the drawdown/kill-switch feed; keep mid for P&L display if desired.

### T5 [M] [trading-logic / fees] Maker fills modeled as **always-zero fee** — overstates maker-strategy edge

`KalshiFeeModel.estimate` returns `0` for `liquidity == "maker"` (`adapters/venues/kalshi/fees.py:37-43`). Kalshi's published schedule charges fees per contract and several market families charge makers; assuming a free maker side inflates the backtested edge of exactly the strategies that depend on it — OBI scalper, queue-evader, the microstructure sleeves. A strategy can look profitable in paper purely on a fee assumption that won't hold live.

**Fix:** make the maker rate a parameter (default to the current published maker rate per market family, not 0), and surface it in the sleeve fee config. Re-run the microstructure sweeps under the corrected schedule before any promotion.

### T6 [M] [trading-logic / sim fidelity] Cancel/replace have **zero modeled latency** while submits are delayed

`MarketPaperSimulator.submit` draws `latency.draw("submit")` and rests the order with an `effective_at` in the future (`execution/market_simulator.py:444-458`), but `cancel` (`:601`) and `replace` (`:613`) mutate state **instantly**. A strategy can therefore cancel a stale quote with zero latency in backtest and dodge adverse-selection fills it would absorb live (the WS sees the toxic print, you "cancel" before it lands). This biases every passive/quoting strategy's backtest upward.

**Fix:** draw `latency.draw("cancel")` / `latency.draw("replace")` and apply cancels/replaces on an `effective_at` timeline exactly like submits, so trades printing during the cancel-in-flight window still fill the resting order.

### T7 [M] [trading-logic / sim fidelity] No market impact — the sim's own orders don't deplete the book

`_fill_marketable` walks `state.book` levels but never decrements them (`execution/market_simulator.py:509-544`); `state.book` is only replaced by the next `OrderBookEvent`. Two marketable orders in the same tick both receive full top-of-book liquidity, and a single large taker pays no impact beyond the visible snapshot it happened to see. For any strategy that sizes up, this overstates achievable size and fill price.

**Fix:** apply a transient consumption model — within a tick, debit consumed quantity from the working book copy so subsequent fills walk deeper levels; optionally add a configurable impact/refill decay. At minimum, document the no-impact assumption and cap per-tick taker size in risk.

### T8 [L] [trading-logic / sim fidelity] Passive queue only advances on trade prints

`_apply_trade` burns `queue_ahead` only when a trade prints at the level (`execution/market_simulator.py:326-331`); cancellations ahead of you in the queue never advance your position. This *under*-estimates passive fills (conservative, so lower priority), but it skews queue-evader research. Note in the simulator docstring; optionally model a cancel-decay on `queue_ahead`.

### S1 [H] [strategy-integration / dev-ergonomics] **27 Python strategies, 3 promotable to Rust** — the promotion model does not scale

Python auto-discovers every plugin via `@register` (`strategy/registry.py:72-95`) — **27 strategy modules** exist under `plugins/strategies/`. The Rust `default_registry()` hand-registers exactly **three**: `weather_threshold`, `example_threshold`, `sports_tennis_xgboost` (`runner/src/registry.rs:80-98`). Parity cases (`contracts/parity/`) and promotion manifests (`configs/promotion/`) likewise cover only those three.

The architecture's promise — "research in Python → promote to Rust live" — currently means **hand-porting each strategy into Rust and manually keeping two implementations in parity.** That is both the biggest barrier to "easiest to develop new strategies" and a silent-divergence correctness risk (24 strategies have no cross-language guarantee at all).

**Fix (design, choose one and spec it before coding):**
- **(a) Spec-driven archetypes (recommended).** Most plugins reduce to a few archetypes: *threshold*, *model-edge-vs-fair*, *passive-quoter/scalper*, *cross-venue-arb*. Implement these as parameterized Rust runtimes driven entirely by the TOML `parameters`, so a new strategy of a known archetype is **config-only, zero new Rust**. `ThresholdStrategy` already proves the pattern (one Rust impl serves `weather_threshold` + `example_threshold`).
- **(b) Embedded interpreter for non-latency-tier sleeves.** For `RELAXED`/`STANDARD` tier strategies, run the *Python* implementation in-process from Rust (PyO3) behind the same `StrategyRuntime` trait, reserving hand-written Rust for `FAST`/`CRITICAL` alpha. Promotion then means "tag the tier," not "rewrite."

Pick based on latency tiers actually in use. Document the decision in `docs/strategy-promotion.md`.

### S2 [M] [dev-ergonomics] No strategy scaffolding — adding one is all-manual convention

There is no generator. Adding a strategy means hand-creating: the plugin module + `@register`, a `configs/strategies/*.toml`, a `configs/sleeves/*.toml`, a parity case directory, and (for promotion) a Rust impl + registry line + manifest. Nothing enforces that these stay in sync.

**Fix:** add `ec new-strategy <name> --archetype <kind>` (a CLI under `cli/`) that stamps out the spec, sleeve, an empty parity directory, and — if the archetype is spec-driven (S1a) — nothing else. Add a `make verify-strategy <name>` that asserts: spec validates against `strategy_spec.schema.json`, the name is registered in both languages (or is a recognized archetype), and a parity directory exists.

### S3 [M] [strategy-integration] Parity coverage is 3/27 — promoted strategies have no cross-language guarantee

Even within the 3 promotable strategies, the parity bar should be a gate. Make parity a **promotion precondition**: a strategy cannot be referenced by a live (non-paper) sleeve unless a parity case set exists and `parity_check` passes within tolerance. Wire this into `validate_bundle` / promotion-manifest validation.

### Sp1 [M] [speed] Venue client still blocks the runtime on the sync trait path

`kalshi/src/venue_client.rs:64,97,117,137` use `block_in_place(|| handle.block_on(...))` for submit/cancel/cancel-all. The async gateway path (`submit_async`) exists and the live-runner uses it, but the **sync `VenueClient` trait impl remains** and any caller of the sync path (tests, future callers) reintroduces head-of-line blocking on the tokio worker. **Fix:** make the async trait the only production surface; mark the sync impl `#[cfg(test)]` or delete it; assert in CI that the live-runner never calls the sync path.

### Sp2 [M] [speed] ONNX inference is serialized through one `Mutex<Session>`

`model-runtime/src/lib.rs:78` holds a single `Mutex<Session>`; concurrent scoring serializes. `with_intra_op_spinning(false)` is set but `intra_op_num_threads` is not, and there is no session pool. For multi-instrument ONNX scoring under load this is a throughput ceiling. **Fix:** add a small session pool (N sessions round-robin) or set explicit `intra_op_num_threads`, and bench under the live-runner load harness (`benches/ws_lag_under_load.rs`).

### Sp3 [L] [speed] Per-decision/per-quote allocations on the hot path

Carried from V4 (N28–N30): per-quote `String` clone for the `last_quote_epoch_secs` key, repeated `epoch_seconds(now)` per intent, `format!("c-onnx-yes-{:08}", n)` per decision. Low impact individually; fix opportunistically with interned keys / a single `now` decode per batch.

### D1 [M] [data-security] Raw private events stored unencrypted, no TTL; demo key in plaintext on disk

Carried from V4 N20/N21. `demokey.txt` and `.env` are gitignored (good) but live as plaintext on disk; raw own-fill / own-order envelopes are persisted unencrypted with no retention TTL. **Fix:** (1) document and script credential handling in `docs/runbooks/credential-rotation.md` (exists — verify it covers the demo key); (2) add a retention/redaction pass for `data/` private channels; (3) add `KALSHI_PRIVATE_KEY_PEM` inline env option so deployments need not write a key file at all.

### D2 [L] [data-security / supply-chain] Confirm `pip-audit` + `cargo audit` actually run in CI and deps are pinned

V4 claimed CI enforcement. Verify `.github/workflows/quality.yml` runs both, that `python/requirements*.txt` are pinned, and add branch protection / SBOM if missing.

### R1 [L] [redundancy] Crate and doc sprawl

`bus` and `allocator` crates are compiled but unused in the production path (carried). More pressing: there are now **six** overlapping audit/spec docs (`v3`, `v4`, `v5`, `hyper-comprehensive`, `logic-audit-report`, plus `prompts/`). After this spec lands, **consolidate**: mark v3/v4/hyper as superseded (a one-line header pointing here) so a future agent reads one current document, not six.

---

## §3. Phased Implementation Plan

Each phase is independently shippable and ordered so later phases build on earlier ones. Trading-safety correctness first, then fidelity, then ergonomics/scale, then speed/housekeeping.

| Phase | Theme | Findings | Severity |
| --- | --- | --- | --- |
| **P1** | Lifecycle correctness | T1 | H |
| **P2** | Fee-edge gate made live | T2 | H |
| **P3** | Unrealized drawdown kill switch | T3, T4 | H |
| **P4** | Sim fidelity: fees + cancel/replace latency + impact | T5, T6, T7, T8 | M |
| **P5** | Strategy-integration scale: archetypes | S1 | H (arch) |
| **P6** | Dev-ergonomics: scaffolding + promotion gate | S2, S3 | M |
| **P7** | Speed: venue-client async-only, ONNX pool | Sp1, Sp2, Sp3 | M/L |
| **P8** | Data security / supply chain | D1, D2 | M/L |
| **P9** | Redundancy cleanup + doc consolidation | R1 | L |

### P1 — Lifecycle correctness (T1)
- In `normalization/kalshi.py:411-421`, stop mapping `close_date_updated` (and any pure metadata event) to a state-changing kind. Add a non-state-changing path so the simulator and gateway leave `MarketState`/`status` untouched. Keep `deactivated`→PAUSED, real close/determine/settle as-is.
- Audit the simulator + gateway to ensure only genuinely terminal/paused states cancel or block.

### P2 — Fee-edge gate made live (T2)
- Extend `DecisionPayload::PlaceOrder` with serde-default `fair_price`, `min_executable_edge_ticks`, `fee_rate_bps`.
- Populate them from the strategy runtimes that compute a fair value; thread into `IntentSnapshot` in `gateway::prepare_one` (`gateway/src/lib.rs:1318`).
- Default `fee_rate_bps` to the sleeve's configured Kalshi rate (700 bps) when unset; default `min_executable_edge_ticks` from sleeve risk config.
- Python: have `SleeveRiskGate` run the identical check when `fair_price` is present in the `PlaceOrder` metadata.

### P3 — Unrealized drawdown kill switch (T3, T4)
- Add `mark_mode` to `PnLTracker` (`mid` | `liquidation`). Liquidation mark = best executable exit (bid for longs).
- On each quote/timer, compute portfolio unrealized using the liquidation mark and feed an `unrealized_drawdown` signal to the risk layer.
- Trip the kill switch on `realized_loss + max(0, −unrealized) ≥ daily_loss_limit`. Mirror in Rust `SleeveState`.

### P4 — Simulator fidelity (T5–T8)
- T5: parameterize maker rate in `KalshiFeeModel`; default ≠ 0; surface in sleeve fee config; re-run microstructure sweeps.
- T6: model cancel/replace latency on an `effective_at` timeline; trades during the in-flight window still fill the resting order.
- T7: debit consumed quantity from a working book copy within a tick; optional impact/refill decay; cap per-tick taker size in risk.
- T8: document queue-advance-on-trade-only; optional cancel-decay on `queue_ahead`.

### P5 — Strategy archetypes (S1)
- Decide (a) spec-driven archetypes vs (b) PyO3-embedded for non-FAST tiers; document in `docs/strategy-promotion.md`.
- Implement the chosen path so that adding a strategy of a known archetype requires **no new Rust**. Prove it by promoting at least two currently Python-only strategies through the new path with parity cases.

### P6 — Scaffolding + promotion gate (S2, S3)
- Add `ec new-strategy` generator and `make verify-strategy`.
- Make a passing `parity_check` a hard precondition for any non-paper sleeve referencing a strategy (enforced in bundle/promotion-manifest validation).

### P7 — Speed (Sp1–Sp3)
- Make the async venue trait the only production surface; gate/delete the sync `block_in_place` path.
- Add an ONNX session pool or explicit `intra_op_num_threads`; bench under `ws_lag_under_load`.
- Opportunistically remove per-tick allocations.

### P8 — Data security (D1, D2)
- `KALSHI_PRIVATE_KEY_PEM` inline option; retention/redaction for private `data/` channels; verify credential runbook covers the demo key.
- Confirm `pip-audit` + `cargo audit` run in CI; pin deps; add branch protection / SBOM.

### P9 — Redundancy / docs (R1)
- Remove or feature-gate unused `bus`/`allocator` crates if still dead.
- Add superseded headers to v3/v4/hyper/logic docs pointing to this one.

---

## §4. Per-Fix Test-Efficacy Recipes

- **T1** — Unit test: feed a `close_date_updated` envelope through `KalshiNormalizer`; assert the emitted lifecycle does **not** produce `CLOSED`/`PAUSED` and that a simulator holding a resting order on that instrument still has it open afterward.
- **T2** — Rust integration test: enqueue a `PlaceOrder` with `fair_price` such that edge < fee; assert `GatewayError::RiskRejected`. Then a profitable-after-fee order; assert it submits. Add a parity case so Python and Rust reject the same order.
- **T3** — Build a portfolio with one open position, push quotes that drive unrealized below `−daily_loss_limit`; assert the kill switch trips and subsequent intents are rejected — **without** any closing fill.
- **T4** — Same position, wide book; assert liquidation-mark equity = mid-mark equity − (half-spread × qty).
- **T5** — Maker fill with non-zero maker rate produces non-zero fee; re-run a microstructure sweep and assert edge drops.
- **T6** — Resting buy; submit a cancel; print an adverse trade at the level *before* `cancel.effective_at`; assert the resting order still fills (cancel was in-flight).
- **T7** — Two marketable buys in one tick against a 1-level book; assert the second walks to a deeper level (or is unfilled), not the same top level twice.
- **S1** — Add a new strategy of an existing archetype with **only** a TOML file; assert both Python runner and Rust `default_registry`/archetype loader instantiate it and `parity_check` passes.
- **S2/S3** — `ec new-strategy foo --archetype threshold` then `make verify-strategy foo` passes; a live sleeve referencing a strategy with no parity case fails validation.
- **Sp1** — Grep/CI assert the live-runner build does not link the sync venue path; bench shows WS lag unaffected by a slow submit.
- **Sp2** — Bench concurrent scoring with the pool vs single session; assert throughput improves and p99 latency drops.

### Full minimum-efficacy suite (run after every phase)
```bash
cd C:/QWS/eventcontracts
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace
cargo run --manifest-path rust/Cargo.toml -q -p eventcontracts-parity --bin parity_check -- \
  --strategy-spec contracts/examples/weather_threshold/strategy_spec.toml \
  --cases contracts/parity/weather_threshold
cd python && python -m ruff check src tests && python -m mypy src/eventcontracts tests && python -m pytest tests -q && cd ..
```

---

## §5. Final Live-Readiness Gate

A strategy may move from paper to live only when ALL hold:

1. The fee-edge gate (T2) is live and rejects negative-after-fee orders in **both** languages, proven by a shared parity case.
2. The kill switch trips on **unrealized** drawdown (T3) using liquidation marks (T4).
3. `close_date_updated` (and other pure-metadata lifecycle events) do not halt trading (T1).
4. A parity case set exists for the strategy and `parity_check` passes within tolerance (S3 gate).
5. The simulator fees/latency/impact reflect the corrected models (P4); the strategy's edge survives them.
6. The live-runner uses only the async venue path (Sp1).

## §6. Out Of Scope For V5
- New alpha research; multi-account netting; Polymarket live execution; full venue position/balance reconciliation beyond resting-order adoption (track separately).
