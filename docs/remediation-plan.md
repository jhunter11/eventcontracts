# Eventcontracts Remediation & Improvement Plan

This document outlines a step-by-step, actionable plan to fix the logic bugs, architectural gaps, and latency bottlenecks discovered during the full-lifecycle logic audit (see `logic-audit-report.md`).

The plan is divided into five phases, ordered by severity and architectural dependency. 

## Current Implementation Status (2026-05-27)

- **Phase 1:** Complete. Market logic, YES/NO inversion, reconstructed book cleanup, and conservative unknown-book queueing are implemented and covered by tests.
- **Phase 2:** Complete. Stateful context, PnL memory behavior, and settlement fee/net PnL accounting are implemented.
- **Phase 3:** Complete for product flow. Feature-builder state has been externalized, `MarketPaperSimulator` accepts `IntentEnvelope` directly, backtest/weather diagnostics and integration tests use the envelope-native path, and `live_paper.py` feeds events through `StrategyRunner`. Legacy `OrderIntent`/`SimulatedFill` remain only as adapter compatibility for gateway/tests.
- **Phase 4:** Complete for the current live-paper critical path. Rust risk and strategy pricing now use fixed-point integer math, runner strategies consume typed `StrategyEvent` records, YES/NO outcome side is explicit in gateway payloads, and Kalshi WS quote/trade normalization avoids fully materializing every tick as `serde_json::Value`. Full binary IPC and non-string public contracts remain a future ultra-low-latency upgrade.
- **Phase 5:** Complete for tennis live-paper deployment. Parquet part filenames use UUIDs, replay uses k-way merge over sorted streams, the Rust model runtime validates/loads the tennis ONNX bundle, and the live runner can score upcoming tennis snapshot JSONL files into strategy events via `--tennis-artifact` plus `--tennis-snapshots-jsonl`. Generic categorical multi-outcome support and live venue order submission remain future production work.

---

## Phase 1: Critical Market Logic & Safety Fixes
*These issues represent dangerous bugs that would cause incorrect fills, risk bypasses, or corrupted market data. They must be fixed before any real capital is deployed.*

1. **Fix Risk Gate Bypass**
   - **File:** `python/src/eventcontracts/risk/policy.py`
   - **Action:** Update `SleeveRiskGate.evaluate` to properly validate `ReplaceOrder` intents. A replacement must be evaluated as if the original order is canceled and a new order is placed, applying the same position, notional, and exposure limits to the new price/quantity.
2. **Fix Simulator YES/NO Conflation**
   - **File:** `python/src/eventcontracts/execution/market_simulator.py`
   - **Action:** Rewrite `_apply_trade`. It currently conflates `OutcomeSide` (YES/NO) with the aggressor side (Buy/Sell). Ensure resting passive orders are filled strictly based on the order's explicit `order_side`, the touch price, and whether the trade exhausted the queue ahead. 
3. **Fix Kalshi Ask Ladder Sorting (The "Test Blindness" Bug)**
   - **Files:** `python/src/eventcontracts/normalization/kalshi.py` and `adapters/venues/kalshi/client.py`
   - **Action:** Asks must be sorted *ascending* (lowest price first). The current logic using `reversed()` and `100 - p` sorts them descending. This breaks marketable order matching for quantities greater than the top level. Fix the sorting logic.
4. **Fix NO-Price Inversion Bug**
   - **File:** `python/src/eventcontracts/normalization/kalshi.py`
   - **Action:** Ensure that if a raw Kalshi event only contains a `yes_price`, the `no_price` is correctly inverted (e.g., `100 - yes_price`) when creating the `NormalizedEvent`.
5. **Fix Ghost Liquidity in Book Reconstruction**
   - **File:** `python/src/eventcontracts/replay/order_book.py`
   - **Action:** When receiving a top-of-book quote update, explicitly clear out stale price levels from the reconstructed book.
6. **Fix Optimistic Queueing**
   - **File:** `python/src/eventcontracts/execution/queue.py`
   - **Action:** Change `DepthQueueEstimator` to assume the back of the queue (or reject) if the order book state is missing, rather than defaulting to `0` (front of queue).

---

## Phase 2: Feedback Loops & Memory Leaks
*These issues cause systemic failures in backtests or prevent strategies from seeing their own trades.*

1. **Implement `StatefulContextProvider`**
   - **File:** `python/src/eventcontracts/runner/base.py` / `context.py`
   - **Action:** Create a context provider that maintains a running tally of positions and cash. Wire the execution `Fill` outputs directly into this provider so the `StrategyContext` always reflects the most up-to-date portfolio state.
2. **Fix `PnLTracker` Memory Leak**
   - **File:** `python/src/eventcontracts/execution/pnl.py`
   - **Action:** Stop eagerly creating `PositionRecord` objects for instruments the strategy has no exposure to just because a `QuoteEvent` arrived. Only track mark prices for assets with non-zero positions or pending orders.
3. **Add Settlement Fees & Net PnL**
   - **File:** `python/src/eventcontracts/execution/pnl.py` & `risk/state.py`
   - **Action:** Apply fee models to settlement events. Update `DailyLossLedger` to calculate actual *net* daily PnL, not just gross losses, to avoid prematurely triggering kill-switches on high-volume strategies.

---

## Phase 3: Architectural Unification (Python)
*These steps unify the divergent simulation and live-paper code paths, ensuring backtests accurately reflect live execution.*

1. **Unify `MarketPaperSimulator` and `ExecutionSimulator`**
   - **Files:** `python/src/eventcontracts/execution/simulator.py` & `market_simulator.py`
   - **Action:** Delete `OrderIntent` and `SimulatedFill`. Refactor `MarketPaperSimulator` to accept `IntentEnvelope` directly and emit standard `domain.fills.Fill` objects. 
2. **Deprecate the `live_paper.py` Custom Loop**
   - **File:** `python/src/eventcontracts/cli/live_paper.py`
   - **Action:** Remove the duplicated event normalization, risk gating, and strategy feeding logic. Instantiate a canonical `StrategyRunner` and feed it events from an `asyncio.Queue` adapter.
3. **Extract Hidden Feature Builder State**
   - **File:** `python/src/eventcontracts/features/builders.py`
   - **Action:** Move rolling windows, buffers, and EWMA states out of private instance variables (`self._states`) and into the serializable `OnlineFeatureState` object. Switch EWMA calculations to be time-weighted rather than tick-step weighted.

---

## Phase 4: Rust Latency Redesign (Ultra-Low Latency)
*These changes require breaking the current string-based domain model to achieve queue priority in the live runner.*

1. **Abolish Strings on the Hot Path**
   - **Files:** `rust/crates/contracts/src/lib.rs` and all dependent crates.
   - **Action:** 
     - Replace string decimals with fixed-point integers (`i64` representing basis points or specific venue ticks).
     - Replace RFC3339 timestamp strings with `u64` (nanoseconds since UNIX epoch).
     - Replace UUID strings with `[u8; 16]` arrays or `u64` hashes.
2. **Implement Zero-Copy Venue Deserialization**
   - **Files:** `rust/crates/kalshi/src/`
   - **Action:** Remove `serde_json::Value`. Use `simd-json` or strict zero-copy `serde` parsers with `&'a str` lifetimes to extract only the necessary fields directly from the network buffer.
3. **Remove Double JSON Serialization**
   - **Files:** `rust/crates/runner/src/lib.rs` & `rust/crates/gateway/src/lib.rs`
   - **Action:** Stop passing raw JSON payloads into the strategy on every tick. Pre-parse events into typed structs. Stop parsing Intent JSON twice in the gateway.

---

## Phase 5: Production Readiness & Multi-Venue Support
*These final steps expand the system's capabilities for cross-venue deployment.*

1. **Abstract YES/NO Binary Hardcoding**
   - **Files:** `python/src/eventcontracts/domain/models.py`
   - **Action:** The system currently hardcodes `OutcomeSide` to YES/NO. To support Polymarket categorical markets, this must be refactored to support generic `outcome_id`s.
2. **Build the Generic Rust Executor**
   - **Files:** `rust/crates/live-runner/src/main.rs`
   - **Action:** Replace the manual Rust strategy implementations (e.g., `ThresholdStrategy`) with a generic engine capable of loading ONNX models, interpreting the `feature_schema.json`, and natively executing the promoted `ArtifactBundle`.
3. **Fix the Ingestion Race Condition**
   - **Files:** `python/src/eventcontracts/storage/parquet_store.py`
   - **Action:** Replace the `len(glob('part-*.parquet'))` logic with UUID-based or high-resolution timestamp filenames to ensure concurrent tick cachers do not overwrite each other's data. Implement k-way merge for replay to avoid OOM crashes on large datasets.
