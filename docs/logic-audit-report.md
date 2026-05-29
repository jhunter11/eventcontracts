# Eventcontracts Full Lifecycle Logic Audit Report

**Superseded:** Use `docs/v5-audit-and-agent-implementation-spec.md` as the
current implementation source of truth. This file is retained for historical
context only.

**Date:** 2026-05-27

This document provides a comprehensive, step-by-step logic audit of the entire `eventcontracts` framework. It traces the lifecycle of a strategy from initial research and tick capture, through modeling and backtesting, into serialization, and finally to Rust-based live execution.

At each step, we explain the intended logic and explicitly call out any architectural disconnects, edge cases, race conditions, or logic bugs found in the Python implementation.

---

## Step 1: Research Idea & Tick Capture (The "Tick Cacher")

**The Intended Logic:**
To backtest an idea, analysts first need historical data. The `IngestionPipeline` (the tick cacher) connects to venues (e.g., Kalshi, Polymarket) or external sources (Open-Meteo) and captures raw websocket/REST data. This data is wrapped in an `EventEnvelope` and written to the `ParquetEventStore` partitioned by `venue/source/date`. The `DuckDbEventStore` then maps these Parquet files into SQL views (`raw_events`, `normalized_events`) for ad-hoc analytical queries and fast bulk processing in Jupyter notebooks. 

**Audit Findings & Logic Flaws:**
1. **Parquet Write Race Condition [Critical]:** `ParquetEventStore._next_part_path` uses `len(glob('part-*.parquet'))` to determine the next filename. This is not thread-safe or process-safe. Concurrent ingestion jobs writing to the same partition will overwrite each other, causing silent data loss.
2. **Replay OOM Vulnerability:** `ParquetEventStore.read()` reads all partitioned files into memory and sorts them locally. For a multi-month historical dataset, this will cause Out-Of-Memory (OOM) crashes. Replay should utilize a streaming k-way merge or push down the time-based sorting to DuckDB.
3. **Determinism Gap:** The `InMemoryEventStore` (used in tests) and `ParquetEventStore` (used in production backtests) use slightly different fallback logic for sorting events when timestamps collide. This guarantees that a backtest run locally vs. in CI may yield divergent event orderings.
4. **Lack of Ingestion Resilience:** The `IngestionPipeline` has no backpressure, retry, or checkpointing logic. A network failure mid-capture leaves a partial write with no programmatic way to resume the job.

---

## Step 2: Feature Engineering & Model Training

**The Intended Logic:**
Inside a Jupyter notebook (`00_strategy_research_loop.ipynb`), analysts query the DuckDB views to generate parity cases and extract normalized features. The `FeatureBuilder` protocol converts incoming events into typed `FeatureVector`s. If an ML model is required, it is trained (e.g., LightGBM, Torch) against these features. The framework mandates that feature builders must be "online"—meaning they maintain their state in an `OnlineFeatureState` object so they can be snapshotted and resumed during live trading.

**Audit Findings & Logic Flaws:**
1. **Hidden State in Feature Builders [High]:** While the `OnlineFeatureState` object exists, actual computation states (like rolling windows, buffers, and EWMA values) are stored in hidden private instance dictionaries (e.g., `self._states` in `RollingMidVwapImbalanceBuilder`). This means feature builders cannot actually be snapshotted and resumed cleanly, breaking the core promise of distributed live execution.
2. **Tick-Based EWMA Distortion:** The reference feature builders use a fixed-step alpha for Exponentially Weighted Moving Averages (EWMA). This makes the features highly sensitive to tick frequency (e.g., a burst of 10 rapid trades) rather than true elapsed time, introducing a time-distortion bias in the features.
3. **Kalshi Normalizer NO-Price Inversion Bug [High]:** During the creation of `NormalizedEvent`s, the `KalshiNormalizer` fails to properly invert the YES price for NO-side trades when a dedicated NO price is absent from the raw payload. This results in corrupt price data being fed to the models.

---

## Step 3: Python Backtesting & Simulation

**The Intended Logic:**
Once features and models are defined, the strategy is run through the `StrategyRunner`. The runner streams events, feeds them to `Strategy.on_event`, and receives `StrategyDecision`s. Decisions are wrapped in an `IntentEnvelope`, gated by the `SleeveRiskGate`, and then simulated by the `MarketPaperSimulator`. The simulator calculates fees, applies latency, estimates queue position, walks the `OrderBook`, and produces `Fill` events. A `PnLTracker` maintains the running portfolio.

**Audit Findings & Logic Flaws:**
1. **Simulator YES/NO Side Conflation [Critical]:** The `MarketPaperSimulator`'s trade application logic (`_apply_trade`) fails to filter trades by `OutcomeSide` (YES vs. NO). Furthermore, it assumes `aggressor_side` maps directly to standard Buy/Sell logic (YES=Buy, NO=Sell). On binary markets, this will fill resting passive orders on the completely wrong side of the contract.
2. **Ghost Liquidity in Book Reconstruction [High]:** The `OrderBookReconstructor` fails to clear out old price levels when new top-of-book quotes arrive. Over a long backtest, this creates massive amounts of synthetic "ghost" liquidity at stale prices, heavily skewing taker slippage calculations.
3. **State Feedback Loop Disconnect:** Fills produced by the simulator are *never* routed back to the `ContextProvider`. Strategies are blind to their own executed positions and cannot reliably execute inventory-management or capital-allocation logic.
4. **PnL Tracker Memory Leak & Fee Ignorance [High]:** The `PnLTracker` eagerly creates a `PositionRecord` for every single instrument it observes a quote for, leading to a massive memory leak in wide-universe backtests. Furthermore, it completely ignores settlement-time fees.
5. **Optimistic Queueing:** When an order book is momentarily unavailable, the `DepthQueueEstimator` defaults to putting the order at the absolute front of the queue (queue ahead = 0). This is highly unsafe for conservative simulation; it should default to the back of the queue or reject the order.
6. **Duplicated Execution Architecture:** The `cli/live_paper.py` script bypasses `StrategyRunner` entirely and re-implements the loop asynchronously. This means backtests and live-paper runs are not actually running the same framework logic.

---

## Step 4: Strategy Serialization (Communicable Format)

**The Intended Logic:**
When a strategy proves its efficacy in the Python backtest, it must be promoted to the production Rust environment. This is achieved via an `ArtifactBundle`. The framework exports the `StrategySpec` and `SleeveSpec` (TOML), the `feature_schema.json`, serialized ONNX models, and a `parity_cases.parquet` file containing deterministic inputs and expected outputs. 

**Audit Findings & Logic Flaws:**
1. **Schema Validation:** *Functioning as intended.* The schemas defined in `contracts/schemas/` effectively validate the TOML/JSON payloads, ensuring no Python-specific objects leak into the bundle.
2. **Risk Gate Bypass [Critical]:** While not an artifact bug, it affects live deployment: The `SleeveRiskGate` explicitly ignores `ReplaceOrder` intents. A strategy could serialize an intent to place a 1-lot order (passing risk), and then immediately replace it with a 10,000-lot order, entirely bypassing position and notional limits.
3. **Over-Restrictive Daily Loss:** The `DailyLossLedger` tracks absolute *gross* losses rather than *net* PnL. A strategy with a high win rate but high volume of small trades will hit the daily kill-switch prematurely, rendering it un-promotable.

---

## Step 5: Rust Runner & Live Deployment

**The Intended Logic:**
The Rust environment (`rust/crates/live-runner`) loads the `ArtifactBundle`. It spins up an identical event loop. The Rust CI pipeline runs the events from `parity_cases.parquet` through the Rust implementations and asserts that the resulting fills, PnL, and decisions are byte-for-byte identical to the Python backtest. If parity passes, the strategy runs live.

**Audit Findings & Logic Flaws:**
1. **Missing Generic Executor:** The Rust side currently lacks a generic execution engine capable of dynamically interpreting the Python-generated artifacts. Instead, the Rust `runner` crate relies on manual, hard-coded Rust re-implementations of the specific strategy logic (e.g., `ThresholdStrategy`). This breaks the entire "communicable format" promise; promotion still requires manual engineering effort.
2. **State Pattern Mismatch:** The Rust `FeatureBuilder` trait is designed expecting a stateless pattern (taking state by value), whereas the Python implementation hides state internally. This means Python features cannot currently be ported to Rust without architectural rewrites on one side of the language boundary.

---

## Summary of Action Items

To stabilize the system for production trading, the following must be addressed immediately:

**Phase 1: Fix Core Market Mechanics (High Priority)**
*   Fix the YES/NO conflation in `MarketPaperSimulator`.
*   Fix the NO-price inversion bug in `KalshiNormalizer`.
*   Fix "ghost liquidity" level-pruning in the `OrderBookReconstructor`.
*   Patch the `ReplaceOrder` vulnerability in the `SleeveRiskGate`.

**Phase 2: Fix Feedback Loops & Leaks (Medium Priority)**
*   Implement a `StatefulContextProvider` that routes `Fill` objects back to the strategy.
*   Resolve the `PnLTracker` memory leak and add settlement fees.
*   Refactor `ParquetEventStore` writes to use UUID-based filenames to avoid race conditions.

**Phase 3: Architectural Unification (Long-term)**
*   Extract hidden states from Python `FeatureBuilder`s into `OnlineFeatureState`.
*   Refactor `live_paper.py` to use `StrategyRunner` natively (or via an async adapter).
*   Develop the generic Rust executor to natively consume the Artifact Bundle without manual strategy rewrites.

---

## 6. Execution Efficiency & Latency Audit

To be "first to the queue" in a production environment, the critical path (Tick -> Normalization -> Strategy -> Risk -> Gateway -> Network) must be entirely free of heap allocations, context switches, and string parsing. 

Currently, the architecture heavily prioritizes cross-language readability over latency, introducing several devastating bottlenecks.

### A. The Rust Live Runner (The Critical Path)
The Rust runner is synchronous, avoiding context-switching, but it is bogged down by the cumulative latency of its data structures.

1. **String-Heavy Hot Path [Critical]:** The core architecture mandates that IDs, decimals, and timestamps are passed between layers as `String`s. This causes massive heap allocation overhead on every tick. The OMS and Risk crates are forced to parse decimal strings into `f64`s to perform arithmetic, and then format them back into strings.
2. **Double JSON Serialization [High]:** Events and intents are constantly serialized and deserialized to/from `serde_json::Value` on the hot path. For example, strategies re-parse `payload_json` on every event, and the Gateway parses intent JSON twice per order (once to enqueue, once to process).
3. **Manual RFC3339 Parsing [High]:** The Gateway drops to manually parsing RFC3339 timestamp strings on every evaluation loop just to check order age limits. 
4. **Lack of Zero-Copy:** The `kalshi` normalizer pulls raw bytes off the network buffer and immediately allocates owned `String` fields in structs, rather than using `&str` lifetimes and zero-copy `serde` parsers.

### B. The Python Simulation Engine
While not used for live trading, Python simulation latency directly impacts research velocity.
1. **Frozen Dataclass Overhead:** The domain models (`QuoteEvent`, `NormalizedEvent`) are implemented as frozen Pydantic/dataclass objects with recursive validation and expensive `FrozenMap` metadata. 
2. **Object Instantiation per Tick:** Creating a fresh graph of Python objects for every tick across millions of rows will severely bottleneck backtests. Moving toward vectorized backtesting (Polars/VectorBT) or pushing the simulation loop down to PyO3/Rust is required for scale.

### Recommendations for Ultra-Low Latency
To fix these bottlenecks and win queue priority, the Rust architecture requires an overhaul:
*   **Abolish Strings:** Replace string-based decimals with integer fixed-point math (`i64`). Replace RFC3339 strings with `u64` nanoseconds since epoch. Replace string IDs with stack-allocated fixed-size arrays (`[u8; 16]` or `u64`).
*   **Zero-Copy Parsers:** Implement `simd-json` or strict zero-copy `serde` parsers for the venue adapters. Extract only the bytes needed to make a decision without parsing the entire DOM.
*   **Lock-Free Concurrency:** If the architecture moves to a multi-threaded design (e.g., separating the network read thread from the strategy execution thread), use a lock-free ring buffer (e.g., LMAX Disruptor pattern or `crossbeam` queues) pinned to dedicated CPU cores, completely bypassing the async executor (Tokio) for the critical path.
# Superseded

This audit is superseded by `docs/v5-audit-and-agent-implementation-spec.md`.
