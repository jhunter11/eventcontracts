# Kalshi Live-Readiness Remediation Plan

**Source audit:** End-to-end audit conducted 2026-05-27 against `main` HEAD plus this session's uncommitted work (runtime-hot, KalshiVenueClient, OnnxQuoteStrategy, runner rewiring).
**Scope:** Single-venue (Kalshi) path from current state to "live with real capital on a small sleeve, unattended overnight."
**Excluded from scope:** Polymarket adapter, NATS / cross-process bus, container deployment, async-submit refactor, rolling-feature surface, Trade/Book strategy variants. These are tracked separately and explicitly deferred — see §[Out-of-Scope](#out-of-scope-deferred) for the reasoning.

---

## How to use this document

Each phase has:
- A **goal** — the operational capability gained when the phase is complete.
- A **prerequisite** list — phases earlier in the sequence that must be done first.
- One or more **tasks**, each spec'd in enough detail that a competent engineer (or a coding agent operating cold) can execute without making architectural decisions.

Each task contains:
- **Audit finding** with severity, linking back to the source audit.
- **Files** — exact paths to modify or create.
- **New API / types** — concrete signatures, including where to put them.
- **Implementation steps** — ordered.
- **Edge cases** — things you'll get wrong if you don't think about them.
- **Tests** — named test cases with the scenario each covers.
- **Verification** — how to prove the task is done.
- **Effort** — rough engineer-hours assuming familiarity with the codebase.

**Sequencing rule:** Do phases in order. Within a phase, tasks can sometimes parallelize, but the phase as a whole must be complete before moving on — the next phase depends on its capabilities. Skipping ahead breaks the implicit invariants the next phase relies on.

---

## Build sequencing rationale

The phases are sequenced by **what they unblock**, not by code-locality or convenience:

```
A. Truth-from-Venue Foundation
   └─ enables seeing reality (fills, audit chain, clean restarts)
        │
B. Risk Becomes Real ───────────┐
   └─ enables risk constraining │ (depends on knowing fills exist)
        │                       │
C. Operator Safety Net ──────────┘
   └─ enables emergency halt    (depends on knowing what to cancel)
        │
D. Live Capital Tracking
   └─ enables max-daily-loss + real PnL  (depends on fills + position update)
        │
E. Operational Visibility
   └─ enables unattended operation  (depends on everything emitting truth)
        │
F. Promotion Gate Closure
   └─ enables strategy churn without re-auditing  (depends on stable runtime)
```

Each phase opens the door for the next. If you do D before B, your ledger records meaningless numbers (positions are wrong). If you do E before A, your metrics show fictional state. If you do F before A-D, your parity tests prove the wrong behavior matches in two languages.

---

## Phase A: Truth-from-Venue Foundation

**Goal:** After this phase, the runner sees its own fills, advances OMS state past `Acked`, produces a real audit chain, and shuts down cleanly without orphaning orders on the venue.

**Prerequisites:** Working build of current `main` + Kalshi demo credentials.

**Why this phase first:** Every other safety mechanism in the codebase presumes the runner has *truth*. Today the runner is essentially blind once an order is acked. Every Phase B/C/D check is theater if Phase A isn't done.

**Capabilities unlocked when complete:**
- OMS state advances `Acked → PartiallyFilled → Filled` from real venue events.
- Cross-language audit chain has a real hash anchor (not `"0".repeat(64)`).
- WS reconnects automatically on transient network drops.
- Ctrl-C / SIGTERM cancels open orders before exit.

---

### A1: Implement real `canonical_sha256` and replace placeholders

**Audit finding:** §1.6 [HIGH]

**Goal:** Every `AuditStamp.canonical_sha256` field carries the actual SHA-256 of the canonical-JSON serialization of the audited payload. Replace every `"0".repeat(64)` placeholder in the codebase.

**Files to create:**
- (none new; extend existing crate)

**Files to modify:**
- `rust/crates/contracts/src/lib.rs` — add `canonical_sha256` helper module.
- `rust/crates/contracts/Cargo.toml` — add `sha2` dep.
- `rust/crates/kalshi/src/normalize.rs:66` — replace placeholder.
- `rust/crates/live-runner/src/main.rs` — replace at lines 443 (intent envelope wrap), 625 (tennis prediction event).
- `rust/crates/runner/src/lib.rs:717` — replace in `wrap_envelope`.
- `rust/crates/gateway/src/lib.rs:499` — test fixture: leave `"0".repeat(64)` here since it's a test stub, but assert it's distinguishable from real audit values.
- All other test files: keep test-only placeholders explicitly tagged.

**New API:**

```rust
// rust/crates/contracts/src/audit_hash.rs (new module)

use sha2::{Digest, Sha256};
use serde::Serialize;

/// Compute the canonical-JSON SHA-256 of a payload. Output is 64-character
/// lowercase hex, matching the contract's `canonical_sha256` field format.
///
/// Canonicalization rules (must match Python implementation byte-for-byte):
/// - Object keys sorted lexicographically at every level.
/// - No whitespace between tokens (no spaces after `:` or `,`).
/// - Decimals serialized as strings (already enforced by the contract types).
/// - UTF-8 encoding.
///
/// The returned string is suitable as a direct value for
/// `AuditStamp.canonical_sha256`.
pub fn canonical_sha256<T: Serialize>(value: &T) -> Result<String, ContractError> {
    let canonical = serde_json::to_string(value)
        .map_err(|e| ContractError::Json(e.to_string()))?;
    // serde_json defaults to compact (no spaces) but does NOT sort keys.
    // We round-trip through serde_json::Value with sort_keys to get the
    // canonical form. This is the same approach the Python adapter uses.
    let value: serde_json::Value = serde_json::from_str(&canonical)?;
    let sorted = serialize_sorted(&value);
    let mut hasher = Sha256::new();
    hasher.update(sorted.as_bytes());
    Ok(hex_lower(&hasher.finalize()))
}

fn serialize_sorted(value: &serde_json::Value) -> String { /* recursive impl */ }
fn hex_lower(bytes: &[u8]) -> String { /* lowercase hex */ }
```

**Implementation steps:**

1. Add `sha2 = "0.10"` to `contracts/Cargo.toml`.
2. Create `rust/crates/contracts/src/audit_hash.rs` with the helper above and a recursive canonical serializer.
3. Re-export from `lib.rs`: `pub use audit_hash::canonical_sha256;`.
4. Add a parity test: produce known input → known SHA. The Python side (in `python/src/eventcontracts/audit.py` or similar) should be able to produce byte-identical output. Confirm with at least three fixtures covering: nested objects, decimal-string values, RFC3339 timestamps.
5. Walk each placeholder site and replace. For each, the payload being audited is the parent record minus the `audit` field — i.e., you hash the record's content and stamp the hash into its audit field. Pattern:
   ```rust
   let mut audit = AuditStamp {
       canonical_sha256: String::new(), // placeholder
       /* other fields */
   };
   let mut record = NormalizedEventRecord {
       audit: audit.clone(),
       /* payload fields */
   };
   record.audit.canonical_sha256 = canonical_sha256(&record_payload_without_audit)?;
   ```
   
   The "without audit" trick matters because hashing the record-with-audit would create a chicken-and-egg cycle. Helper:
   ```rust
   pub fn stamp_record<T: Serialize>(
       payload: &T,
       audit: &mut AuditStamp,
   ) -> Result<(), ContractError> {
       audit.canonical_sha256 = canonical_sha256(payload)?;
       Ok(())
   }
   ```

**Edge cases:**
- Maps with non-string keys: serde_json forbids these; if encountered, panic or convert to strings. Document the constraint.
- Floating-point numbers in payloads: the contracts use decimal-strings, so this shouldn't occur. Add an assertion that fails the test if any f64 leaks into a serialized payload.
- Unicode in market IDs: must serialize in UTF-8, no escaping; verify with a fixture containing non-ASCII chars.

**Tests:**
- `canonical_sha256_matches_python_reference_for_simple_payload` — three hard-coded `(input, expected_hash)` pairs computed offline.
- `canonical_sha256_is_order_insensitive_for_object_keys` — same logical object with keys in different declaration orders produces the same hash.
- `canonical_sha256_distinguishes_string_vs_number_decimals` — `{"x": "1"}` and `{"x": 1}` hash differently.
- `replace_all_placeholders_audit` — a workspace-wide grep test (in `tests/no_placeholder_audit.rs`) that asserts no live-path file contains `"0".repeat(64)` outside `#[cfg(test)]` blocks.

**Verification:**
- Run the live-runner against a recorded fixture. Pipe the emitted intent JSON through `sha256sum` and compare against the `canonical_sha256` field — they must match.
- Cross-language: emit a normalized event from Python and from Rust with identical content. Assert the two `canonical_sha256` values are identical.

**Effort:** 3-4 hours including the cross-language fixture.

---

### A2: Subscribe to authenticated `fill` and `order` channels; ingest into the live-runner

**Audit finding:** §1.1 [CRITICAL]

**Goal:** The runner subscribes to Kalshi's authenticated `fill` and `order` WS channels in addition to public market data. Incoming fills route into `gateway.apply_fill()` so the OMS state machine advances.

**Files to modify:**
- `rust/crates/kalshi/src/ws.rs` — extend subscribe call site documentation; the existing `subscribe(channels, market_tickers)` works for these channels too.
- `rust/crates/kalshi/src/normalize.rs` — extend `normalize_ws_payload` to handle `"fill"` and `"order"` message types.
- `rust/crates/live-runner/src/main.rs:187` — add channels to subscribe.
- `rust/crates/live-runner/src/main.rs` (main loop) — route projected `HotEvent::OwnFill` / `HotEvent::OwnOrderUpdate` into the gateway.

**New API:**

Inside `normalize.rs`, extend the channel-kind mapping:

```rust
let kind = match env.msg_type.as_str() {
    "ticker" => "quote",
    "trade" => "trade",
    "orderbook_snapshot" | "orderbook_delta" => "book",
    "lifecycle" => "lifecycle",
    "fill" => "own_fill",
    "order" => "own_order_update",  // status field distinguishes reject
    "subscribed" | "ok" | "error" => return Err(NormalizeError::Ignored("control msg")),
    other => return Err(NormalizeError::UnsupportedChannel(other.to_string())),
};
```

Then build the appropriate payload JSON. The shapes (from Kalshi docs) are:

```jsonc
// fill channel message
{
    "type": "fill",
    "msg": {
        "trade_id": "...",
        "order_id": "kalshi-side venue order id",
        "market_ticker": "...",
        "side": "yes" | "no",
        "action": "buy" | "sell",
        "yes_price": <integer cents>,  // mutually exclusive with no_price
        "no_price": <integer cents>,
        "count": <integer>,
        "ts": <epoch_ms>
    }
}

// order channel message
{
    "type": "order",
    "msg": {
        "order_id": "...",
        "client_order_id": "...",
        "market_ticker": "...",
        "status": "resting" | "canceled" | "executed" | "rejected" | "expired",
        "ts": <epoch_ms>,
        "reason": "..."  // optional, present on reject
    }
}
```

The normalizer translates these to the contract's `own_fill` / `own_order_update` payload shapes that `runtime-hot::project_event` already expects (see `runtime-hot/src/project.rs:72-87` for `OwnFillPayload`):

```rust
struct OwnFillPayload {
    fill_id: String,           // ← trade_id
    instrument: String,        // ← "kalshi:" + market_ticker
    client_order_id: String,   // ← need lookup; see below
    price: String,             // ← cents / 100, e.g. "0.42"
    quantity: String,          // ← count as integer-string
    fee: String,               // ← computed if needed, else "0"
}
```

**The `client_order_id` lookup problem:** Kalshi's fill messages carry `order_id` (venue id), not `client_order_id`. To translate, you need the reverse of the `KalshiVenueClient.venue_ids` cache. Two options:

1. **Reverse-cache:** When `KalshiVenueClient::submit` populates `venue_ids[client_id] = venue_id`, also populate a reverse map `client_ids[venue_id] = client_id` in a shared state accessible to the normalizer.
2. **Fill carries both:** Kalshi's order channel (not fill) carries both ids — subscribe to it as well, build a venue→client map, then enrich fill events.

Option 1 is simpler. Implement:

```rust
// rust/crates/kalshi/src/venue_client.rs
pub struct KalshiVenueClient {
    rest: Arc<KalshiRest>,
    handle: Handle,
    /// Outbound cache populated on submit.
    venue_ids: HashMap<String, String>,
    /// Inbound cache for matching fill messages back to our orders. Shared.
    pub venue_to_client: Arc<RwLock<HashMap<String, String>>>,
}
```

The `Arc<RwLock<HashMap<...>>>` is cloned into the live-runner's normalizer-adjacent code so it can resolve `venue_order_id → client_order_id` at fill time. The lock is held for microseconds (a single map lookup); contention is not a concern at WS message rate.

**Implementation steps:**

1. Update [`live-runner/src/main.rs:187`](rust/crates/live-runner/src/main.rs:187):
   ```rust
   ws.subscribe(
       &["ticker", "trade", "orderbook_delta", "fill", "order"],
       &ticker_refs,
   ).await?;
   ```
   Note: `fill` and `order` channels don't require `market_tickers` (they're account-scoped), but Kalshi accepts the filter.

2. Add `venue_to_client: Arc<RwLock<HashMap<String, String>>>` field to `KalshiVenueClient`. Initialize empty. On `submit` success, take the write-lock and insert `(venue_id, client_id)`.

3. Extend `normalize.rs::build_payload_json` to handle `"own_fill"` and `"own_order_update"` kinds. Add a `&venue_to_client` parameter or thread it through a context struct.

4. In the live-runner main loop, after `project_event`, branch on `HotEvent::OwnFill`:
   ```rust
   match hot_event {
       HotEvent::OwnFill(fill) => {
           let oms_fill = Fill {
               fill_id: fill.fill_id.to_string(),
               client_order_id: fill.client_order_id.to_string(),
               price: fill.price.to_decimal_string(),
               quantity: format!("{}", fill.quantity.raw()),
               fee: fill.fee.to_decimal_string(),
               trade_ts: rfc3339_now(),
           };
           match gateway.apply_fill(oms_fill) {
               Ok(applied) => {
                   if applied { metrics.own_fills_applied += 1; }
                   else { metrics.own_fills_duplicate += 1; }
               }
               Err(e) => {
                   metrics.own_fill_errors += 1;
                   eprintln!("own_fill error: {e}");
               }
           }
       }
       HotEvent::OwnOrderUpdate(update) | HotEvent::OwnOrderReject(update) => {
           // Map state string to OrderState and transition the OMS. See A2-b.
       }
       other => { /* existing strategy path */ }
   }
   ```

5. Add metrics counters: `own_fills_applied`, `own_fills_duplicate`, `own_fill_errors`, `own_order_updates`.

**Sub-task A2-b: own-order-update routing**

The `"order"` channel messages report order state transitions (acked, canceled, rejected, expired). Map them to OMS transitions:

```rust
fn map_order_status_to_state(status: &str) -> Option<OrderState> {
    match status {
        "resting" => Some(OrderState::Acked),
        "canceled" => Some(OrderState::Canceled),
        "executed" => Some(OrderState::Filled),  // also: fills will arrive on fill channel; this is the umbrella state
        "rejected" => Some(OrderState::Rejected),
        "expired" => Some(OrderState::Expired),
        _ => None,
    }
}
```

Apply via `gateway.oms.transition(&client_order_id, mapped_state, &now, reason)`. Note: this may collide with state already advanced by `apply_fill` — if so, swallow `OmsError::IllegalTransition` quietly (the fill path takes precedence). A clean design wraps both with idempotency: only transition if the current state is strictly behind.

**Edge cases:**
- Fill arrives before the order ack (race in Kalshi's pipelines). Solution: if `apply_fill` returns `UnknownOrder`, buffer the fill in a small bounded retry queue and re-apply after the next order channel update arrives. Cap the buffer at 100 entries; drop oldest with a metric.
- Duplicate fill messages — already handled by `InMemoryOms.applied_fills` dedupe at `oms/src/lib.rs:235`.
- The `order_id` in a fill message has no corresponding `client_order_id` in our cache (e.g., order placed by a different process). Drop the fill with a `unknown_venue_order_id` metric increment. Do not panic.
- Authentication failure on the fill/order channel. Kalshi sometimes returns 401 or silently drops the subscription. Solution: watchdog (A3) detects the silence and reconnects.

**Tests:**
- `normalize_kalshi_fill_message_produces_own_fill_event` — feeds a recorded fill JSON, asserts the normalized record has `event_kind = "own_fill"` and the projected `HotEvent::OwnFill` has correct fields.
- `normalize_kalshi_order_message_resting_status_maps_to_acked` — same for order channel.
- `apply_fill_on_unknown_client_order_id_buffers_and_replays` — race-condition fixture: fill arrives before order ack; assert it's buffered then applied.
- `apply_fill_with_unknown_venue_order_id_drops_with_metric` — fill for an order we didn't place; assert metric increment and no OMS error.
- `duplicate_fill_arriving_twice_applies_once` — already covered in OMS but add an end-to-end version.

**Verification:**
- Manual: with Kalshi demo creds, place a small order; observe `own_fills_applied` increment in the metrics report and `OmsError::Filled` state on the order via a debug dump.
- Determine the OMS state matches venue UI for at least 10 manual orders.

**Effort:** 6-8 hours including the cross-process cache plumbing and tests.

---

### A3: WS reconnect with exponential backoff and re-subscribe

**Audit finding:** §1.2 [CRITICAL], §1.4 [HIGH]

**Goal:** The WS receive loop tolerates transient disconnects, half-open TCP, and intermittent Kalshi outages. On disconnect, reconnect with exponential backoff, re-subscribe to all previously subscribed market_tickers and channels, and resume.

**Files to modify:**
- `rust/crates/kalshi/src/ws.rs` — refactor `KalshiWsClient` to track subscription state and add reconnect logic.
- `rust/crates/live-runner/src/main.rs` — adapt main loop to use the new resilient client.

**New API:**

```rust
// rust/crates/kalshi/src/ws.rs

pub struct ResilientWsClient {
    inner: KalshiWsClient,
    /// State to re-subscribe with after reconnect.
    subscriptions: Vec<SubscribeParams>,
    /// Backoff for the *next* reconnect attempt.
    backoff: BackoffState,
    /// Last successful message receipt; watchdog uses this.
    last_recv_at: Instant,
    /// Configured watchdog timeout. Default 20s.
    idle_timeout: Duration,
}

struct BackoffState {
    next_delay: Duration,  // current delay, doubles on each failure
    max_delay: Duration,   // capped at 30s
    consecutive_failures: u32,
}

impl ResilientWsClient {
    pub fn new(ws_url: String, auth: KalshiAuth) -> Self { ... }

    pub async fn connect_and_subscribe(
        &mut self,
        channels: &[&str],
        market_tickers: &[&str],
    ) -> Result<(), WsError> {
        // Connect and remember subscription state.
    }

    /// Drive the receive loop; reconnects automatically on transient errors.
    /// Returns Err only when reconnect attempts have exceeded the configured
    /// budget (default: unlimited unless `with_max_reconnects` set).
    pub async fn next_envelope_resilient(
        &mut self,
    ) -> Result<KalshiWsEnvelope, WsError> { ... }

    /// Force-drop the current stream and trigger reconnect on next call.
    /// Used by the watchdog when idle_timeout fires.
    pub fn force_reconnect(&mut self) { ... }
}
```

**Implementation steps:**

1. Add `ResilientWsClient` wrapper. Internal `KalshiWsClient` stays for compatibility.
2. Store every `subscribe()` call's params in `subscriptions: Vec<SubscribeParams>`. On reconnect, replay them.
3. Implement `next_envelope_resilient`:
   ```rust
   pub async fn next_envelope_resilient(&mut self) -> Result<KalshiWsEnvelope, WsError> {
       loop {
           match self.inner.next_envelope().await {
               Ok(Some(env)) => {
                   self.last_recv_at = Instant::now();
                   self.backoff.reset();
                   return Ok(env);
               }
               Ok(None) => continue,
               Err(e) if Self::is_transient(&e) => {
                   eprintln!("ws transient error: {e}; reconnecting...");
                   self.reconnect_with_backoff().await?;
               }
               Err(e) => return Err(e),  // unrecoverable; let caller decide
           }
       }
   }

   async fn reconnect_with_backoff(&mut self) -> Result<(), WsError> {
       let delay = self.backoff.next();  // 250ms, 500ms, 1s, 2s, ..., capped at 30s
       let jittered = jitter_uniform(delay, 0.1);  // ±10%
       tokio::time::sleep(jittered).await;
       self.inner.connect().await?;
       for params in &self.subscriptions {
           self.inner.subscribe_raw(params).await?;
       }
       self.last_recv_at = Instant::now();
       Ok(())
   }
   ```

4. Add a watchdog. Spawn a tokio task that ticks every 5s and calls `force_reconnect` if `now - last_recv_at > idle_timeout`. The watchdog only flips a flag; the receive loop checks the flag at the top of each iteration and explicitly reconnects.

5. Pings: in the inner `next_envelope` loop, after handling each ping with a pong, also update `last_recv_at` — pings count as liveness signal.

6. Classify errors as `is_transient`:
   ```rust
   fn is_transient(e: &WsError) -> bool {
       matches!(
           e,
           WsError::Closed
               | WsError::Tungstenite(tokio_tungstenite::tungstenite::Error::Io(_))
               | WsError::Tungstenite(tokio_tungstenite::tungstenite::Error::ConnectionClosed)
               | WsError::Tungstenite(tokio_tungstenite::tungstenite::Error::AlreadyClosed)
       )
   }
   ```
   Non-transient (auth failures, malformed responses) should NOT trigger reconnect; bubble them up.

7. Add metrics: `ws_reconnects_total`, `ws_reconnect_failures`, `ws_idle_timeouts`, `ws_consecutive_failures_high_water`.

**Edge cases:**
- Reconnect itself fails (Kalshi extended outage). After N consecutive failures (configurable, default 20 ≈ 10 minutes given backoff), surface a fatal error. Do not loop forever silently.
- Stale subscription state: if the runner has added/removed tickers since the last subscribe (e.g., after a manual update), the replay would resubscribe to stale ones. For v1, just re-subscribe everything; the live-runner doesn't mutate subscriptions mid-run.
- Auth signature has a timestamp. If reconnect happens at the boundary of a timestamp window (Kalshi requires `now - ts_ms < 10s`), regenerate via `auth.sign()` at reconnect time (not at original connect time). The current `KalshiWsClient::connect` already does this.
- WS pong handler must update `last_recv_at`. Currently the loop continues on pong without updating; explicitly add `self.last_recv_at = Instant::now();` in the pong branch.

**Tests:**
- `resilient_reconnects_after_close` — start a mock WS server that closes the connection after the first message; assert the client reconnects, re-subscribes, and receives a second message.
- `resilient_backoff_doubles_then_caps` — fixture that fails connect N times; assert delay sequence is `[250ms, 500ms, 1s, 2s, 4s, 8s, 16s, 30s, 30s, ...]` with jitter tolerance.
- `resilient_resubscribes_to_all_market_tickers` — start with subscriptions to `[A, B, C]`; trigger reconnect; assert the mock server received the same subscribe payload.
- `resilient_surfaces_auth_error_without_reconnect_loop` — fixture that returns 401 on connect; assert error bubbles up after exactly one attempt.
- `resilient_watchdog_force_reconnects_on_idle_timeout` — mock server that sends a message, then goes silent; assert reconnect fires after `idle_timeout`.

**Verification:**
- Manual: run against Kalshi demo, manually kill the TCP connection (block port via firewall briefly); observe reconnect log line and continued message flow.
- Stress: against a recorded WS dump with intentional gaps inserted, ensure the client recovers and message counts match the input.

**Effort:** 8-10 hours including a mock WS server for tests.

---

### A4: Shutdown handler with cancel-on-disconnect

**Audit finding:** §4.7 [HIGH]

**Goal:** On Ctrl-C, SIGTERM, panic, or any clean exit path, the runner cancels every open order on Kalshi before exiting. No orphaned orders.

**Files to modify:**
- `rust/crates/kalshi/src/rest.rs` — add `cancel_all_orders` REST call.
- `rust/crates/kalshi/src/venue_client.rs` — add `cancel_all` method.
- `rust/crates/live-runner/src/main.rs` — add signal handler and graceful shutdown sequence.

**New API:**

```rust
// rust/crates/kalshi/src/rest.rs
impl KalshiRest {
    /// `DELETE /portfolio/orders` — bulk cancel.
    /// Kalshi accepts a `ticker` query param to scope; omit for cross-market cancel.
    pub async fn cancel_all_orders(
        &self,
        ticker: Option<&str>,
    ) -> Result<BulkCancelResponse, RestError> { ... }
}

#[derive(Debug, Clone, Deserialize)]
pub struct BulkCancelResponse {
    #[serde(default)]
    pub canceled_orders: Vec<String>,
    #[serde(default)]
    pub message: Option<String>,
}

// rust/crates/kalshi/src/venue_client.rs
impl KalshiVenueClient {
    /// Cancel every order this client has placed. Returns the list of
    /// successfully canceled `client_order_id`s, plus failures.
    pub fn cancel_all(&mut self) -> Result<CancelAllReport, GatewayError> { ... }
}

pub struct CancelAllReport {
    pub canceled: Vec<String>,    // client_order_ids
    pub failed: Vec<(String, GatewayError)>,
}
```

**Implementation steps:**

1. Implement `KalshiRest::cancel_all_orders` mirroring the single-cancel pattern in `cancel_order`. Path: `/portfolio/orders`. Method: `DELETE`. Sign via `auth.sign("DELETE", url.path())`.

2. `KalshiVenueClient::cancel_all` iterates over `self.venue_ids`, calling `KalshiRest::cancel_order(venue_id)` for each. Collect results. Alternative: use the bulk-cancel endpoint if Kalshi supports it; check API docs and prefer one-shot for atomicity.

3. Add a shutdown coordinator in `live-runner`:
   ```rust
   #[tokio::main]
   async fn main() -> Result<(), Box<dyn Error>> {
       // ... existing setup ...

       // Spawn the main strategy loop in a task so we can race it with signals.
       let strategy_task = tokio::spawn(async move {
           run_strategy_loop(/* args */).await
       });

       let signal_task = tokio::spawn(async move {
           tokio::signal::ctrl_c().await.ok();
           eprintln!("Ctrl-C received; initiating shutdown");
       });

       tokio::select! {
           res = strategy_task => { /* normal completion */ }
           _ = signal_task => { /* signal-triggered shutdown */ }
       }

       // Shutdown sequence — always runs.
       shutdown_cleanup(/* gateway, venue, oms */).await;
       Ok(())
   }

   async fn shutdown_cleanup(/* ... */) {
       eprintln!("shutdown: cancelling all open orders...");
       let report = venue.cancel_all();
       match report {
           Ok(r) => eprintln!("shutdown: canceled {} orders, {} failures",
                              r.canceled.len(), r.failed.len()),
           Err(e) => eprintln!("shutdown: cancel_all error: {e}; orders may remain open!"),
       }
       // Flush WS, drain gateway, write final report.
   }
   ```

4. On panic: install a panic hook that calls `cancel_all` synchronously before terminating. Tricky in async contexts; use a `std::sync::Mutex<Option<Arc<KalshiVenueClient>>>` global that the hook reads:
   ```rust
   static SHUTDOWN_CLIENT: Mutex<Option<Arc<KalshiVenueClient>>> = Mutex::new(None);

   std::panic::set_hook(Box::new(|info| {
       eprintln!("PANIC: {info}");
       if let Ok(guard) = SHUTDOWN_CLIENT.lock() {
           if let Some(client) = guard.as_ref() {
               // synchronous cancel — block on the existing tokio handle
               // best-effort; if the runtime is already torn down, this no-ops
               let _ = client.cancel_all_blocking();
           }
       }
   }));
   ```
   Note: this requires `KalshiVenueClient` to expose a sync `cancel_all_blocking` that doesn't rely on `Handle::current()` (since the runtime may be dead). Build a dedicated thread + runtime for this case.

5. Add a `--no-cancel-on-shutdown` CLI flag for diagnostic runs where you want to leave orders alone.

**Edge cases:**
- Network is the reason for shutdown (so cancels will fail). Surface but don't loop. Log the orphaned `client_order_id`s prominently so the operator can cancel manually via the Kalshi UI.
- Cancel-all races with a concurrent fill arriving on WS. Solution: cancel is idempotent at the venue — the venue ignores cancel on an already-filled order. We may see a `cancel_order` REST 404; treat that as success.
- Panic during cancel-all itself. The panic hook should not panic again. Wrap in `catch_unwind`.
- Multiple shutdown triggers in flight (e.g., Ctrl-C twice). Use `AtomicBool` to ensure cleanup runs once.

**Tests:**
- `cancel_all_calls_venue_for_every_cached_order` — mock REST server; place 5 orders; trigger cancel_all; assert 5 DELETE calls.
- `cancel_all_continues_on_individual_failure` — mock returns 500 for one order; assert other 4 still cancel and the report lists the failure.
- `panic_hook_triggers_cancel` — induce a panic in a test thread; assert cancel-all was called via mock.
- `ctrl_c_drains_gateway_then_cancels` — fixture with pending intents; signal Ctrl-C; assert gateway drained intents, then canceled.

**Verification:**
- Manual: with demo creds, place an order, then Ctrl-C. Check the Kalshi UI confirms the order is canceled.
- Stress: induce a panic via `panic!()` injected mid-run; verify cleanup ran.

**Effort:** 6-8 hours.

---

## Phase A Acceptance Criteria

Before declaring Phase A done, the following must all be true:

- [ ] `cargo test --workspace` passes.
- [ ] Workspace grep finds **zero** occurrences of `"0".repeat(64)` outside `#[cfg(test)]` blocks.
- [ ] A live demo run produces real, non-placeholder SHA-256 in every audit stamp.
- [ ] An induced WS disconnect (firewall block 30s) results in automatic reconnect and continued message flow.
- [ ] An induced idle freeze (60s with no incoming messages) triggers watchdog reconnect.
- [ ] Ctrl-C during a live run cancels all open orders on the venue, verified via Kalshi UI.
- [ ] An induced panic (debug `panic!` injection) also cancels all open orders.
- [ ] One real fill (size=1) on demo advances the OMS order state from `Acked` to `Filled` automatically, verified in the metrics report.

---

## Phase B: Risk Becomes Real

**Goal:** Risk checks actually constrain. Position tracking is exact, gateway-side risk sees the same state as runner-side risk, OMS arithmetic is deterministic.

**Prerequisites:** Phase A complete (specifically: fills are arriving and `apply_fill` is being called).

**Capabilities unlocked:**
- `SleeveState.positions` reflects actual venue position post-fill.
- Gateway-side risk re-check is meaningful (not always-pass).
- OMS state can't drift due to f64 parsing failures.
- Venue post-ack rejects are handled cleanly.

---

### B1: Migrate OMS to exact decimal arithmetic

**Audit finding:** §4.1 [CRITICAL], §4.2 [CRITICAL]

**Goal:** Replace all `f64`-based parsing and arithmetic in the OMS with `rust_decimal::Decimal`. Eliminate `unwrap_or(0.0)`. Use exact comparison for fill-completion and overfill checks.

**Files to modify:**
- `rust/crates/oms/Cargo.toml` — add `rust_decimal = "1.36"`.
- `rust/crates/oms/src/lib.rs` — rewrite `parse_decimal`, `format_decimal`, the `apply_fill` body, the `Order::remaining` method.

**New API:**

```rust
// rust/crates/oms/src/lib.rs

use rust_decimal::Decimal;
use std::str::FromStr;

/// Strict decimal parse. Returns `OmsError::Contract(ContractError::InvalidDecimal)`
/// on any failure. NO silent zero fallback.
fn parse_decimal_strict(field: &'static str, s: &str) -> Result<Decimal, OmsError> {
    Decimal::from_str(s).map_err(|_| {
        OmsError::Contract(ContractError::InvalidDecimal(field))
    })
}

fn format_decimal(d: Decimal) -> String {
    // Strips trailing zeros but preserves at least one digit after the point
    // when present. "10" not "10.0"; "10.5" not "10.50"; "0" not ".0".
    d.normalize().to_string()
}
```

**Implementation steps:**

1. Add the dep. Pin to a tested version.
2. Replace `parse_decimal(s: &str) -> f64` with `parse_decimal_strict(field, s) -> Result<Decimal, OmsError>`.
3. Update `Order::remaining` to return `Decimal`:
   ```rust
   pub fn remaining(&self) -> Result<Decimal, OmsError> {
       let total = parse_decimal_strict("quantity", &self.quantity)?;
       let filled = parse_decimal_strict("filled_quantity", &self.filled_quantity)?;
       Ok(total - filled)
   }
   ```
4. Rewrite `apply_fill` to use `Decimal` end-to-end:
   ```rust
   let fill_qty = parse_decimal_strict("quantity", &fill.quantity)?;
   let total = parse_decimal_strict("quantity", &order.quantity)?;
   let already_filled = parse_decimal_strict("filled_quantity", &order.filled_quantity)?;
   let remaining = total - already_filled;
   if fill_qty > remaining {  // exact comparison, no epsilon
       return Err(OmsError::Overfill { /* ... */ });
   }
   let new_filled = already_filled + fill_qty;
   order.filled_quantity = format_decimal(new_filled);
   let next_state = if new_filled == total {
       OrderState::Filled
   } else {
       OrderState::PartiallyFilled
   };
   ```
5. Remove the `1e-9` epsilon comparisons.
6. Update `apply_fill` to also recompute `weighted_avg_price` for downstream position tracking (B2 will read this). Add `Order::avg_fill_price: Decimal` field if useful for the ledger.

**Edge cases:**
- Decimal parsing of scientific notation: `rust_decimal::Decimal::from_str` accepts `"1e2"`. Decide if you accept it. The contract validators reject scientific notation; reject here too for consistency.
- Negative quantities: `rust_decimal` accepts them. The OMS should reject any negative quantity field as a contract violation.
- Trailing whitespace: trim before parsing? No — strict mode means input is rejected. Document the strict-format invariant.
- Locale-dependent decimal separators: ensure `from_str` is locale-independent (it is, by default in `rust_decimal`).

**Tests:**
- `oms_parse_decimal_rejects_empty_string` — currently maps to 0; new behavior is `Err`.
- `oms_parse_decimal_rejects_garbage` — `"abc"` and `"1.2.3"` both error.
- `oms_overfill_detected_at_exact_boundary` — fill_qty exactly remaining+1 minimal unit; assert overfill.
- `oms_fill_completes_at_exact_total` — fill_qty exactly equals total; assert `Filled` state.
- `oms_partial_fill_accumulates_exactly` — sequence of three partial fills summing exactly to total; assert `Filled`.
- `oms_high_precision_quantities_preserved` — 0.1234 + 0.5678 + 0.3088 = 1.0 exactly; assert `Filled`.

**Verification:**
- All existing OMS tests pass with the new arithmetic.
- A property test (use `proptest`): for any `(total, fills)` partition summing exactly to total, the OMS reaches `Filled`.
- Replay a recorded production fill stream from Python's paper trader through the Rust OMS; quantities must match byte-for-byte.

**Effort:** 4-6 hours.

---

### B2: Update `SleeveState.positions` from fills

**Audit finding:** §4.5 [CRITICAL], §4.3 [HIGH]

**Goal:** After every fill, `SleeveState.positions[instrument]` reflects the actual position derived from OMS state. Position arithmetic is exact (using `Decimal` or fixed-point integers).

**Files to modify:**
- `rust/crates/risk/src/lib.rs` — change `Position` fields to `Decimal` types.
- `rust/crates/gateway/src/lib.rs` — `apply_fill` recomputes positions.

**New API:**

```rust
// rust/crates/risk/src/lib.rs
use rust_decimal::Decimal;

#[derive(Clone, Debug, Default, PartialEq)]
pub struct Position {
    pub quantity: Decimal,
    pub avg_price: Decimal,
    /// True if this position was opened by Buy YES (long YES contracts).
    /// False if Buy NO (long NO contracts). Sell-side positions track as
    /// negative quantity on the same side.
    pub side: OutcomeSide,
}

#[derive(Clone, Debug, Default)]
pub struct SleeveState {
    pub open_orders: u32,
    pub positions: HashMap<String, Position>,
    pub daily_realized_loss: Decimal,
    pub kill_switch_engaged: bool,
    pub market_data_age_secs: HashMap<String, u32>,
}
```

**Implementation steps:**

1. Change `Position` to use `Decimal`. Adjust all references.
2. Remove `fixed_from_f64` from `risk/src/lib.rs:265-267`. Replace direct `Decimal` arithmetic.
3. In `gateway::DryRunGateway::apply_fill`:
   ```rust
   pub fn apply_fill(&mut self, fill: Fill) -> Result<bool, GatewayError> {
       let applied = self.oms.apply_fill(fill.clone())?;
       if !applied {
           return Ok(false);
       }

       // Recompute open orders count (existing behavior).
       self.sleeve_state.open_orders = self.oms.open_orders().count() as u32;

       // NEW: update positions from this fill.
       let order = self
           .oms
           .get(&fill.client_order_id)
           .expect("order exists by virtue of apply_fill returning Ok");
       let instrument = order.instrument_id.clone();
       let fill_qty = parse_decimal_strict("quantity", &fill.quantity)
           .map_err(|e| GatewayError::Oms(e))?;
       let fill_price = parse_decimal_strict("price", &fill.price)
           .map_err(|e| GatewayError::Oms(e))?;

       let pos = self.sleeve_state.positions.entry(instrument).or_default();
       let signed_qty = match order.side {
           Side::Buy => fill_qty,
           Side::Sell => -fill_qty,
       };
       let old_qty = pos.quantity;
       let new_qty = old_qty + signed_qty;

       // VWAP update: only when adding to position. If reducing or flipping,
       // realized PnL crystallizes (handled by D2).
       if (old_qty.signum() == signed_qty.signum()) || old_qty.is_zero() {
           let old_notional = old_qty * pos.avg_price;
           let new_notional = old_notional + (signed_qty * fill_price);
           pos.avg_price = if new_qty.is_zero() { Decimal::ZERO } else { new_notional / new_qty };
       }
       pos.quantity = new_qty;
       // side: derived from order.outcome_side at first fill; subsequent fills
       // on the same instrument inherit.

       Ok(true)
   }
   ```
4. Update every test fixture that constructs `Position { quantity: f64, avg_price: f64 }` to use `Decimal::from_str_exact`.

**Edge cases:**
- Position fully closed (`new_qty.is_zero()`): zero out `avg_price` to avoid stale state.
- Position flipped (long → short via large sell): handle the cross-zero case. Above code only updates VWAP on adds; flips reset `avg_price` to the new entry price for the residual.
- Multiple instruments fill in the same tick: each updates independently; no cross-instrument coupling.
- Fill with `quantity = "0"`: should never happen; if it does, log and skip without state change.

**Tests:**
- `position_updates_on_first_fill_sets_avg_price` — fill of 5 @ 0.42 → `position { quantity: 5, avg_price: 0.42 }`.
- `position_vwap_on_second_add_at_different_price` — first 5 @ 0.40, then 5 @ 0.50 → `avg_price: 0.45`.
- `position_zeros_out_on_full_close` — buy 10, sell 10 → `quantity: 0, avg_price: 0`.
- `position_flip_resets_avg_to_new_entry` — buy 5 @ 0.40, sell 10 @ 0.50 → `quantity: -5, avg_price: 0.50`.

**Verification:**
- End-to-end: place an order on demo, observe fill, dump `SleeveState.positions` to JSON, compare with Kalshi's `GET /portfolio/positions` response.

**Effort:** 4-6 hours.

---

### B3: Plumb runner sleeve state into gateway

**Audit finding:** §4.4 [HIGH]

**Goal:** Gateway-side risk re-check evaluates against the same `SleeveState` the runner used to approve the intent. Today the gateway's `sleeve_state` is initialized empty and never receives the runner's state — so gateway risk fires only on `OrderNotionalExceeded` and `OpenOrdersExceeded`.

**Files to modify:**
- `rust/crates/contracts/src/lib.rs` — extend `IntentEnvelopeRecord` with a state snapshot or a hash reference.
- `rust/crates/gateway/src/lib.rs` — accept and use the snapshot in `process_one`.
- `rust/crates/runner/src/lib.rs` and `rust/crates/live-runner/src/main.rs` — populate the snapshot at intent creation.

**Decision:** Two design options. Pick one explicitly.

- **Option 1 — Embed snapshot in `IntentEnvelopeRecord`.** Adds bytes to every intent (positions map, daily loss). Simple. Stale by definition (snapshot at emit time, gateway processes microseconds later — but state may have changed via a fill).
- **Option 2 — Shared `SleeveStateActor`.** Both runner and gateway hold an `Arc<RwLock<SleeveState>>`. The actor is the source of truth; both read/write through it. Cleaner architecturally but requires reworking `&mut SleeveState` plumbing across both crates.

**Recommendation:** **Option 2** — Shared actor. Long-term correct. The audit identified the deeper problem (two truth sources). Fix it once.

**New API:**

```rust
// rust/crates/risk/src/lib.rs (or a new sleeve_state crate)

use parking_lot::RwLock;
use std::sync::Arc;

#[derive(Clone)]
pub struct SleeveStateHandle {
    inner: Arc<RwLock<SleeveState>>,
}

impl SleeveStateHandle {
    pub fn new(initial: SleeveState) -> Self { /* */ }

    pub fn read<R>(&self, f: impl FnOnce(&SleeveState) -> R) -> R {
        f(&self.inner.read())
    }

    pub fn write<R>(&self, f: impl FnOnce(&mut SleeveState) -> R) -> R {
        f(&mut self.inner.write())
    }

    /// Snapshot (clone) the state. Use for risk evaluation where you want
    /// a consistent read without holding the lock across the evaluation.
    pub fn snapshot(&self) -> SleeveState {
        self.inner.read().clone()
    }
}
```

**Implementation steps:**

1. Add `parking_lot = "0.12"` to risk crate.
2. Introduce `SleeveStateHandle`. Update `RiskGate::evaluate` to accept `&SleeveState` (unchanged signature; callers pass a snapshot from the handle).
3. Update `DryRunGateway` to hold `SleeveStateHandle` instead of `SleeveState`:
   ```rust
   pub struct DryRunGateway<C: VenueClient> {
       pub scheduler: PriorityScheduler,
       pub idempotency: IdempotencyStore,
       pub oms: InMemoryOms,
       pub risk: RiskGate,
       pub state: SleeveStateHandle,  // changed
       pub venue: C,
       /* ... */
   }
   ```
4. Update `process_one` to snapshot before evaluating:
   ```rust
   let snap = self.state.snapshot();
   match self.risk.evaluate(&snap, &intent_snap) {
       RiskDecision::Approved => { /* proceed */ }
       /* ... */
   }
   ```
   Mutations (after ack, after fill) acquire the write lock briefly.
5. In `live-runner::main`, create the `SleeveStateHandle` once and share it with the gateway and the runner's risk path. Replace `&mut sleeve_state` with `&state_handle`.
6. Update `SleeveRunner` in `runner/src/lib.rs` similarly. Its existing `&mut SleeveState` becomes `&SleeveStateHandle`.

**Edge cases:**
- Lock contention is minimal (single writer per state mutation) but document the invariant: state mutations must be brief. No I/O while holding the write lock.
- Snapshot-then-evaluate is racy: a fill could arrive between snapshot and decision. Document this as acceptable — risk uses a recent-but-not-current snapshot; the gateway will see any new fill on the next intent.
- Deadlock potential if both runner and gateway take the lock recursively: forbid recursive locking. Use `try_write` with timeout for diagnostic; if it fails, log and skip the mutation (data race).

**Tests:**
- `gateway_risk_sees_positions_after_apply_fill` — sequence: enqueue intent, process, apply_fill, enqueue same instrument again; second intent's risk check must reflect the new position.
- `sleeve_state_handle_thread_safe_concurrent_reads` — proptest with 1000 concurrent reads and 100 writes; no data race.
- `gateway_position_check_fires_correctly` — large existing position, small intent that would breach `max_position_notional`; assert reject.

**Verification:**
- Replay a captured fill stream; dump position state at multiple points; compare to a hand-computed expectation.

**Effort:** 8-10 hours including the type plumbing through the call chain.

---

### B4: Extend OMS transition table for `Acked → Rejected`

**Audit finding:** §4.6 [HIGH]

**Goal:** The OMS state machine allows a venue-initiated reject from `Acked` and from `PartiallyFilled` states. Today it only allows `Submitted → Rejected`, which doesn't match real-world venue behavior.

**Files to modify:**
- `rust/crates/oms/src/lib.rs:150-167` — extend `transition_allowed`.

**New code:**

```rust
fn transition_allowed(from: OrderState, to: OrderState) -> bool {
    use OrderState::*;
    matches!(
        (from, to),
        (Created, Submitted)
            | (Submitted, Acked)
            | (Submitted, Rejected)
            | (Submitted, Canceled)
            | (Acked, PartiallyFilled)
            | (Acked, Filled)
            | (Acked, Canceled)
            | (Acked, Expired)
            | (Acked, Rejected)            // NEW: venue post-ack reject
            | (PartiallyFilled, PartiallyFilled)
            | (PartiallyFilled, Filled)
            | (PartiallyFilled, Canceled)
            | (PartiallyFilled, Expired)
            | (PartiallyFilled, Rejected)  // NEW: venue rejects mid-flight
    )
}
```

**Implementation steps:**

1. Update the matchset above.
2. Update the state-diagram doc comment at `oms/src/lib.rs:10-19` to reflect the new edges.
3. Add a "rejection reason" optional field already exists; nothing else changes structurally.

**Edge cases:**
- Reject after partial fill: filled quantity is preserved; only the unfilled remainder is canceled by the venue. The order state goes to `Rejected` (or `Canceled`, depending on venue semantics — use `Rejected` for venue-initiated, `Canceled` for our-initiated).
- The audit doc mentions [`live-rust-runner-roadmap.md:341-352`](../docs/live-rust-runner-roadmap.md:341) suggests adding `replace_pending`, `reconciled` states. Out of scope for B4; track separately.

**Tests:**
- `oms_acked_to_rejected_now_allowed` — sequence: place → submit → ack → reject (with reason "market_closed"); assert success.
- `oms_partial_filled_to_rejected_preserves_filled_quantity` — partial fill of 3/10, then reject; assert `state = Rejected, filled_quantity = "3"`.

**Verification:**
- Construct a venue-side reject fixture; replay through end-to-end; OMS reaches `Rejected` cleanly.

**Effort:** 1 hour.

---

### B5: Mark-price tracking for accurate gross-exposure

**Audit finding:** §4.11 [MEDIUM]

**Goal:** Risk's gross-exposure projection uses a live mark price per instrument, not the conservative `max(avg_price, intent_price)`.

**Files to modify:**
- `rust/crates/risk/src/lib.rs` — add `mark_prices` to `SleeveState`.
- Live-runner main loop — update mark from every `HotEvent::Quote`.

**New API:**

```rust
// SleeveState additions
pub mark_prices: HashMap<String, Decimal>,  // instrument_id -> mid price
```

**Implementation steps:**

1. Add `mark_prices` field.
2. In `live-runner::main` strategy event handling, after projecting a `HotEvent::Quote`, write the mid to `state.write(|s| s.mark_prices.insert(instrument, mid))`. Mid = `(bid + ask) / 2`.
3. In `risk/src/lib.rs::evaluate`, the gross-exposure projection at line 195-204 uses `state.mark_prices.get(instrument).unwrap_or(&pos.avg_price)` instead of `pos_price.max(price)`.

**Edge cases:**
- Mark unavailable for a new instrument (first quote not yet seen): fall back to `avg_price` for existing positions, `intent.price` for the new intent.
- Stale mark (data freshness): the `market_data_age_secs` check already handles this — if mark is stale, the intent is rejected at the freshness step, never reaching gross-exposure.

**Tests:**
- `risk_uses_mark_price_for_gross_exposure_when_available` — fixture with a position at avg=0.40 and a mark at 0.60; assert gross uses 0.60.
- `risk_falls_back_to_avg_when_mark_missing` — same position, no mark; assert gross uses 0.40.

**Verification:**
- Compare risk decisions before and after on a captured fixture with realistic mark movement.

**Effort:** 2 hours.

---

## Phase B Acceptance Criteria

- [ ] All workspace tests pass.
- [ ] Workspace grep finds zero `f64` arithmetic in OMS hot paths.
- [ ] `parse_decimal_strict` rejects every previously-silent-zero failure case via a regression test.
- [ ] After a fill, `gateway.sleeve_state.positions[instrument]` reflects the actual position.
- [ ] Gateway's risk re-check uses the same `SleeveState` as the runner's first risk check (verified by an integration test that writes a position into the shared state and asserts both checks see it).
- [ ] OMS allows `Acked → Rejected` transition with a venue-supplied reason.
- [ ] Manual: place a $0.40 bid on demo with `--max-position-notional 100`; observe an immediate fill; place a second buy that would exceed; observe gateway-side `PositionNotionalExceeded` rejection.

---

## Phase C: Operator Safety Net

**Goal:** An operator can halt trading instantly and cancel every open order in one command. Strategies don't deadlock themselves after a single bad order. Model outputs can't drive trades when they're malformed.

**Prerequisites:** Phase A (so cancel-all can reach all orders).

**Capabilities unlocked:**
- A file-touch or signal triggers full halt + cancel-all.
- Strategies recover after a rejection and retry on subsequent events.
- A NaN/Inf model output stops execution rather than triggering a trade.

---

### C1: Out-of-band kill switch with bulk cancel

**Audit finding:** §4.10 [HIGH]

**Goal:** A kill-switch mechanism that can be triggered without restarting the process or having developer access. On trigger: set `state.kill_switch_engaged = true` (blocking all new intents) and call `venue.cancel_all`.

**Files to create:**
- `rust/crates/live-runner/src/kill_switch.rs` — file-watch and signal handlers.

**Files to modify:**
- `rust/crates/live-runner/src/main.rs` — wire the kill switch task.

**New API:**

```rust
// rust/crates/live-runner/src/kill_switch.rs

use std::path::PathBuf;
use std::time::Duration;
use tokio::time::interval;

pub struct KillSwitchConfig {
    /// File path whose existence triggers the kill switch.
    /// Default: `./KILL_SWITCH`.
    pub trigger_file: PathBuf,
    /// Poll interval. Default 1s.
    pub poll_interval: Duration,
}

pub async fn watch_kill_switch(
    config: KillSwitchConfig,
    state: SleeveStateHandle,
    venue: Arc<Mutex<Box<dyn VenueClient + Send>>>,
) {
    let mut ticker = interval(config.poll_interval);
    loop {
        ticker.tick().await;
        if config.trigger_file.exists() {
            eprintln!("kill switch triggered (file present): {}", config.trigger_file.display());
            state.write(|s| s.kill_switch_engaged = true);
            // Bulk cancel.
            let report = venue.lock().await.cancel_all();
            match report {
                Ok(r) => eprintln!("kill-switch: canceled {} orders ({} failures)",
                    r.canceled.len(), r.failed.len()),
                Err(e) => eprintln!("kill-switch: cancel_all error: {e}"),
            }
            // Stay engaged until process exit. Operator removes the file
            // and restarts the process to resume.
            return;
        }
    }
}
```

Also handle SIGUSR1 (Unix) / Ctrl-Break (Windows) as alternative triggers.

**Implementation steps:**

1. Create `kill_switch.rs` with the watcher.
2. In `main()`, spawn the watcher as a tokio task alongside the strategy task. Trigger fires through the shared `state_handle` and the cloned venue.
3. The risk gate already rejects intents when `kill_switch_engaged == true` (see `risk/src/lib.rs:121-123`). Nothing additional needed there.
4. After the kill switch fires, the strategy loop continues but every intent is risk-rejected. The runner exits when duration elapses.
5. CLI flag: `--kill-switch-file PATH` (default `./KILL_SWITCH`).

**Edge cases:**
- File created and then immediately deleted (operator mistake): the latch is one-way — once triggered, stays engaged for the rest of the process. To resume, operator restarts.
- Polling overhead: 1s poll is negligible.
- Trigger happens during a venue submit: the venue cancel runs concurrently and may race; that's fine — Kalshi accepts cancel on any state.

**Tests:**
- `kill_switch_triggers_on_file_creation` — fixture creates the file; assert state.kill_switch_engaged == true within poll interval.
- `kill_switch_calls_cancel_all_once` — mock venue; trigger kill; assert exactly one cancel_all call.
- `kill_switch_blocks_subsequent_intents` — trigger kill; send intent; assert risk reject with `KillSwitchEngaged`.

**Verification:**
- Manual: during a live run, `touch KILL_SWITCH`; observe within 1s the strategy stops emitting acks and the Kalshi UI shows all orders canceled.

**Effort:** 3-4 hours.

---

### C2: Cross-process cancel via venue-id persistence and REST fallback

**Audit finding:** §4.13 [HIGH]

**Goal:** After a process restart, the runner can cancel orders it placed in a previous lifetime. Combine two mechanisms: persist the `venue_ids` cache to disk between runs, AND implement a REST lookup as fallback for orders not in the cache.

**Files to modify:**
- `rust/crates/kalshi/src/venue_client.rs` — persistence and fallback.
- `rust/crates/kalshi/src/rest.rs` — add `get_orders_by_client_order_id` if not present.

**New API:**

```rust
// rust/crates/kalshi/src/rest.rs
impl KalshiRest {
    /// Look up an order by our client-side id. Kalshi's
    /// `GET /portfolio/orders?client_order_id=...` endpoint.
    pub async fn get_order_by_client_id(
        &self,
        client_order_id: &str,
    ) -> Result<Option<KalshiOrder>, RestError> { ... }
}

// rust/crates/kalshi/src/venue_client.rs
pub struct KalshiVenueClient {
    /* existing fields */
    /// Optional persistence file. None = in-memory only.
    venue_ids_persist_path: Option<PathBuf>,
}

impl KalshiVenueClient {
    pub fn with_persistence(mut self, path: PathBuf) -> Self {
        self.venue_ids_persist_path = Some(path);
        // Load existing cache if file exists.
        if let Some(p) = &self.venue_ids_persist_path {
            if p.exists() {
                if let Ok(data) = std::fs::read_to_string(p) {
                    if let Ok(map) = serde_json::from_str::<HashMap<String, String>>(&data) {
                        self.venue_ids = map;
                    }
                }
            }
        }
        self
    }

    fn persist(&self) {
        if let Some(p) = &self.venue_ids_persist_path {
            // Best-effort serialization. Write atomically via tmpfile + rename.
            let tmp = p.with_extension("tmp");
            if let Ok(data) = serde_json::to_string(&self.venue_ids) {
                let _ = std::fs::write(&tmp, data);
                let _ = std::fs::rename(&tmp, p);
            }
        }
    }
}
```

Update `submit` to call `self.persist()` after every cache update. Update `cancel`:

```rust
fn cancel(/* ... */) -> Result<GatewayAck, GatewayError> {
    let venue_order_id = match self.venue_ids.get(client_order_id) {
        Some(v) => v.clone(),
        None => {
            // FALLBACK: ask the venue.
            let rest = self.rest.clone();
            let handle = self.handle.clone();
            let coid = client_order_id.to_string();
            let order = block_in_place(move || {
                handle.block_on(async move { rest.get_order_by_client_id(&coid).await })
            }).map_err(rest_err_to_gateway)?
              .ok_or_else(|| GatewayError::Transport(format!(
                  "no order found at venue for client_order_id `{client_order_id}`"
              )))?;
            // Populate cache for future cancels.
            self.venue_ids.insert(client_order_id.to_string(), order.order_id.clone());
            self.persist();
            order.order_id
        }
    };
    // ... existing cancel logic ...
}
```

**Implementation steps:**

1. Implement `KalshiRest::get_order_by_client_id`. Signed GET with the query param.
2. Add persistence path to `KalshiVenueClient`. Builder method.
3. On every cache mutation, persist (atomic rename).
4. Fallback path in `cancel`.
5. CLI flag in `live-runner`: `--venue-cache-path PATH` (default `./.kalshi-venue-cache.json`). Gitignore it.

**Edge cases:**
- Persistence file corrupted: log error, start with empty cache. Don't crash.
- Concurrent processes writing to the same cache path: serialize via file lock (`fs2` crate) or use per-process suffixed paths.
- Cache larger than memory budget: bounded LRU eviction (cap at 100k entries). For a typical sleeve this is years' worth of orders.
- REST fallback fails (network or auth): bubble up; operator must handle manually.

**Tests:**
- `venue_cache_persists_across_instances` — create client, submit (mocked), drop client; create new client with same path; assert cache loaded.
- `cancel_fallback_to_rest_succeeds` — mock REST returns the order; cache is empty; cancel succeeds and populates cache.
- `persist_is_atomic` — concurrent persist + read; never observes partial JSON.

**Verification:**
- Restart the live-runner with active orders; verify `cancel_all` on shutdown reaches them via cache load.

**Effort:** 5-6 hours.

---

### C3: Prediction output validation

**Audit finding:** §3.2 [HIGH]

**Goal:** Every scorer output is validated for finiteness and range before flowing to a strategy decision. Bad outputs trigger a typed error, not a trade.

**Files to modify:**
- `rust/crates/feature-builder/src/lib.rs` — extend `ScorerError`.
- `rust/crates/model-runtime/src/lib.rs` — validate in `OnnxScorer::predict`.
- `rust/crates/runner/src/lib.rs` — `OnnxQuoteStrategy::on_event` treats scorer errors as no-action with audit.

**New API:**

```rust
// rust/crates/feature-builder/src/lib.rs
#[derive(Debug, Error)]
pub enum ScorerError {
    /* existing */
    #[error("scorer produced non-finite value: {value}")]
    NonFiniteOutput { value: f32 },
    #[error("scorer produced out-of-range value: {value} (expected in [{min}, {max}])")]
    OutOfRangeOutput { value: f32, min: f32, max: f32 },
    #[error("scorer produced empty output")]
    EmptyOutput,
}

pub trait Scorer: Send {
    fn input_width(&self) -> usize;
    fn predict(&mut self, features: &[f32]) -> Result<Vec<f32>, ScorerError>;
}

// rust/crates/model-runtime/src/lib.rs
impl OnnxScorer {
    /* ... */
    /// Validate the output tensor. Probability-mode scorers (config flag)
    /// reject anything outside [0, 1].
    fn validate_output(&self, out: &[f32]) -> Result<(), ScorerError> {
        if out.is_empty() {
            return Err(ScorerError::EmptyOutput);
        }
        for &v in out {
            if !v.is_finite() {
                return Err(ScorerError::NonFiniteOutput { value: v });
            }
            if self.is_probability_output && (v < 0.0 || v > 1.0) {
                return Err(ScorerError::OutOfRangeOutput { value: v, min: 0.0, max: 1.0 });
            }
        }
        Ok(())
    }
}
```

Add `is_probability_output: bool` to `OnnxScorer` (set via builder, defaults `true` for `OutputSelect::ScalarAt`).

**Implementation steps:**

1. Add the error variants.
2. Add `is_probability_output` flag and validation in `predict_inner`.
3. In `OnnxQuoteStrategy::on_event`, change the predict block:
   ```rust
   let out = match self.scorer.predict(&features) {
       Ok(v) => v,
       Err(ScorerError::NonFiniteOutput { .. } | ScorerError::OutOfRangeOutput { .. }) => {
           // Record but don't trade.
           // TODO(D3): emit a malformed_prediction audit record.
           return Ok(vec![]);
       }
       Err(e) => return Err(RunnerError::Strategy(format!("scorer error: {e}"))),
   };
   ```

**Edge cases:**
- A model that legitimately produces values outside [0,1] (e.g., regression scoring). Solution: the `is_probability_output` flag, set per-model via spec.
- Subnormal floats (very close to zero): they're finite; accept.
- The output tensor has shape (1, N) but we select via `ScalarAt(idx)` past the end. Already errors in `run_inner` via `array.get([0, idx]).copied().ok_or(...)`. Tighten by checking dimensions at load.

**Tests:**
- `scorer_rejects_nan_output` — mock scorer returns `[f32::NAN]`; assert `NonFiniteOutput`.
- `scorer_rejects_inf_output` — `[f32::INFINITY]`; same.
- `scorer_rejects_out_of_range_for_probability_mode` — returns `[1.5]` with `is_probability_output = true`; assert `OutOfRangeOutput`.
- `scorer_accepts_negative_for_regression_mode` — same `[-1.5]` with the flag off; assert success.
- `onnx_quote_strategy_skips_trade_on_invalid_output` — wire `MockScorer` that returns NaN; strategy emits zero decisions.

**Verification:**
- Inject a corrupted model that produces NaN; observe zero trades and metric `malformed_predictions` increment.

**Effort:** 2-3 hours.

---

### C4: Strategy "submitted" flag resets on terminal state

**Audit finding:** §3.1 [HIGH]

**Goal:** When an order placed by a strategy reaches a terminal non-Filled state (Rejected, Canceled, Expired), the strategy's per-instrument `submitted` flag clears so subsequent events can trigger a fresh order.

**Files to modify:**
- `rust/crates/runner/src/lib.rs` — `TennisXgboostStrategy` and `OnnxQuoteStrategy` track per-instrument `OrderTrackingState` instead of a `bool`.

**New API:**

```rust
// internal to each strategy (or shared in a runner helper module)

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
enum OrderTrackingState {
    #[default]
    None,
    Pending { client_order_id: SmolStr },
    Filled,  // sticky; never re-trade
    // Rejected, Canceled, Expired → revert to None on the next observation
}
```

The tracking state advances via:
- Strategy emits order → `Pending`
- OMS reports fill → `Filled`
- OMS reports terminal non-Filled → revert to `None`

Strategies poll for OMS state by receiving `StrategyEvent::OwnOrderUpdate` (a new variant — see §2.3 in audit, scoped here just for own-order events). If we don't want to add the variant yet, alternative: thread an `Option<&OmsView>` into `StrategyContext` so strategies can query.

**Decision:** simplest implementation — extend `StrategyContext` with `recently_terminated_orders: HashSet<String>` populated from OMS state changes since the last event. Strategy checks this set against its `submitted` map.

**Implementation steps:**

1. Add to `StrategyContext`:
   ```rust
   pub recently_terminated_orders: HashSet<String>,
   ```
2. In `SleeveRunner::run` and `live-runner::main`, after each batch of OMS transitions, populate this set with `client_order_id`s that went terminal-non-Filled. Reset on the next iteration.
3. In `OnnxQuoteStrategy::on_event`, before emitting:
   ```rust
   // Check if our prior order terminated without filling. If so, reset.
   if let Some(prior_coid) = self.pending_orders.get(market_id) {
       if ctx.recently_terminated_orders.contains(prior_coid) {
           self.pending_orders.remove(market_id);
           self.submitted.remove(market_id);
       }
   }
   ```
4. Maintain `pending_orders: HashMap<market_id, client_order_id>` in the strategy so we know what to look for.

**Edge cases:**
- The same client_order_id terminated and a new one placed in the same tick: ordering matters. Process terminations first, then emit.
- Partial fill then cancel: order is partially filled; treat as `Filled` for the "did this trade?" question. Strategy should NOT re-trade after a partial.
- Lookup against the OMS state directly (preferred long-term): `ctx.oms_state` reference. Reserve for refactor; the recently-terminated set is the v1.

**Tests:**
- `strategy_resets_after_rejection` — emit intent → OMS rejects → next event re-emits.
- `strategy_stays_locked_after_fill` — emit intent → OMS fills → next event emits zero.
- `strategy_stays_locked_after_partial_fill_then_cancel` — partial then cancel → strategy treats as filled.

**Verification:**
- Fixture: 100 events with 50/50 reject/fill outcomes; assert strategy emits N+1 orders where N is the number of rejections.

**Effort:** 4-5 hours including the context plumbing.

---

## Phase C Acceptance Criteria

- [ ] `touch KILL_SWITCH` triggers cancel-all within 1s.
- [ ] Process restart can cancel orders placed by the previous process.
- [ ] Injected NaN model output produces zero trades and a metric increment.
- [ ] After a rejected order, the strategy re-emits on the next event (verified in test fixture).
- [ ] All workspace tests pass.

---

## Phase D: Live Capital Tracking

**Goal:** Real cash tracking. Real daily-loss enforcement. Audit chain links predictions to decisions to fills.

**Prerequisites:** Phases A, B (so fills are arriving and updating positions exactly).

**Capabilities unlocked:**
- `max_daily_loss` risk check actually fires.
- Settlement events drive realized PnL.
- Every trade has a provable lineage back to the model output that drove it.

---

### D1: Ledger crate

**Audit finding:** §4.8 [HIGH]

**Goal:** A double-entry ledger that records every cash-affecting event: order placement (capital hold), fill (capital convert to position), fee, settlement (binary payout), cancel (release hold). Persisted to SQLite for recovery.

**Files to create:**
- `rust/crates/ledger/Cargo.toml`
- `rust/crates/ledger/src/lib.rs`
- `rust/crates/ledger/src/sqlite_store.rs`

**Files to modify:**
- `rust/Cargo.toml` — add member.
- `rust/crates/gateway/src/lib.rs` — emit ledger entries on submit/ack/fill/cancel.

**New API:**

```rust
// rust/crates/ledger/src/lib.rs

use rust_decimal::Decimal;
use chrono::{DateTime, Utc};

#[derive(Clone, Debug, PartialEq)]
pub enum LedgerEntryKind {
    OpenHold { client_order_id: String, amount: Decimal },
    ReleaseHold { client_order_id: String, amount: Decimal },
    Fill { fill_id: String, client_order_id: String, cash_delta: Decimal },
    Fee { fill_id: String, amount: Decimal },
    Settlement { market_id: String, payout: Decimal },
}

#[derive(Clone, Debug)]
pub struct LedgerEntry {
    pub entry_id: String,           // ULID
    pub timestamp: DateTime<Utc>,
    pub kind: LedgerEntryKind,
    pub cash_balance_after: Decimal,
    pub locked_capital_after: Decimal,
    pub realized_pnl_today: Decimal,
}

pub trait LedgerStore: Send + Sync {
    fn append(&mut self, entry: LedgerEntry) -> Result<(), LedgerError>;
    fn current_balance(&self) -> Result<Decimal, LedgerError>;
    fn current_locked(&self) -> Result<Decimal, LedgerError>;
    fn realized_pnl_today(&self, day: chrono::NaiveDate) -> Result<Decimal, LedgerError>;
    fn entries_since(&self, ts: DateTime<Utc>) -> Result<Vec<LedgerEntry>, LedgerError>;
}

pub struct SqliteLedgerStore { /* connection, prepared statements */ }
pub struct InMemoryLedgerStore { /* for tests */ }
```

**Implementation steps:**

1. Create the crate and add to workspace.
2. Define the schema:
   ```sql
   CREATE TABLE IF NOT EXISTS ledger_entries (
       entry_id TEXT PRIMARY KEY,
       timestamp TEXT NOT NULL,
       kind TEXT NOT NULL,
       payload_json TEXT NOT NULL,
       cash_balance_after TEXT NOT NULL,
       locked_capital_after TEXT NOT NULL,
       realized_pnl_today TEXT NOT NULL
   );
   CREATE INDEX IF NOT EXISTS idx_ledger_ts ON ledger_entries (timestamp);
   ```
3. Implement `SqliteLedgerStore` using `rusqlite`. Open with `journal_mode = WAL`. Use prepared statements.
4. Implement `InMemoryLedgerStore` for tests using `Vec<LedgerEntry>`.
5. Wire into `DryRunGateway`:
   - On `submit` ack: emit `OpenHold { amount: price * quantity }`.
   - On `cancel`: emit `ReleaseHold { amount }`.
   - On `apply_fill`: emit `Fill { cash_delta: -price * fill_qty }` and `ReleaseHold` for the unfilled remainder if final fill.
   - On `Fee` event (if Kalshi provides one): emit `Fee`.
   - On settlement: emit `Settlement { payout }`.
6. The ledger derives `current_balance` from the running sum. Initial balance is loaded from a config (`--initial-capital 1000.00`) or fetched once from Kalshi (`GET /portfolio/balance`).

**Edge cases:**
- Crash between appending the entry and updating `cash_balance_after`: solve via single transaction — append + compute in one SQL statement, or use an event-sourced approach where `cash_balance_after` is derived on read, not stored.
- Replay safety: if the gateway re-processes an idempotent ack (which it shouldn't), the ledger should detect and skip. Use `(kind, client_order_id, fill_id)` natural key with `INSERT OR IGNORE`.
- Day boundary: `realized_pnl_today` resets at UTC midnight. Configurable timezone.
- Concurrent readers: WAL mode handles this; document the assumption.

**Tests:**
- `ledger_records_open_hold_on_ack` — submit → ack → assert one OpenHold entry, balance decreased.
- `ledger_releases_hold_on_cancel` — submit → ack → cancel → assert ReleaseHold + balance restored.
- `ledger_fill_converts_hold_to_position_cost` — submit → ack → full fill → assert Fill entry, locked = 0.
- `ledger_partial_fill_releases_remainder_on_cancel` — partial fill 3/10 → cancel → assert ReleaseHold for 7 units.
- `ledger_survives_restart_via_sqlite` — append entries, drop store, reopen, assert state recovered.
- `ledger_realized_pnl_today_resets_at_utc_midnight` — fixture across day boundary.

**Verification:**
- Run a captured fill stream through the ledger; sum cash_delta + fee + settlement = realized_pnl. Cross-check with a manual spreadsheet.

**Effort:** 12-16 hours.

---

### D2: Daily realized loss propagation to risk

**Audit finding:** §4.8 [HIGH]

**Goal:** `SleeveState.daily_realized_loss` reflects the ledger's `realized_pnl_today` so `max_daily_loss` risk check actually fires.

**Files to modify:**
- `rust/crates/gateway/src/lib.rs` — after every ledger append, update `state.daily_realized_loss`.

**Implementation steps:**

1. After `apply_fill` (and settlement events when those exist), refresh state:
   ```rust
   let pnl = self.ledger.realized_pnl_today(Utc::now().naive_utc().date())?;
   state.write(|s| s.daily_realized_loss = pnl.max(Decimal::ZERO));  // loss is negative pnl; flip sign
   ```
   Actually the risk gate at `risk/src/lib.rs:170-175` checks `fixed_from_f64(state.daily_realized_loss) > max_loss` so the sign convention is "loss is positive". Be careful: `daily_realized_loss = -ledger.realized_pnl_today` when pnl is negative, otherwise 0.

2. Add a background tokio task that ticks every 30s and refreshes `daily_realized_loss` even without trades (for cases where settlement events change PnL).

**Edge cases:**
- PnL is positive (profit): `daily_realized_loss` stays 0. The risk check is one-sided.
- A late-arriving settlement bumps yesterday's PnL after midnight: the per-day ledger query handles this correctly (queries today's entries only).
- Decimal-to-fixed-i128 conversion in risk: B1 changed risk to use Decimal directly; no conversion needed.

**Tests:**
- `daily_loss_updates_after_losing_fill` — fill at higher price than mark, realized loss > 0, risk blocks the next big trade.
- `daily_loss_resets_at_midnight` — losses accumulate, then UTC midnight tick resets to 0.

**Verification:**
- Place enough losing trades on demo to approach the `max_daily_loss` limit; observe risk reject at the threshold.

**Effort:** 2-3 hours.

---

### D3: Prediction record audit chain

**Audit finding:** §3.3 [HIGH]

**Goal:** Every ONNX prediction emits a `PredictionRecord` linking model artifact checksum, feature schema, input vector hash, and the predicted value. The decision audit record references it.

**Files to modify:**
- `rust/crates/contracts/src/lib.rs` — `PredictionRecord` already exists. Confirm it has all needed fields.
- `rust/crates/runner/src/lib.rs` — `OnnxQuoteStrategy` emits prediction records via a sink.

**New API:**

```rust
// rust/crates/runner/src/lib.rs

pub trait PredictionSink: Send {
    fn emit(&mut self, record: PredictionRecord) -> Result<(), RunnerError>;
}

pub struct VecPredictionSink {
    pub records: Vec<PredictionRecord>,
}

// in OnnxQuoteStrategy:
pub struct OnnxQuoteStrategy<S: Scorer, K: PredictionSink> {
    /* existing fields */
    sink: K,
    model_name: String,        // from spec
    model_version: String,     // from spec
    feature_schema_hash: String,  // from bundle manifest, hashed at load
}
```

**Implementation steps:**

1. Verify `PredictionRecord` shape at `contracts/src/lib.rs:281-308` — it has `model_name`, `model_version`, `instrument_id`, `timestamp`, `horizon_seconds`, `value`, `confidence`, `audit`. The audit `parent_ids` field is the link target.
2. After `scorer.predict`, before emitting a decision:
   ```rust
   let feature_hash = canonical_sha256(&features.to_vec())?;
   let prediction = PredictionRecord {
       model_name: self.model_name.clone(),
       model_version: self.model_version.clone(),
       instrument_id: Some(instrument.clone()),
       timestamp: ctx.now.clone(),
       horizon_seconds: 0,  // intra-event; configurable per model
       value: format!("{}", score),
       confidence: None,
       audit: AuditStamp {
           object_id: format!("pred-{}", ulid()),
           object_kind: "prediction".into(),
           schema_version: "prediction-v1".into(),
           produced_at: ctx.now.clone(),
           producer: self.strategy_id.clone(),
           canonical_sha256: String::new(),
           parent_ids: vec![/* event id */],
           trace_id: None,
           metadata: vec![("feature_hash", feature_hash)].into_iter().collect(),
       },
   };
   stamp_record(&prediction_without_audit, &mut prediction.audit);
   self.sink.emit(prediction.clone())?;
   ```
3. The subsequent `IntentEnvelopeRecord` includes `audit.parent_ids = [prediction.audit.object_id]`.
4. Persist prediction records: either to the ledger (extend with a `predictions` table) or to a separate audit log (JSON-lines file per day).

**Edge cases:**
- Multiple predictions per tick (vector output, multiple decisions): emit one record per logical prediction.
- Sink failure: bubble up; don't trade if you can't audit.

**Tests:**
- `prediction_emitted_per_score` — fixture run with N quotes that trigger trades; assert N prediction records emitted.
- `prediction_links_to_decision_via_parent_id` — assert `intent.audit.parent_ids` contains the prediction's `object_id`.

**Verification:**
- Replay a fixture; trace one decision back to its prediction back to the input event using the audit chain.

**Effort:** 4-5 hours.

---

### D4: Sequence-gap detection

**Audit finding:** §1.3 [HIGH]

**Goal:** Detect when Kalshi's WS dropped a message between snapshot and delta or between deltas. Either re-subscribe or mark the affected instrument as stale.

**Files to modify:**
- `rust/crates/kalshi/src/normalize.rs` — track per-(sid, channel) last_seq.
- `rust/crates/live-runner/src/main.rs` — react to gaps.

**New API:**

```rust
// rust/crates/kalshi/src/normalize.rs

pub struct SequenceTracker {
    last_seq_per_key: HashMap<(i64, String), i64>,  // (sid, channel) -> last seq
}

impl SequenceTracker {
    pub fn observe(&mut self, sid: Option<i64>, channel: &str, seq: Option<i64>) -> Option<i64> {
        // Returns the gap size if a gap is detected; None otherwise.
    }
}

// Extend NormalizeError:
pub enum NormalizeError {
    /* existing */
    SequenceGap { sid: i64, channel: String, expected: i64, got: i64 },
}
```

**Implementation steps:**

1. Add `SequenceTracker` field to the normalizer caller. Thread it through `normalize_ws_payload` (alternatively: a static Mutex but prefer explicit state).
2. After successful normalize but before returning, observe the sequence. If gap > 0, emit a `NormalizeError::SequenceGap` (and the event still passes through — the tracker is informational).
3. In the live-runner main loop, count gaps. On significant gap (configurable, default >5), force re-subscribe to that ticker.
4. Mark `state.market_data_age_secs[instrument] = u32::MAX` until a fresh snapshot arrives, blocking trading on that instrument.

**Edge cases:**
- Initial message (no prior seq): no gap; record the seq as baseline.
- seq wraparound: Kalshi uses i64; wraparound is not realistic (centuries of messages).
- Channel restart (Kalshi may reset sequences after reconnect): on reconnect, reset the tracker for those (sid, channel) keys.

**Tests:**
- `sequence_tracker_detects_simple_gap` — fixture: seq [1, 2, 4]; assert gap of 1 detected at the 4.
- `sequence_tracker_ignores_first_message` — fixture: first message any seq; no gap reported.
- `sequence_tracker_resets_on_reconnect` — reset method called; next seq becomes new baseline.

**Verification:**
- Synthesize a WS dump with intentional gap; assert detection.

**Effort:** 3-4 hours.

---

## Phase D Acceptance Criteria

- [ ] Ledger records every cash-affecting event.
- [ ] `daily_realized_loss` reflects ledger truth.
- [ ] A losing fill stream triggers `DailyLossExceeded` at the configured limit.
- [ ] Prediction record audit chain links every trade to its model output via `parent_ids`.
- [ ] Sequence gaps are detected and force re-subscribe.

---

## Phase E: Operational Visibility

**Goal:** Run unattended. On-call can answer "what's it doing" without developer access.

**Prerequisites:** Phase A-D (so the metrics report truth).

**Capabilities unlocked:**
- Prometheus scrape endpoint with live counters/histograms.
- JSON-lines structured logs with correlation ids.
- One runbook per common incident.

---

### E1: Prometheus metrics export

**Goal:** A `/metrics` HTTP endpoint exposes Prometheus-format counters and histograms.

**Files to create:**
- `rust/crates/live-runner/src/metrics.rs` — Prometheus registry + named metrics.

**Files to modify:**
- `rust/crates/live-runner/src/main.rs` — spawn HTTP server task.
- `rust/crates/live-runner/Cargo.toml` — `prometheus = "0.13"`, `axum = "0.7"` or `hyper`.

**New API:**

```rust
// rust/crates/live-runner/src/metrics.rs
use prometheus::{Encoder, IntCounter, IntCounterVec, Histogram, HistogramVec, Registry};
use std::sync::Arc;

pub struct Metrics {
    pub raw_events_total: IntCounter,
    pub normalized_events_total: IntCounter,
    pub normalize_errors_total: IntCounterVec,  // labels: error_kind
    pub decisions_total: IntCounter,
    pub intents_approved_total: IntCounter,
    pub intents_rejected_total: IntCounterVec,  // labels: reason
    pub gateway_acks_total: IntCounter,
    pub gateway_errors_total: IntCounterVec,    // labels: error_kind
    pub own_fills_applied_total: IntCounter,
    pub ws_reconnects_total: IntCounter,
    pub ws_idle_timeouts_total: IntCounter,
    pub kill_switch_active: IntGauge,
    pub normalize_latency_us: Histogram,
    pub strategy_latency_us: Histogram,
    pub end_to_end_latency_us: Histogram,
    pub sequence_gaps_total: IntCounterVec,     // labels: instrument
    pub registry: Registry,
}

impl Metrics {
    pub fn new() -> Self { /* register all */ }
    pub fn render(&self) -> String { /* encode prometheus text format */ }
}
```

**Implementation steps:**

1. Define the metrics struct with `Arc<Metrics>` shared across tasks.
2. Replace the existing `Metrics` struct in `live-runner/src/main.rs:636-659` with the new one, mapping each counter.
3. Spawn an HTTP server on `--metrics-port` (default 9090) with one route: `GET /metrics` → returns Prometheus text.
4. Update every existing metric increment site to use the new typed counters.
5. Add histograms for the three latency vectors (currently `Vec<u64>`); use Prometheus buckets `[10us, 50us, 100us, 500us, 1ms, 5ms, 50ms, 500ms]`.

**Edge cases:**
- The HTTP server must not block the main strategy loop. Spawn in a separate tokio task; it's stateless.
- Port already in use: log and exit with a clear message. Don't fall back to a random port (predictability matters).

**Tests:**
- `metrics_endpoint_returns_prometheus_format` — start server, fetch `/metrics`, assert response contains expected counter names.
- `metrics_counters_increment_on_events` — synthetic event stream; assert counters reflect the count.

**Verification:**
- `curl http://localhost:9090/metrics` after a live run; pipe to a Prometheus instance briefly.

**Effort:** 4-5 hours.

---

### E2: Structured JSON logging

**Goal:** Replace `eprintln!` with `tracing` JSON-lines logs that include correlation ids, strategy ids, severity.

**Files to modify:**
- All Rust crates — replace `eprintln!` with `tracing::{info, warn, error}`.
- `rust/crates/live-runner/src/main.rs` — initialize `tracing_subscriber` with JSON format.

**New API:**

```rust
// main.rs
fn init_tracing() {
    use tracing_subscriber::{fmt, EnvFilter};
    fmt()
        .json()
        .with_env_filter(EnvFilter::from_default_env().add_directive("info".parse().unwrap()))
        .with_target(true)
        .init();
}
```

Use spans for the per-event lifecycle:
```rust
let span = tracing::info_span!("event_processing",
    event_id = %normalized.event_id,
    correlation_id = tracing::field::Empty,
);
let _enter = span.enter();
// ... process ...
// when correlation_id is known, record it:
span.record("correlation_id", &correlation_id.as_str());
```

**Implementation steps:**

1. Add `tracing`, `tracing-subscriber` (with `json` feature) to live-runner.
2. Replace every `eprintln!`. Categorize:
   - Routine info → `info!`
   - Recoverable errors (parse, retry) → `warn!`
   - Unrecoverable errors → `error!`
3. Add spans for the event processing pipeline.
4. CLI flag `--log-format json|pretty` for development.

**Edge cases:**
- Log volume: `info!` per event is too much. Sample to 1% or move to `debug!`. Keep `info!` for state transitions.
- Secret values in logs: redact `KALSHI_PRIVATE_KEY_PATH`, etc. Already redacted in `KalshiAuth::Debug` impl.

**Tests:**
- `tracing_emits_json_with_required_fields` — capture stdout, parse a log line as JSON, assert fields.

**Effort:** 6-8 hours (mechanical but touches many files).

---

### E3: Heartbeat and market-data-age updates

**Goal:** Background task that maintains `state.market_data_age_secs[instrument]` so the freshness risk check has real data.

**Files to modify:**
- `rust/crates/live-runner/src/main.rs` — track last quote time per instrument, age out.

**Implementation steps:**

1. Add `last_quote_at: HashMap<InstrumentId, Instant>` to the live-runner's local state.
2. On every `HotEvent::Quote`, update `last_quote_at[instrument] = Instant::now()`.
3. Background tokio task every 1s computes age for each instrument:
   ```rust
   state.write(|s| {
       for (inst, last) in &last_quote_at {
           let age = last.elapsed().as_secs() as u32;
           s.market_data_age_secs.insert(inst.to_string(), age);
       }
   });
   ```
4. Risk gate already checks `max_market_data_age_secs`. Nothing else changes.

**Tests:**
- `market_data_age_increments_without_quotes` — fixture: stop sending quotes for 60s; assert age = 60.
- `risk_rejects_intent_on_stale_data` — set age above limit; assert reject.

**Effort:** 2 hours.

---

### E4: Runbook documentation

**Goal:** A `docs/runbooks/` directory with one document per common incident.

**Files to create:**
- `docs/runbooks/ws-disconnect.md`
- `docs/runbooks/venue-503.md`
- `docs/runbooks/kill-switch.md`
- `docs/runbooks/orphaned-orders.md`
- `docs/runbooks/daily-loss-breach.md`

Each runbook covers:
- Symptoms (what the operator sees)
- Diagnostic commands (specific shell commands)
- Remediation steps
- Escalation path
- Test fixture that simulates the condition

**Effort:** 4-6 hours.

---

## Phase E Acceptance Criteria

- [ ] `curl localhost:9090/metrics` returns Prometheus output.
- [ ] All `eprintln!` replaced with `tracing` in live path.
- [ ] Risk reject fires after configured staleness window with no quotes.
- [ ] At least 5 runbooks committed.

---

## Phase F: Promotion Gate Closure

**Goal:** Strategies can be promoted from Python research to Rust live without re-auditing; parity is mechanically verified.

**Prerequisites:** Phases A-E (so the runtime behavior under test is stable).

**Capabilities unlocked:**
- New strategies can be added with confidence.
- Bundles are immutable and checksummed.
- Promotion gates are runnable, not documents.

---

### F1: Parity harness implementation

**Audit finding:** §5.5 [HIGH]

**Goal:** A test runner that loads JSON parity cases, replays events through the Rust strategy registry, and asserts emitted decisions match expected.

**Files to create:**
- `rust/crates/parity/src/json_loader.rs`
- `rust/crates/parity/src/registry_runner.rs`
- `rust/crates/parity/tests/parity_cases.rs`

**Files to modify:**
- `rust/crates/parity/Cargo.toml` — add `eventcontracts-runner`, `serde_json`.

**New API:**

```rust
// rust/crates/parity/src/json_loader.rs

pub struct JsonParityLoader {
    root: PathBuf,
}

impl JsonParityLoader {
    pub fn new(root: impl AsRef<Path>) -> Self { ... }

    /// Load every `*.json` parity case from `<root>/<strategy_name>/`.
    pub fn load_for_strategy(&self, name: &str) -> Result<Vec<ParityCase>, ParityError> { ... }
}

// rust/crates/parity/src/registry_runner.rs
pub struct StrategyRegistryParityRunner {
    registry: StrategyRegistry,
}

impl ParityRunner for StrategyRegistryParityRunner {
    fn run_case(&self, case: &ParityCase) -> Result<ParityResult, ParityError> { ... }
}
```

**Implementation steps:**

1. JSON case schema:
   ```json
   {
       "case_id": "weather_threshold/below_buy_below",
       "strategy_spec": { /* StrategySpecArtifact */ },
       "events": [ /* NormalizedEventRecord[] */ ],
       "expected_decisions": [ /* DecisionPayload[] */ ],
       "tolerance_bps": "0"
   }
   ```
2. Loader walks `contracts/parity/<strategy>/*.json`.
3. Runner: for each case, instantiate strategy via registry, replay events, compare emitted decisions to expected. Diff by field; produce `ParityResult` with list of differences.
4. Cargo test entry:
   ```rust
   #[test]
   fn parity_all_strategies() {
       let loader = JsonParityLoader::new("../../contracts/parity");
       let runner = StrategyRegistryParityRunner::new(default_registry());
       for strategy in &["weather_threshold", "sports_tennis_xgboost", "onnx_quote"] {
           let cases = loader.load_for_strategy(strategy).unwrap();
           for case in cases {
               let result = runner.run_case(&case).unwrap();
               assert!(result.passed, "{}: {:?}", case.case_id, result.differences);
           }
       }
   }
   ```

**Edge cases:**
- Floating-point in expected outputs: round per `tolerance_bps`. For Kalshi binary contracts (price in cents), tolerance can be 0.
- Strategy state mutation: each case starts with a fresh strategy instance.
- Spec doesn't match registry: surface as `ParityError::UnknownStrategy`.

**Tests:**
- `parity_loads_threshold_strategy_cases` — fixture directory; assert N cases loaded.
- `parity_run_matches_expected_decisions` — known case; assert pass.
- `parity_detects_decision_diff` — modified expected; assert fail with specific diff.

**Effort:** 8-10 hours.

---

### F2: Strategy bundle manifest + checksums

**Goal:** Every promoted strategy is a sealed bundle: spec + model + feature schema + parity cases + signed manifest.

**Files to create:**
- `rust/crates/bundle/Cargo.toml`
- `rust/crates/bundle/src/lib.rs` — manifest reader/validator.

**New API:**

```rust
pub struct BundleManifest {
    pub bundle_id: String,
    pub strategy_name: String,
    pub strategy_version: String,
    pub created_at: String,
    pub artifacts: BTreeMap<String, ArtifactRef>,  // path → checksum
    pub parity_cases: Vec<String>,
    pub signature: Option<String>,  // future: ed25519 sig
}

pub struct ArtifactRef {
    pub relative_path: String,
    pub sha256: String,
    pub size_bytes: u64,
}

impl BundleManifest {
    pub fn load(path: impl AsRef<Path>) -> Result<Self, BundleError> { ... }
    pub fn verify_checksums(&self, root: &Path) -> Result<(), BundleError> { ... }
}
```

**Implementation steps:**

1. Define manifest JSON schema.
2. Implement loader + checksum verifier.
3. `live-runner --bundle PATH` mode that loads from a bundle directory, verifies, and constructs the spec/scorer accordingly.
4. Deny mutable strategy source loads in `--live-submit` mode unless the bundle is signed (future).

**Tests:**
- `bundle_load_validates_checksums` — fixture with correct + corrupted file; correct passes, corrupted fails.
- `bundle_missing_parity_case_rejected` — manifest claims a parity case file that doesn't exist.

**Effort:** 6-8 hours.

---

## Phase F Acceptance Criteria

- [ ] `cargo test -p eventcontracts-parity` runs at least one parity case per registered strategy and passes.
- [ ] `live-runner --bundle PATH` mode works for an example bundle.
- [ ] Live-submit mode refuses to load non-bundle strategies (configurable, default deny).

---

## Out-of-scope (deferred)

These items are real gaps but explicitly not in this remediation. Track separately.

| Gap | Why deferred | When to revisit |
|---|---|---|
| Async venue submit (audit §5.1) | Sync path is correct, just slow under heavy load. We're not at that load yet. | After first month of live operation if submit count > 1/sec sustained. |
| `StrategyEvent::Trade` / `::Book` (§2.3) | No current strategy needs them. Adding is a feature, not a fix. | When a microstructure strategy is on the research roadmap. |
| Rolling features (§2.2, §3.6) | Same — feature work, not safety work. The current OnnxQuoteStrategy is a placeholder. | When a real model is trained that needs them. |
| NATS bus / multi-process (§2.1) | Single-process is sufficient until we scale beyond one sleeve. | When operations require process-level isolation. |
| Container deployment | Operationally important but doesn't gate live correctness. | Before the first multi-day unattended run. |
| Polymarket adapter | Separate venue, separate work track. | After Kalshi has been live for 1+ month. |
| Bundle signing (ed25519) | Manifest checksums are enough for v1. Signing is for multi-party trust. | When external researchers contribute strategies. |
| WASM strategy sandbox (roadmap Phase 6) | Today all strategies are first-party. | When third-party strategy code is loaded. |
| OMS Postgres backend | SQLite is enough for one sleeve. | When sleeve count > 5 or HA is required. |

---

## Go-live Definition of Done

The runner is considered live-ready when EVERY item below is true. Operator runs this checklist before flipping `--live-submit` on prod with real capital.

### Truth-from-Venue
- [ ] Fills arrive on WS and advance OMS state. Verified by manual demo run + observation that `Acked` → `Filled` happens automatically.
- [ ] Audit chain has real SHA-256 (no placeholders).
- [ ] WS reconnect demonstrably recovers from a 30s blocked connection.
- [ ] Ctrl-C during a run cancels all open orders. Verified manually.

### Safety
- [ ] Kill switch file triggers cancel-all within 1s.
- [ ] Process restart can cancel pre-restart orders.
- [ ] Injected NaN model output produces zero trades.
- [ ] Strategy re-emits after order rejection.

### State Correctness
- [ ] OMS uses exact decimal arithmetic; no `unwrap_or(0.0)` in live path.
- [ ] `apply_fill` updates `sleeve_state.positions` exactly.
- [ ] Gateway-side risk sees the same state as runner-side (single source of truth).
- [ ] OMS allows `Acked → Rejected`.

### Capital
- [ ] Ledger records every cash event to SQLite.
- [ ] `daily_realized_loss` reflects ledger truth; `max_daily_loss` risk fires at limit.
- [ ] Prediction audit chain links predictions to decisions to fills.

### Operability
- [ ] Prometheus `/metrics` returns expected counters.
- [ ] Structured JSON logs with `tracing`.
- [ ] At least 5 runbooks committed.
- [ ] Market-data age updates and risk rejects on stale data.

### Promotion
- [ ] Parity harness runs and passes for every registered strategy.
- [ ] At least one strategy loads from a signed bundle manifest.

### Real-money preconditions
- [ ] All of the above completed against Kalshi **demo**.
- [ ] One 24-hour unattended demo run with zero incidents.
- [ ] One operator drill: induce a fault (kill WS, force a rejection, hit kill-switch), observe correct response.
- [ ] Initial real-money cap explicitly configured: `--max-live-orders 5`, `--max-order-notional 10`, `--max-daily-loss 25`.
- [ ] Production credentials stored outside the repo, accessible via env vars only.
- [ ] Runbook for "first hour of real money" written and reviewed.

---

## Estimated total effort

| Phase | Effort (engineer-hours) |
|---|---|
| A: Truth-from-Venue Foundation | 23-30 |
| B: Risk Becomes Real | 19-26 |
| C: Operator Safety Net | 14-18 |
| D: Live Capital Tracking | 21-28 |
| E: Operational Visibility | 16-21 |
| F: Promotion Gate Closure | 14-18 |
| **Total** | **107-141** |

At 6 productive hours/day, that's **~18-24 engineer-days** for a single engineer with deep familiarity. With pair review and pauses, plan for **5-6 calendar weeks**.

For a more aggressive schedule with two engineers, A+B in parallel (one each) in week 1, C+D in week 2, E+F in week 3 — but each phase still needs the integration glue, so realistically 4 weeks at the floor.

---

## How to start

The very next action when picking this up:

1. **Read this document end to end.** It's long but each section is self-contained.
2. **Confirm scope.** If you disagree with anything in "Out-of-scope" or want to re-sequence, update this doc before writing code.
3. **Start at A1.** It's a small, mechanical task and unlocks A2 (which is the actual hardest task). Doing A1 first builds confidence and produces a reusable helper.
4. **Don't skip phases.** The sequencing matters. The temptation to go straight to "fill ingestion" (A2) without A1 will result in an audit chain that's still broken; you'll have to redo A1's work to make it useful.

When the runner can pass the Go-live checklist, the codebase is ready for a small-capital live deployment on Kalshi. Polymarket and other expansions stack on top.
