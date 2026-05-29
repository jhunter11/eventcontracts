# Comprehensive End-to-End System Audit: EventContracts

**Role:** You are a Principal Trading Systems Architect and Performance Auditor specializing in high-throughput, low-latency prediction market trading systems (Rust & Python).

**Objective:** Conduct an exhaustive, end-to-end architectural, logical, and performance audit of the `eventcontracts` codebase. You must trace the lifecycle of data from the absolute edge (ingestion) to the final output (order routing), aggressively identify bottlenecks, uncover critical errors, and outline missing components required before safely deploying the live production version.

## Execution Instructions for the Agent:

Please perform your audit by deeply reading the relevant files in the codebase (using your search and read tools) across both the `rust/crates/` directory (live execution) and `python/src/` directory (research/modeling). 

Structure your final audit report using the following phases:

### Phase 1: Data Ingestion & The Edge (Trace ALL Data In)
- **Target Areas:** `rust/crates/gateway`, `rust/crates/kalshi`, API/WS clients.
- **Tasks:**
  1. Trace how WebSockets and REST external APIs ingest market data, order book updates, and fills.
  2. Audit the resilience of the ingestion layer: Are reconnects handled gracefully? Are dropped/out-of-order messages accounted for?
  3. Check data normalization: How is external raw data mapped to internal schemas (`contracts/schemas/raw_envelope.schema.json`, etc.)? Is this parsing step zero-allocation/highly efficient?

### Phase 2: Internal Data Flow & Transformations (The Pipeline)
- **Target Areas:** `rust/crates/bus`, `rust/crates/feature-builder`, `contracts/schemas/`
- **Tasks:**
  1. Trace the normalized data as it enters the internal message bus or queue. 
  2. Follow the data into the feature builder. How are features generated for the models (e.g., `tennis_xgboost`, `weather_threshold`)?
  3. Evaluate the structural logic: Is the data handoff between crates the *easiest and most logical* way to accomplish this? Are we unnecessarily copying data?

### Phase 3: Strategy Execution & Model Inference (The Brain)
- **Target Areas:** `rust/crates/model-runtime`, `rust/crates/runtime-hot`, `rust/crates/runner`, ONNX integrations.
- **Tasks:**
  1. Audit the transition from feature generation to model inference.
  2. Evaluate the ONNX runtime integration or any hot-reloaded logic. 
  3. Identify critical errors: Are there type mismatches between Python training schemas and Rust inference schemas? Are there potential panics or race conditions in the model execution loop?

### Phase 4: Order Management & Risk (The Output)
- **Target Areas:** `rust/crates/oms`, `rust/crates/risk`, `rust/crates/allocator`
- **Tasks:**
  1. Trace the signal output from the strategy into the Order Management System.
  2. Evaluate the Risk checks. Are they synchronous and blocking? Could a slow risk check delay an urgent order?
  3. Look for critical logical flaws in order state management (e.g., phantom fills, un-tracked open orders, failure to cancel on disconnect).

### Phase 5: Architecture & Efficiency Review (Speed & Latency Focus)
- **Tasks:**
  1. **Speed Audit:** Identify memory allocations in the hot path, lock contention (Mutex/RwLock abuse), and async overhead. 
  2. **Simplicity Check:** Challenge the architecture. Is the separation between Python (research/data) and Rust (live runner) optimal? Are there over-engineered components that could be simplified?

### Phase 6: Production Readiness & Gap Analysis (The "Go-Live" Checklist)
- **Tasks:**
  1. Identify exactly what is *missing* from this codebase before it can be run in live production with real capital.
  2. Check for missing operational features: Metrics, telemetry, alerting, fail-safes (kill switches), and state reconciliation on startup.
  3. Review the live readiness documentation (`docs/live-readiness-audit-report.md`, `docs/live-rust-runner-roadmap.md`) against the actual code. What hasn't been built yet?

## Output Requirements:
Deliver a comprehensive markdown report addressing each phase. For every vulnerability, inefficiency, or missing feature identified, provide:
1. **The exact file path and line number/module** (if applicable).
2. **The severity/impact** of the issue.
3. **A concrete, code-level remediation recommendation.**
