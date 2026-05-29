# Live Readiness Audit Report

**Superseded:** Use `docs/v5-audit-and-agent-implementation-spec.md` as the
current implementation source of truth. This file is retained for historical
context only.

**Date:** May 27, 2026
**Target:** Kalshi Real-Money Tennis Deployment

## Executive Summary
Phases 1 through 5 of the codebase remediation are structurally complete. The Python backtesting path is unified, the Rust hot-path avoids string serialization, and the ONNX model loader successfully bridges predictive research into live-paper strategy events. However, **the system is NOT ready for live, real-money execution.** Critical operational, infrastructural, and reconciliation gaps must be addressed to prevent catastrophic capital loss.

## Audit Findings

### 1. Execution & Venue Connectivity (Critical Gap)
- **Missing Venue Adapter:** The Rust `live-runner` currently relies on a `DryRunGateway`. The actual Kalshi REST client (`KalshiVenueClient`) for submitting real orders has not been implemented.
- **Rate Limit Management:** The gateway lacks an internal rate-limiter, meaning a runaway strategy could easily trigger a venue ban (429 responses).
- **WebSocket Resilience:** The current `KalshiWsClient` lacks automated reconnect logic and sequence-gap detection. If the connection drops momentarily, the system will miss trades and quote updates, feeding stale state to the strategies.

### 2. State & Reconciliation (High Severity)
- **Order State Drift:** The framework assumes an order is open until explicitly filled or canceled. There is no background "Reconciliation Loop" (OMS Sync) to poll the venue and verify that our local order state matches the venue's actual order state.
- **Double-Entry Ledger:** The `PnLTracker` is designed for paper trading. Real money requires a robust ledger to track locked capital vs. settled cash, factoring in actual venue fees rather than theoretical `FeeModel` estimates.

### 3. Risk & Safety Controls (High Severity)
- **No Global Kill-Switch:** While pre-trade limits exist, there is no out-of-band "panic button" that an operator can hit to immediately suspend the runner and send bulk cancel requests to the venue.
- **Orphaned Order Risk:** If the Rust runner crashes, any open orders on Kalshi will remain active. The system needs an initialization phase that fetches open orders on startup and either adopts them into the strategy context or cancels them.

### 4. Ops & Security (Medium Severity)
- **Secrets Management:** The configuration specs and environment setups do not yet securely inject ECDSA private keys or API credentials into the Rust runtime.
- **Deployment Containerization:** The strategy relies on local Parquet files and local ONNX models. A production deployment needs a strict CI/CD pipeline that packages the model artifact, the Rust binary, and the config into a sealed Docker container.

## Remediation Plan (Phase 7: Real-Money Readiness)
Before funding the account, complete the following:
1. Implement `KalshiVenueClient` according to the new design spec (`docs/kalshi-live-execution-design.md`).
2. Implement a background OMS polling task to detect order state drift.
3. Add robust reconnect and sequence tracking to the Kalshi WebSocket client.
4. Implement a "Cancel All on Startup / Shutdown" safety flag.
5. Create a secure vault integration for injecting Kalshi ECDSA keys.
