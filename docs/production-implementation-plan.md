# Production Implementation Plan & Technical Specification

This document breaks down the final remaining engineering, validation, and operational tasks required to move to a safe, real-money live deployment supporting multiple venues (Kalshi, Polymarket) and a distributed cloud pod architecture.

It has been comprehensively expanded based on the findings from the hyper-comprehensive codebase audit (`docs/hyper-comprehensive-codebase-audit-agent-spec.md`). Technical Ambiguity is explicitly minimized here. Specific crates, endpoints, architectural contracts, data flows, and performance constraints are defined.

---

## 0. Exact Data Flow & Type Specifications
*The strict path of a single market tick through the distributed system.*

1. **Ingestion (WebSocket):**
   - **Type:** Raw bytes -> `serde_json::value::RawValue` (avoiding full DOM allocation).
   - **Component:** `eventcontracts-ingestor`.
2. **Normalization (Ingestor -> NATS):**
   - **Type:** `NormalizedEventRecord` (JSON strings for public contract, but fast-path fields parsed eagerly).
   - **Bus Topic:** `market.kalshi.KXHIGHNY`.
3. **Strategy Ingestion (NATS -> Worker):**
   - **Type:** `StrategyEvent` (Rust enum containing `FixedPrice` `i64` ticks, NOT floats or strings).
   - **Component:** `eventcontracts-worker`. Parses the JSON exactly once.
4. **Context & Execution (Worker):**
   - **Type:** `StrategyContext` evaluates the event. Outputs a `DecisionPayload` (e.g., `PlaceOrder { outcome_side: OutcomeSide::Yes, side: Side::Buy, ... }`).
   - **Wrapper:** Wrapped into an `IntentEnvelopeRecord`.
5. **Intent Routing (Worker -> NATS -> CEG):**
   - **Type:** `IntentEnvelopeRecord` sent over NATS topic `intents.{sleeve_id}`.
6. **Risk & Venue Submission (CEG):**
   - **Type:** `IntentSnapshot`. CEG parses the intent, validates it against `SleeveState` (in memory).
   - **Network:** `KalshiVenueClient` maps it to Kalshi's JSON schema and fires the HTTP POST using `reqwest`.
7. **Fill Feedback (CEG -> NATS -> Worker):**
   - **Type:** Venue response -> `OwnFillEvent` -> NATS topic `fills.{sleeve_id}` -> Strategy context update.

---

## 00. Speed Considerations (Ultra-Low Latency)
*Future implementations MUST adhere to these performance constraints.*

- **Zero-Copy & Binary Serialization:** Replace `serde_json` over NATS with a zero-copy binary format like `rkyv` or `bincode` for `NormalizedEventRecord` and `IntentEnvelopeRecord`. JSON should only exist at the absolute outer boundary (communicating with Kalshi/Polymarket).
- **No Strings on the Hot Path:** Strings allocate. Prices and quantities MUST remain `FixedPrice(i64)` internally. `client_order_id` and `instrument_id` should use stack-allocated `[u8; 16]` arrays or interned string references (e.g., `smol_str` or `ustr`) rather than `String` clones.
- **Lock-Free Concurrency:** Inside the CEG, avoid highly contended `Arc<RwLock<SleeveState>>`. Use channel-based actor models (e.g., passing mutations to a single pinned thread) or lock-free data structures (`crossbeam` / `flume`) for processing risk checks.
- **Pre-Allocated Buffers:** In `eventcontracts-ingestor` and NATS consumers, use pre-allocated buffers (e.g., `bytes::BytesMut`) for reading network sockets to eliminate per-tick allocations.
- **ONNX Execution:** In `model-runtime`, use `ort` session execution pools and pinned memory (`Tensor::from_array`) to avoid copying arrays when evaluating snapshots.
- **Performance Budgeting:** Introduce `criterion` benchmarks for normalization, risk evaluation, and gateway last-look. CI must run `cargo bench --workspace --no-run` and fail if p99 end-to-end latency regresses.

---

## Track A: Live-Paper Validation (Tennis Specific)
*Goal: Prove the strategy has edge in a live environment before risking capital.*

### Task A.1: Finalize Tennis Model Artifact
- **Implementation:** Execute `notebooks/01_tennis_xgboost_research.ipynb`.
- **Artifact Contract:**
  - `model.onnx` must accept a `float32` tensor of shape `[1, N]` where N matches `feature_schema.json`.
  - Output tensor must be a probability array where index `[0, 1]` is Player 1 win probability.
- **Validation:** `cargo test -p eventcontracts-model-runtime` must pass against a generated `parity_cases.parquet` using `ort` (ONNX Runtime for Rust).
- **Implementation Notes:** The Rust `ort` integration must pre-allocate the input tensor arrays and simply update values per-tick to avoid allocation overhead during inference. 

### Task A.2: Snapshot Ingestion Pipeline
- **Implementation:** A Python script deployed as a cron job (or systemd timer on the pod) running daily at 00:00 UTC.
- **Data Sources:** Scrape upcoming matches from ATP/WTA schedules; fetch Elo from Jeff Sackmann CSVs/API.
- **Output:** Appends to `/data/tennis/upcoming_matches.jsonl`.
- **JSONL Schema:** Must strictly map to `eventcontracts_feature_builder::TennisMatchSnapshot` (e.g., `{"market_id": "KXTENNIS-...", "p1_elo": 1500.5, ...}`).
- **Data Leakage Check:** Strict enforcement that NO future outcome data, post-match statistics, or dynamic Elo adjustments from during the match are included in the snapshot. The snapshot must represent $T_0$ (match start).

### Task A.3: Live-Paper Execution & Analysis
- **Implementation:** Run `eventcontracts-live-runner --tennis-artifact <dir> --tennis-snapshots-jsonl <file>` using the `DryRunGateway`.
- **Validation Criteria:** 
  - Collect 7 days of logs.
  - Extract `SimulatedFill` records. Compare execution price to the actual Kalshi `TradeEvent` tape at `fill.filled_at`. Slippage tolerance must be < 2 cents average.

---

## Track B: Core Execution & Multi-Venue Adapters
*Goal: Secure, rate-limited execution across REST (Kalshi) and CLOB (Polymarket).*

### Task B.1: Kalshi VenueClient & ECDSA Auth
- **Crate:** `eventcontracts-kalshi`
- **Auth Implementation:** 
  - Load ECDSA private key (PEM format) from `KALSHI_PRIVATE_KEY_PATH`.
  - Use `p256::ecdsa::SigningKey` and `sha2::Sha256` to sign `timestamp + method + path`.
  - Headers: `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-SIGNATURE`, `KALSHI-ACCESS-TIMESTAMP`.
- **Endpoints:**
  - Submit: `POST /trade-api/v2/portfolio/orders`
  - Cancel: `DELETE /trade-api/v2/portfolio/orders/{client_order_id}`
- **Translation:** 
  - `price`: `intent.price.ticks() / 10_000` (cents).
  - `action`: `if intent.side == Buy { "buy" } else { "sell" }`.
  - `side`: `if intent.outcome_side == Yes { "yes" } else { "no" }`.
- **Implementation Notes:** See Issue Log #1. `kalshi` crate is currently using `rsa` instead of `p256`. The cryptographic primitive needs to match the key format you obtain from Kalshi's developer portal. Ensure Base64 encoding of the signed bytes is correct (e.g., standard vs URL-safe).

### Task B.2: Polymarket VenueClient & Web3 Auth
- **Crate:** `eventcontracts-polymarket`
- **Library:** Use `alloy` (specifically `alloy-signer`, `alloy-primitives`, `alloy-sol-types`).
- **Auth Implementation:** EIP-712 structured data signing.
  - Domain: `name: "Polymarket CTF Exchange", version: "1", chainId: 137, verifyingContract: <CLOB_ROUTER_ADDRESS>`.
  - Message: `Order { salt, maker, signer, taker, tokenId, makerAmount, takerAmount, expiration, nonce, feeRateBps, side, signatureType }`.
- **Endpoints:** Polymarket CLOB API `POST https://clob.polymarket.com/order`.
- **Translation:**
  - `tokenId`: Requires a lookup mapping `InstrumentId` to the specific Yes/No ERC1155 Token ID.
  - `makerAmount` / `takerAmount`: Calculated using price and USDC decimals (1e6).
- **Implementation Notes:** Scaffold the crate explicitly in Cargo.toml. Ensure you implement the specific `alloy` EIP-712 traits so serialization handles the padding and offsets as the smart contract expects. Keep the token lookup cached locally.

### Task B.3: Global Rate Limiting & Nonce Management
- **Kalshi Limiter:** Use `governor` crate. `Quota::per_second(nonzero!(10u32))`. Wait asynchronously if bucket is empty.
- **Polymarket Nonces:** Maintain an `AtomicU64` for order salts/nonces. If the CLOB API returns "Nonce already used", auto-increment and retry exactly once.

### Task B.4: Executable BBO Last-Look
- **Implementation:** Modify CEG final last-look checks to strictly require an executable BBO (latest bid/ask on the correct side).
- **Constraint:** Remove any fallback to the `mark_price_ticks`. Mark price may only be used for gross exposure valuation, never as the submit-time price reference.
- **Action:** Add `LastLookConfig.require_executable_bbo = true`.

### Task B.5: Limit Order Enforcement
- **Implementation:** Completely disable `OrderType.MARKET` in live environments.
- **Constraint:** All intents must be bounded Limit Orders (specifically IOC/GTC limits) that carry the exact quote-derived `MarketSnapshot` used to choose the price. Add a live-promotion linter to fail any strategy emitting `MARKET` types.

---

## Track C: Reliability & Reconciliation (The Safety Net)
*Goal: Handle distributed system edge cases (netsplits, API timeouts).*

### Task C.1: WebSocket Sequence-Gap Recovery
- **State Machine:** `KalshiWsClient` transitions: `Connected` -> `GapDetected` -> `FetchingSnapshot` -> `Connected`.
- **Implementation:** 
  - Track `last_seq`. If `msg.seq != last_seq + 1`, pause bus broadcasting.
  - Hit `GET /trade-api/v2/markets/{ticker}/orderbook`.
  - Emit a full `OrderBookEvent` to the bus to reset all strategy reconstructions.
  - Discard buffered WS messages older than the REST snapshot. Resume broadcasting.
- **Implementation Notes:** Suspending the bus broadcast means the `kalshi` client needs a mechanism to pause reading or queue messages without OOMing. Use a bounded channel or explicit backpressure. See Issue Log #4.

### Task C.2: Global OMS Full Reconciliation Loop
- **Implementation:** A `tokio::spawn` task inside the CEG (Central Execution Gateway).
- **Scope:** Fetch all open/resting orders, recent fills since last local checkpoint, positions, and available balance.
- **Polling:** Every 10 seconds, paginating through `GET /trade-api/v2/portfolio/orders?status=resting`.
- **Reconciliation Logic:**
  - If `local_orders.contains(id)` but `!venue_orders.contains(id)`: Assume filled or canceled. Fetch specific order status `GET /orders/{id}`. Emit `OwnFillEvent` or `OwnOrderRejectEvent`.
  - If `venue_orders.contains(id)` but `!local_orders.contains(id)`: Orphaned order. Send `DELETE` immediately.
- **Startup:** Live submit must refuse to run unless the operator chooses a startup reconciliation policy (`--reconcile-on-start` or `--cancel-orphans-on-start`).

### Task C.3: Cancel/Replace Ambiguity Recovery
- **Implementation:** When `DELETE` or `POST` timeouts occur (HTTP 504 / reqwest timeout):
  - Mark order state as `SubmitUnknown` locally (remaining risk-open).
  - Block further intents for this `client_order_id`.
  - Trigger an immediate out-of-band OMS Reconciliation fetch for this specific order to resolve `SubmitUnknown` -> `Open` or `Canceled`.

---

## Track D: Distributed Cloud Pod Architecture
*Goal: Decouple strategies from risk/execution to prevent single-point-of-failure crashes.*

### Task D.1: Inter-Process Communication (NATS)
- **Broker:** Deploy `nats-server` (JetStream disabled, purely in-memory pub/sub for <1ms latency).
- **Topics:**
  - `market.kalshi.{ticker}`: `NormalizedEventRecord` (Binary serialized via `rkyv`).
  - `intents.{sleeve_id}`: `IntentEnvelopeRecord` (Binary serialized) from Strategy to CEG.
  - `feedback.{sleeve_id}`: `IntentRejected`, `IntentAccepted`, `VenueSubmitUnknown`, `VenueAcked`, `VenueTerminal`, `OwnFillEvent` from CEG to Strategy.
- **Implementation Notes:** Introduce the `async-nats` crate. Write explicit Publisher and Subscriber traits in the `bus` crate. Abstract the binary serialization codec so we can swap `serde_json` to `rkyv` gradually.

### Task D.2: Central Execution Gateway (CEG) Daemon
- **Implementation:** `rust/crates/ceg-daemon` (Must be split from monolithic `live-runner`).
- **Role:** 
  1. Subscribes to `intents.*`.
  2. Holds `SleeveState` (cash, positions, open orders) via a high-performance actor model (no `RwLock` contention).
  3. Runs `SleeveRiskGate::evaluate`. 
  4. If approved: Updates local state, performs Send-Time Last-Look, forwards to VenueClient.
  5. Upon venue ACK/Fill/Reject: Publishes to `feedback.{sleeve_id}`.
- **Risk Invariant:** The CEG is the *absolute authority* on capital. Strategies merely request capital allocation.
- **Implementation Notes:** See Issue Log #3. Currently, logic is packed into `live-runner`. This requires splitting targets in Cargo workspace and moving risk components out of the strategy hot-loop.

### Task D.3: Modular Strategy Workers
- **Implementation:** `rust/crates/strategy-worker`
- **CLI:** `eventcontracts-worker --strategy <path.toml> --sleeve <path.toml> --nats-url nats://localhost:4222`
- **Behavior:** 
  - Subscribes to `market.{venues}` and `feedback.{sleeve_id}`.
  - Maintains a local `StrategyContext`.
  - **Crucial Update:** Strategy-local state MUST NOT assume an emitted intent is pending or successful. Local pending state must only advance based on events arriving on the `feedback` topic.
  - **Partial Fills:** Tracks `desired_position` vs `confirmed_position`.
  - Completely stateless; can be restarted instantly. Recovers state by requesting a snapshot from the CEG via NATS Request-Reply.

### Task D.4: Strategy Logic Enforcement (Adverse Move & Edge)
- **Cancel-on-Adverse-Move:** Implement a resting-order TTL per priority tier. If the latest BBO moves against a resting order by `x` bps or a new trade crosses fair value, the CEG should cancel the resting limit order.
- **Executable Edge Verification:** Every model strategy must compute executable edge (`fair_value - ask` for buys). The edge must exceed `min_executable_edge_bps` which includes fees, expected slippage, and an adverse-selection buffer.

### Task D.5: Pod Orchestration
- **Implementation:** `docker-compose.live.yml`
- **Services:**
  - `nats`: Official NATS alpine image.
  - `ingestor`: Runs `eventcontracts-kalshi-ws`.
  - `ceg`: Runs `eventcontracts-ceg-daemon` (mounts `.env` with keys).
  - `worker-tennis`: Runs `eventcontracts-worker` with the ONNX mount.

---

## Track E: Operations & Controls
*Goal: Give humans the ability to stop the machine.*

### Task E.1: Mandatory Sleeve Config Loading
- **Implementation:** Add `--sleeve-spec <path>` to `eventcontracts-live-runner` and workers.
- **Action:** The CEG must build `RiskLimits`, sleeve ID, capital cap, and portfolio group metadata from the sleeve file. It must refuse `--live-submit` if no sleeve spec is provided (unless explicitly overridden for paper/demo).
- **Portfolio Correlation:** Implement grouping across sleeves (e.g., `risk_group`, `correlation_cluster`, `max_cluster_gross`). The allocator must consume these tags.

### Task E.2: Live-Promotion Manifest and Parity V2
- **Implementation:** Add a manifest per promoted live strategy linking the `strategy_spec`, `sleeve_spec`, parity cases, and external replay fixtures.
- **Parity V2:** Extend parity checks from static outputs to stateful sequences comparing decision payloads, priority tiers, TTL, audit metadata, risk verdicts, gateway verdicts, and final strategy state.

### Task E.3: Global Kill-Switch
- **Implementation:** CEG exposes `POST /admin/kill` via an `axum` HTTP server on port 8080.
- **Action:**
  - Sets `AtomicBool` `KILL_SWITCH_TRIPPED = true`.
  - Pre-trade risk immediately returns `RiskDecision::Rejected("kill_switch")` for all new intents.
  - Iterates all `open_orders` and fires `VenueClient::cancel_order` concurrently via `futures::future::join_all`.

### Task E.4: Persistent State Checkpoints
- **Implementation:** Use `sqlite` (via `rusqlite` crate) attached to a mounted Docker volume `/data/ceg_state.db`.
- **Action:** On every `OwnFillEvent` or `CashTransfer`, execute an `INSERT OR REPLACE` into the `sleeve_positions` and `sleeve_cash` tables. This guarantees the CEG knows exact exposure even after a hard SIGKILL.

### Task E.5: Data Security & Hygiene
- **Implementation:** Add secret scanning to CI, redacting/encrypting private raw payload storage (for account, order, and fill data), and generate SBOM/vulnerability scans on Docker builds.

### Task E.6: CEG Startup Safety (Orphan Sweep)
- **Implementation:** Before CEG binds to the NATS `intents.*` topics, it MUST:
  1. Fetch all open orders from Kalshi and Polymarket.
  2. Filter for orders matching prefix `qws-`.
  3. Load persistent checkpoint (Task E.4).
  4. If a live order is NOT in the persistent checkpoint, execute `CANCEL`.

### Task E.7: Webhook Alerting
- **Implementation:** Use `reqwest` to send Slack incoming webhooks.
- **Payload:** `{"text": "🚨 [CEG] Risk Reject in weather-arb: max_exposure exceeded"}`.
- **Triggers:** Gateway 5xx errors, Kill-Switch activation, Risk Gate rejections, WS Gap recoveries.

---

## Track F: Future Expansion
*Goal: Broaden the framework's capabilities to non-binary markets and complex microstructure.*

### Task F.1: Categorical Multi-Outcome Core Migration
- **Implementation:** 
  - Change `enum OutcomeSide { Yes, No }` to `pub struct OutcomeId(pub String)`.
  - Update `NormalizedEventRecord` schemas to accept string outcome IDs.
  - Update Kalshi normalizer to map "Yes" -> "yes", "No" -> "no".
  - Update Polymarket normalizer to map CTF Token IDs to string hex hashes.
  - Risk engine updates: position tracking moves from `(InstrumentId, OutcomeSide)` to `(InstrumentId, OutcomeId)`.
- **Implementation Notes:** `OutcomeId` MUST wrap a `smol_str::SmolStr` or an interned string token (like an integer ID) rather than allocating standard `String` on the heap for every tick.

### Task F.2: Spoof-Resilient Microstructure
- **Implementation:** Add persistence-weighted order book features. Disable live promotion for pure L1 imbalance strategies until they track cancel-rates, quote-flicker, and minimum displayed size duration.

---

## Excruciatingly Detailed Issues Log
*Known implementation roadblocks and required technical decisions uncovered during the plan expansion.*

1. **Cryptography Primitive Mismatch (RSA vs ECDSA)**
   - *Detail*: The roadmap specification for Task B.1 explicitly states `Load ECDSA private key` and `p256::ecdsa::SigningKey`. However, the current code inside `rust/crates/kalshi/src/auth.rs` uses `rsa::pss` (RSA-PSS-SHA256). Kalshi V2 generally generates RSA keys natively when using their dashboard for automated trading API keys. Attempting to force ECDSA may fail on Kalshi's backend, or require regenerating specific API key types.
   - *Action*: Validate with the Kalshi Developer Portal whether ECDSA is supported. If not, update Task B.1 to strictly require RSA keys and maintain the existing `rsa` crate implementation.

2. **Zero-Copy Serialization Overhead (IPC)**
   - *Detail*: The plan mandates `rkyv` or `bincode` for zero-copy binary serialization over NATS to achieve ultra-low latency. Currently, `rust/crates/contracts` relies heavily on `serde` and `serde_json` for cross-language parity with Python. Integrating `rkyv` requires annotating all domain types with `#[derive(Archive, Deserialize, Serialize)]` from the `rkyv` crate. Handling string parsing in zero-copy environments (e.g., mapping incoming JSON payloads to zero-copy internal buffers) presents major lifetime boundary issues across the NATS consumer.
   - *Action*: Create an intermediate codec layer. Expose standard `serde_json` for development and a specialized fast-path struct in `rust/crates/contracts/src/fast_binary.rs` explicitly built for `rkyv` before refactoring everything.

3. **Crate Missing / Monolithic Artifacts**
   - *Detail*: Tasks D.2 (`ceg-daemon`) and D.3 (`strategy-worker`) do not currently exist in the Rust workspace. Currently, `rust/crates/live-runner` acts as a monolithic binary incorporating runner, gateway, risk, and feature building. 
   - *Action*: Update `rust/Cargo.toml` to scaffold `ceg-daemon` and `strategy-worker`. The inner loops of `live-runner` need to be decoupled, moving the `RiskGate` logic to the CEG and the strategy logic to the worker.

4. **WebSocket Snapshot Gap Pause Deadlock**
   - *Detail*: Task C.1 requires pausing bus broadcasting when a sequence gap is detected, making a REST request to fetch the snapshot, and dropping older WS messages. If `eventcontracts-ingestor` pauses without draining the TCP buffer from the WebSocket, the OS network buffer will fill up, potentially causing Kalshi to drop the connection entirely, creating an infinite loop of disconnects.
   - *Action*: The ingestor must continue draining the WebSocket stream into an internal unbounded (or large bounded) buffer while awaiting the REST snapshot, dropping only messages preceding the new snapshot sequence once it arrives.

5. **Categorical Outcomes String Violation**
   - *Detail*: Task F.1 suggests `pub struct OutcomeId(pub String)`. Task 00 strictly prohibits `Strings on the Hot Path` because heap allocation destroys microsecond latency guarantees.
   - *Action*: Use `smol_str::SmolStr` or an `OutcomeId([u8; 16])` (e.g., UUID or short hex hash) for polymorphic tokens. Never use `std::string::String` on the internal risk or execution path.

6. **Alloy / Polymarket Crypto Bloat**
   - *Detail*: Implementing `alloy` for Web3 EIP-712 signing in `eventcontracts-polymarket` will introduce massive dependency trees (Ethereum crypto primitives) which can bloat the binary and increase compile times significantly compared to the lightweight Kalshi REST client.
   - *Action*: Ensure `alloy` dependencies are scoped strictly to the `eventcontracts-polymarket` crate and do not bleed into the shared `contracts` or `ceg-daemon` hot-path models.

7. **Side-Specific BBO Missing Fallback**
   - *Detail*: Last-look validation will now strictly require executable BBO. If a partial quote drops a side, or a fallback test generates only mark prices, live submit will reject the order.
   - *Action*: Ensure normalizers cleanly persist BBO state per side.

8. **Strategy Emit-Time Assumptions**
   - *Detail*: Python/Rust strategies incorrectly advance state simply because an intent was emitted, locking local capital until a timeout.
   - *Action*: Strictly implement the `feedback` NATS channel routing `IntentRejected` / `VenueAcked` to release local state.

9. **Fake Liquidity Spoofing**
   - *Detail*: L1 imbalance features in `eventcontracts-worker` can be spoofed by adversaries placing and canceling large top-of-book orders.
   - *Action*: Add persistence-weighted imbalance tracking.

10. **Inline REST Blocking Data Loop**
    - *Detail*: Currently, Kalshi venue client uses `block_in_place` which pauses market data ingestion while submitting an order, leading to stale subsequent decisions.
    - *Action*: Decoupling into CEG and workers via NATS fixes this by allowing the ingestor to run unblocked while the CEG awaits REST execution.