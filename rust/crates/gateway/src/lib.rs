//! Venue-facing gateway.
//!
//! Live invariants enforced here:
//! - approved intents only — runner's risk approval is required upstream, and
//!   this crate re-checks risk against its own snapshot before submission,
//! - idempotency by `correlation_id`,
//! - priority scheduling: `fast` is drained before `standard`, which is
//!   drained before `relaxed`; cancels jump the queue,
//! - stale-intent dropping by `max_intent_age_secs`,
//! - the `KillSwitchEngaged` risk path is never bypassed.
//!
//! The `DryRunGateway` records every command into a recorder without touching
//! the venue. The `VenueClient` trait is what a live Kalshi gateway later
//! implements; tests use `RecordingVenueClient`.

use eventcontracts_contracts::{AuditStamp, IntentEnvelopeRecord, Metadata};
pub use eventcontracts_oms::OutcomeSide;
use eventcontracts_oms::{Fill, InMemoryOms, OmsError, Order, OrderState, Side, TimeInForce};
use eventcontracts_risk::{
    outcome_position_key, utc_day_from_epoch_secs, IntentSnapshot, Position, RiskDecision,
    RiskGate, RiskRejection, SleeveState,
};
pub use eventcontracts_runtime_hot::MarketState;
use serde::{Deserialize, Serialize};
use std::collections::{HashMap, VecDeque};
use thiserror::Error;

// ---------- error ----------

#[derive(Debug, Error, PartialEq, Clone)]
pub enum GatewayError {
    #[error("idempotency conflict for correlation_id {0}")]
    Idempotency(String),
    #[error("unknown command kind {0}")]
    UnknownCommand(String),
    #[error("decision payload could not be decoded: {0}")]
    DecisionDecode(String),
    #[error("risk rejected intent: {0:?}")]
    RiskRejected(RiskRejection),
    #[error("intent dropped: stale by {age_secs}s (limit {limit_secs}s)")]
    Stale { age_secs: i64, limit_secs: i64 },
    #[error(
        "last-look rejected intent for {instrument}: {reason} \
         (intent={intent_ticks}, mark={mark_ticks:?}, age_secs={quote_age_secs:?})"
    )]
    LastLook {
        instrument: String,
        reason: &'static str,
        intent_ticks: i64,
        mark_ticks: Option<i64>,
        quote_age_secs: Option<u32>,
    },
    #[error("portfolio rejected intent: {0:?}")]
    PortfolioRejected(PortfolioRejection),
    #[error(
        "self-cross rejected for {instrument}: incoming {incoming_side:?} {incoming_ticks} \
         would cross resting order {resting_client_order_id} at {resting_ticks}"
    )]
    SelfCross {
        instrument: String,
        resting_client_order_id: String,
        incoming_side: Side,
        incoming_ticks: i64,
        resting_ticks: i64,
    },
    #[error("toxicity circuit open until epoch millisecond {until_epoch_millis}: {reason}")]
    ToxicityCircuitOpen {
        until_epoch_millis: i64,
        reason: String,
    },
    #[error("market suspended for {instrument}: state={state:?}")]
    MarketSuspended {
        instrument: String,
        state: MarketState,
    },
    #[error("oms transition failed: {0}")]
    Oms(#[from] OmsError),
    #[error("venue transport error: {0}")]
    Transport(String),
}

// ---------- decision payload ----------

fn default_outcome_side() -> OutcomeSide {
    OutcomeSide::Yes
}

/// Decision payloads the gateway accepts from a strategy. `PlaceOrder` carries
/// the full intent; `CancelOrder` only needs the client order id.
///
/// There is deliberately no `ReplaceOrder` variant (audit F5). The only live
/// sleeve — the single-taker tennis-XGBoost strategy — prices at the opposite
/// touch with `TimeInForce::Ioc`, so every order either fills immediately or is
/// killed by the venue; it never rests and therefore can never need an in-place
/// reprice. A partial-fill tail is handled by `CancelOrder`, not amendment.
/// `ReplaceOrder` would only be required by a future resting/maker quoting
/// sleeve, and adding it before such a sleeve exists would put untested
/// order-mutation code on the real-money path. The IOC-only invariant of the
/// live taker is enforced by `tennis_taker_emits_ioc_orders_only` in the runner
/// crate.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case", tag = "kind")]
pub enum DecisionPayload {
    PlaceOrder {
        client_order_id: String,
        instrument_id: String,
        #[serde(default = "default_outcome_side")]
        outcome_side: OutcomeSide,
        side: Side,
        price: String,
        quantity: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        fair_price: Option<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        min_executable_edge_ticks: Option<i64>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        fee_rate_bps: Option<u32>,
        time_in_force: TimeInForce,
    },
    CancelOrder {
        client_order_id: String,
    },
}

// ---------- priority ----------

#[derive(Clone, Copy, Debug, Eq, PartialEq, Hash, Serialize, Deserialize)]
pub enum PriorityTier {
    Fast,
    Standard,
    Relaxed,
}

impl PriorityTier {
    pub fn from_wire(s: &str) -> Result<Self, GatewayError> {
        match s {
            "fast" => Ok(PriorityTier::Fast),
            "standard" => Ok(PriorityTier::Standard),
            "relaxed" => Ok(PriorityTier::Relaxed),
            other => Err(GatewayError::DecisionDecode(format!(
                "unknown priority_tier `{other}`"
            ))),
        }
    }
}

// ---------- scheduler ----------

/// Default cap for non-cancel intent backlog. Cancels are never capped by this
/// queue, because they reduce risk and must be able to jump the line.
pub const DEFAULT_NON_CANCEL_QUEUE_CAPACITY: usize = 1024;

/// Three bounded FIFOs by tier, plus a separate cancel queue that drains
/// ahead of everything else.
#[derive(Debug)]
pub struct PriorityScheduler {
    cancels: VecDeque<IntentEnvelopeRecord>,
    fast: VecDeque<IntentEnvelopeRecord>,
    standard: VecDeque<IntentEnvelopeRecord>,
    relaxed: VecDeque<IntentEnvelopeRecord>,
    max_non_cancel_len: usize,
    shed_non_cancel_intents: u64,
}

impl Default for PriorityScheduler {
    fn default() -> Self {
        Self::with_non_cancel_capacity(DEFAULT_NON_CANCEL_QUEUE_CAPACITY)
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum EnqueueOutcome {
    Enqueued,
    DroppedOldestNonCancel {
        correlation_id: String,
        client_order_id: Option<String>,
    },
    DroppedIncomingNonCancel {
        correlation_id: String,
        client_order_id: Option<String>,
    },
}

impl PriorityScheduler {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn with_non_cancel_capacity(max_non_cancel_len: usize) -> Self {
        Self {
            cancels: VecDeque::new(),
            fast: VecDeque::new(),
            standard: VecDeque::new(),
            relaxed: VecDeque::new(),
            max_non_cancel_len,
            shed_non_cancel_intents: 0,
        }
    }

    pub fn enqueue(
        &mut self,
        envelope: IntentEnvelopeRecord,
        payload: &DecisionPayload,
    ) -> Result<EnqueueOutcome, GatewayError> {
        if matches!(payload, DecisionPayload::CancelOrder { .. }) {
            self.cancels.push_back(envelope);
            return Ok(EnqueueOutcome::Enqueued);
        }
        if self.max_non_cancel_len == 0 {
            self.shed_non_cancel_intents = self.shed_non_cancel_intents.saturating_add(1);
            return Ok(EnqueueOutcome::DroppedIncomingNonCancel {
                client_order_id: client_order_id_from_payload(payload),
                correlation_id: envelope.correlation_id,
            });
        }
        let outcome = if self.non_cancel_len() >= self.max_non_cancel_len {
            match self.pop_oldest_non_cancel() {
                Some(dropped) => {
                    self.shed_non_cancel_intents = self.shed_non_cancel_intents.saturating_add(1);
                    EnqueueOutcome::DroppedOldestNonCancel {
                        client_order_id: client_order_id_from_decision_json(&dropped.decision_json),
                        correlation_id: dropped.correlation_id,
                    }
                }
                None => {
                    self.shed_non_cancel_intents = self.shed_non_cancel_intents.saturating_add(1);
                    return Ok(EnqueueOutcome::DroppedIncomingNonCancel {
                        client_order_id: client_order_id_from_payload(payload),
                        correlation_id: envelope.correlation_id,
                    });
                }
            }
        } else {
            EnqueueOutcome::Enqueued
        };
        let tier = PriorityTier::from_wire(&envelope.priority_tier)?;
        match tier {
            PriorityTier::Fast => self.fast.push_back(envelope),
            PriorityTier::Standard => self.standard.push_back(envelope),
            PriorityTier::Relaxed => self.relaxed.push_back(envelope),
        }
        Ok(outcome)
    }

    pub fn pop_next(&mut self) -> Option<IntentEnvelopeRecord> {
        self.cancels
            .pop_front()
            .or_else(|| self.fast.pop_front())
            .or_else(|| self.standard.pop_front())
            .or_else(|| self.relaxed.pop_front())
    }

    pub fn len(&self) -> usize {
        self.cancels.len() + self.fast.len() + self.standard.len() + self.relaxed.len()
    }

    pub fn non_cancel_len(&self) -> usize {
        self.fast.len() + self.standard.len() + self.relaxed.len()
    }

    pub fn shed_non_cancel_intents(&self) -> u64 {
        self.shed_non_cancel_intents
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    fn pop_oldest_non_cancel(&mut self) -> Option<IntentEnvelopeRecord> {
        let mut oldest: Option<(PriorityTier, i64)> = None;
        for (tier, queue) in [
            (PriorityTier::Fast, &self.fast),
            (PriorityTier::Standard, &self.standard),
            (PriorityTier::Relaxed, &self.relaxed),
        ] {
            if let Some(envelope) = queue.front() {
                let emitted_at = epoch_millis(&envelope.emitted_at).unwrap_or(0);
                if oldest.is_none_or(|(_, ts)| emitted_at < ts) {
                    oldest = Some((tier, emitted_at));
                }
            }
        }
        match oldest.map(|(tier, _)| tier)? {
            PriorityTier::Fast => self.fast.pop_front(),
            PriorityTier::Standard => self.standard.pop_front(),
            PriorityTier::Relaxed => self.relaxed.pop_front(),
        }
    }
}

fn client_order_id_from_payload(payload: &DecisionPayload) -> Option<String> {
    match payload {
        DecisionPayload::PlaceOrder {
            client_order_id, ..
        }
        | DecisionPayload::CancelOrder { client_order_id } => Some(client_order_id.clone()),
    }
}

fn client_order_id_from_decision_json(raw: &str) -> Option<String> {
    let payload: DecisionPayload = serde_json::from_str(raw).ok()?;
    client_order_id_from_payload(&payload)
}

// ---------- idempotency store ----------

#[derive(Debug, Default)]
pub struct IdempotencyStore {
    seen: HashMap<String, IdempotencyRecord>,
}

#[derive(Clone, Debug)]
pub struct IdempotencyRecord {
    pub reserved_at: String,
    pub completed_at: Option<String>,
    pub ack: Option<GatewayAck>,
}

impl IdempotencyStore {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn reserve(&mut self, correlation_id: &str, now: &str) -> Result<(), GatewayError> {
        if self.seen.contains_key(correlation_id) {
            return Err(GatewayError::Idempotency(correlation_id.to_string()));
        }
        self.seen.insert(
            correlation_id.to_string(),
            IdempotencyRecord {
                reserved_at: now.to_string(),
                completed_at: None,
                ack: None,
            },
        );
        Ok(())
    }

    pub fn mark_complete(
        &mut self,
        correlation_id: &str,
        ack: GatewayAck,
        now: &str,
    ) -> Result<(), GatewayError> {
        let entry = self.seen.get_mut(correlation_id).ok_or_else(|| {
            GatewayError::Idempotency(format!(
                "mark_complete on unknown correlation_id `{correlation_id}`"
            ))
        })?;
        entry.completed_at = Some(now.to_string());
        entry.ack = Some(ack);
        Ok(())
    }

    pub fn record(&self, correlation_id: &str) -> Option<&IdempotencyRecord> {
        self.seen.get(correlation_id)
    }

    /// Evict records older than `cutoff` (RFC3339). Comparison is by parsed
    /// epoch seconds, not lexical string compare — RFC3339 strings with
    /// different fractional/offset shapes don't sort correctly as strings
    /// (e.g., `2026-05-27T12:00:00Z` vs `2026-05-27T12:00:00.500Z` or
    /// `+00:00`). Numeric epochs are robust.
    pub fn expire_older_than(&mut self, cutoff: &str) {
        let cutoff_epoch = epoch_seconds(cutoff).unwrap_or(i64::MAX);
        self.seen.retain(|_, record| {
            // Keep when either timestamp is at or after the cutoff epoch.
            let reserved = epoch_seconds(&record.reserved_at).unwrap_or(0);
            if reserved >= cutoff_epoch {
                return true;
            }
            match record.completed_at.as_deref() {
                Some(c) => epoch_seconds(c).unwrap_or(0) >= cutoff_epoch,
                None => false,
            }
        });
    }
}

// ---------- ack & venue client ----------

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct GatewayAck {
    pub correlation_id: String,
    pub accepted: bool,
    pub acknowledged_at: String,
    pub venue_order_id: Option<String>,
    pub reasons: Vec<String>,
}

pub trait VenueClient {
    fn submit(
        &mut self,
        envelope: &IntentEnvelopeRecord,
        payload: &DecisionPayload,
    ) -> Result<GatewayAck, GatewayError>;
    fn cancel(
        &mut self,
        envelope: &IntentEnvelopeRecord,
        client_order_id: &str,
    ) -> Result<GatewayAck, GatewayError>;
    fn cancel_all(&mut self, now: &str) -> Result<Vec<GatewayAck>, GatewayError> {
        Err(GatewayError::Transport(format!(
            "cancel_all not implemented at {now}"
        )))
    }
}

/// Async counterpart to [`VenueClient`].
///
/// The async path is what the live runner uses so a slow REST submit
/// yields to the runtime rather than blocking the WS task. Implementations
/// must NOT call `block_in_place`/`block_on`; instead they `.await` the
/// underlying transport directly.
///
/// `RecordingVenueClient` implements both traits so existing sync tests
/// continue to pass while the live runner can call the async path.
///
/// `#[async_trait]` boxes the returned futures so the trait is
/// `dyn`-compatible — the live runner uses `Box<dyn LiveVenueClient + Send>`
/// to pick between Kalshi and the paper recorder at runtime.
#[async_trait::async_trait]
pub trait AsyncVenueClient {
    async fn submit_async(
        &mut self,
        envelope: &IntentEnvelopeRecord,
        payload: &DecisionPayload,
    ) -> Result<GatewayAck, GatewayError>;

    async fn cancel_async(
        &mut self,
        envelope: &IntentEnvelopeRecord,
        client_order_id: &str,
    ) -> Result<GatewayAck, GatewayError>;

    async fn cancel_all_async(&mut self, now: &str) -> Result<Vec<GatewayAck>, GatewayError> {
        Err(GatewayError::Transport(format!(
            "cancel_all_async not implemented at {now}"
        )))
    }
}

/// Combined trait alias for the live runner: a venue client that supports
/// both sync (startup reconciliation, shutdown bulk cancel) and async
/// (hot-path submit/cancel) dispatch. Any type that implements both
/// `VenueClient` and `AsyncVenueClient` automatically satisfies this.
pub trait LiveVenueClient: VenueClient + AsyncVenueClient + Send {}
impl<T: VenueClient + AsyncVenueClient + Send + ?Sized> LiveVenueClient for T {}

#[async_trait::async_trait]
impl<T: AsyncVenueClient + ?Sized + Send> AsyncVenueClient for Box<T> {
    async fn submit_async(
        &mut self,
        envelope: &IntentEnvelopeRecord,
        payload: &DecisionPayload,
    ) -> Result<GatewayAck, GatewayError> {
        (**self).submit_async(envelope, payload).await
    }
    async fn cancel_async(
        &mut self,
        envelope: &IntentEnvelopeRecord,
        client_order_id: &str,
    ) -> Result<GatewayAck, GatewayError> {
        (**self).cancel_async(envelope, client_order_id).await
    }
    async fn cancel_all_async(&mut self, now: &str) -> Result<Vec<GatewayAck>, GatewayError> {
        (**self).cancel_all_async(now).await
    }
}

/// Blanket impl that lets `Box<dyn VenueClient + Send>` satisfy the trait
/// bound, so a single `DryRunGateway` type can hold either a paper recorder
/// or a real Kalshi client picked at runtime by `live-runner --live-submit`.
impl<T: VenueClient + ?Sized> VenueClient for Box<T> {
    fn submit(
        &mut self,
        envelope: &IntentEnvelopeRecord,
        payload: &DecisionPayload,
    ) -> Result<GatewayAck, GatewayError> {
        (**self).submit(envelope, payload)
    }
    fn cancel(
        &mut self,
        envelope: &IntentEnvelopeRecord,
        client_order_id: &str,
    ) -> Result<GatewayAck, GatewayError> {
        (**self).cancel(envelope, client_order_id)
    }
    fn cancel_all(&mut self, now: &str) -> Result<Vec<GatewayAck>, GatewayError> {
        (**self).cancel_all(now)
    }
}

/// A `VenueClient` that records every call instead of sending. Used by the
/// dry-run gateway and by tests.
#[derive(Debug, Default)]
pub struct RecordingVenueClient {
    pub submitted: Vec<(IntentEnvelopeRecord, DecisionPayload)>,
    pub canceled: Vec<(IntentEnvelopeRecord, String)>,
    next_venue_id: u64,
}

impl RecordingVenueClient {
    pub fn new() -> Self {
        Self::default()
    }
}

#[async_trait::async_trait]
impl AsyncVenueClient for RecordingVenueClient {
    async fn submit_async(
        &mut self,
        envelope: &IntentEnvelopeRecord,
        payload: &DecisionPayload,
    ) -> Result<GatewayAck, GatewayError> {
        self.submit(envelope, payload)
    }
    async fn cancel_async(
        &mut self,
        envelope: &IntentEnvelopeRecord,
        client_order_id: &str,
    ) -> Result<GatewayAck, GatewayError> {
        self.cancel(envelope, client_order_id)
    }
    async fn cancel_all_async(&mut self, now: &str) -> Result<Vec<GatewayAck>, GatewayError> {
        self.cancel_all(now)
    }
}

impl VenueClient for RecordingVenueClient {
    fn submit(
        &mut self,
        envelope: &IntentEnvelopeRecord,
        payload: &DecisionPayload,
    ) -> Result<GatewayAck, GatewayError> {
        self.submitted.push((envelope.clone(), payload.clone()));
        self.next_venue_id += 1;
        Ok(GatewayAck {
            correlation_id: envelope.correlation_id.clone(),
            accepted: true,
            acknowledged_at: envelope.emitted_at.clone(),
            venue_order_id: Some(format!("dry-run-{:08}", self.next_venue_id)),
            reasons: vec![],
        })
    }

    fn cancel(
        &mut self,
        envelope: &IntentEnvelopeRecord,
        client_order_id: &str,
    ) -> Result<GatewayAck, GatewayError> {
        self.canceled
            .push((envelope.clone(), client_order_id.to_string()));
        Ok(GatewayAck {
            correlation_id: envelope.correlation_id.clone(),
            accepted: true,
            acknowledged_at: envelope.emitted_at.clone(),
            venue_order_id: None,
            reasons: vec![],
        })
    }

    fn cancel_all(&mut self, now: &str) -> Result<Vec<GatewayAck>, GatewayError> {
        let ids: Vec<String> = self
            .submitted
            .iter()
            .filter_map(|(_, payload)| match payload {
                DecisionPayload::PlaceOrder {
                    client_order_id, ..
                } => Some(client_order_id.clone()),
                DecisionPayload::CancelOrder { .. } => None,
            })
            .collect();
        Ok(ids
            .into_iter()
            .map(|client_order_id| GatewayAck {
                correlation_id: format!("cancel-all-{client_order_id}"),
                accepted: true,
                acknowledged_at: now.to_string(),
                venue_order_id: None,
                reasons: vec!["recording_cancel_all".into()],
            })
            .collect())
    }
}

// ---------- dry-run gateway ----------

pub struct DryRunGateway<C: VenueClient> {
    pub scheduler: PriorityScheduler,
    pub idempotency: IdempotencyStore,
    pub oms: InMemoryOms,
    pub risk: RiskGate,
    pub sleeve_state: SleeveState,
    pub venue: C,
    pub ledger: Vec<LedgerEntry>,
    pub rate_budget: RateBudget,
    pub max_intent_age_secs: i64,
    pub max_cancel_age_secs: i64,
    pub last_look: LastLookConfig,
    pub portfolio: PortfolioGuard,
    pub toxicity: ToxicityCircuitBreaker,
    /// Per-instrument administrative market state from the venue's
    /// lifecycle feed. An entry is required before a `PlaceOrder` may be
    /// submitted on that instrument when [`Self::require_market_state`]
    /// is `true`; otherwise the absence of an entry means "unknown — let
    /// it through" (legacy behavior, suitable for paper).
    pub market_state: HashMap<String, MarketState>,
    /// When `true`, a `PlaceOrder` for an instrument with no recorded
    /// market state is rejected with `MarketSuspended { state: Listed }`.
    /// Live submit MUST enable this once the lifecycle subscription is
    /// known to be up.
    pub require_market_state: bool,
}

/// Prepared work returned from [`DryRunGateway::prepare_one`]. The
/// gateway has already run every pre-venue check, opened the OMS for
/// places, and reserved idempotency; the caller dispatches the venue
/// call (sync or async) then feeds the result back through
/// `finalize_place` / `finalize_cancel`.
#[derive(Clone, Debug)]
pub enum PreparedExecution {
    Place {
        envelope: IntentEnvelopeRecord,
        payload_for_venue: DecisionPayload,
        client_order_id: String,
    },
    Cancel {
        envelope: IntentEnvelopeRecord,
        client_order_id: String,
    },
}

/// Outcome of [`DryRunGateway::apply_market_state`]. The list of
/// `canceled_client_order_ids` is the set of orders that were enqueued for
/// cancel because the market transitioned to a non-tradable state — the
/// caller must drain them with `process_batch` to actually transmit.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct MarketStateOutcome {
    pub previous: Option<MarketState>,
    pub current: MarketState,
    /// Open `client_order_id`s scheduled for cancel because the venue moved
    /// to a non-tradable state.
    pub canceled_client_order_ids: Vec<String>,
    /// `true` when the transition was non-tradable → tradable; the gateway
    /// invalidated its cached BBO/mark for the instrument so a fresh quote
    /// is required before the next placement clears risk.
    pub bbo_invalidated_for_reopen: bool,
}

/// Send-time "last look" validation. Just before `venue.submit`, recheck
/// that the book hasn't moved or gone stale since the strategy decided.
///
/// All thresholds are in 4-decimal price ticks (1 tick = $0.0001) so they
/// match `SleeveState.mark_price_ticks`. Disable a check by setting its
/// threshold to `i64::MAX` or `u32::MAX`.
#[derive(Clone, Copy, Debug)]
pub struct LastLookConfig {
    /// Max absolute movement of mark vs intent price since the strategy
    /// emitted the intent, in 4-decimal ticks. Default 200 ticks = 2¢.
    pub max_mark_movement_ticks: i64,
    /// Max age of the latest BBO at submit time, in seconds. Default 5s.
    pub max_quote_age_secs: u32,
    /// Whether to require a mark price exists at all. Default true.
    pub require_mark_price: bool,
    /// Whether submit-time validation must use an executable side-specific
    /// BBO rather than falling back to mark. Live submit should enable this.
    pub require_executable_bbo: bool,
    /// Whether an immediately executable order must fit inside displayed L1
    /// quantity. Live submit should enable this once book data is flowing.
    pub require_l1_depth: bool,
}

impl Default for LastLookConfig {
    fn default() -> Self {
        Self {
            max_mark_movement_ticks: 200,
            max_quote_age_secs: 5,
            require_mark_price: true,
            require_executable_bbo: false,
            require_l1_depth: false,
        }
    }
}

#[derive(Clone, Debug)]
pub struct ToxicityCircuitBreaker {
    pub enabled: bool,
    pub max_fills: usize,
    pub window_millis: i64,
    pub cooldown_millis: i64,
    fills: VecDeque<i64>,
    tripped_until_epoch_millis: Option<i64>,
    last_reason: Option<String>,
}

impl Default for ToxicityCircuitBreaker {
    fn default() -> Self {
        Self {
            enabled: false,
            max_fills: 20,
            window_millis: 1_000,
            cooldown_millis: 30_000,
            fills: VecDeque::new(),
            tripped_until_epoch_millis: None,
            last_reason: None,
        }
    }
}

impl ToxicityCircuitBreaker {
    pub fn disabled() -> Self {
        Self::default()
    }

    pub fn enabled(max_fills: usize, window_millis: i64, cooldown_millis: i64) -> Self {
        Self {
            enabled: true,
            max_fills,
            window_millis: window_millis.max(1),
            cooldown_millis: cooldown_millis.max(1),
            fills: VecDeque::new(),
            tripped_until_epoch_millis: None,
            last_reason: None,
        }
    }

    pub fn record_fill(&mut self, fill_epoch_millis: i64) -> Option<String> {
        if !self.enabled || self.max_fills == 0 {
            return None;
        }
        let window_start = fill_epoch_millis.saturating_sub(self.window_millis);
        while self
            .fills
            .front()
            .is_some_and(|oldest| *oldest < window_start)
        {
            self.fills.pop_front();
        }
        self.fills.push_back(fill_epoch_millis);
        if self.fills.len() >= self.max_fills {
            let reason = format!(
                "fill_velocity {} fills in {}ms",
                self.fills.len(),
                self.window_millis
            );
            self.tripped_until_epoch_millis =
                Some(fill_epoch_millis.saturating_add(self.cooldown_millis));
            self.last_reason = Some(reason.clone());
            return Some(reason);
        }
        None
    }

    pub fn reject_reason(&self, now_epoch_millis: i64) -> Option<(i64, String)> {
        let until = self.tripped_until_epoch_millis?;
        if now_epoch_millis <= until {
            return Some((
                until,
                self.last_reason
                    .clone()
                    .unwrap_or_else(|| "toxicity_circuit".into()),
            ));
        }
        None
    }
}

// ---------- portfolio admission ----------

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum PortfolioRejection {
    TotalGrossExceeded {
        projected: String,
        limit: String,
    },
    GroupGrossExceeded {
        group: String,
        projected: String,
        limit: String,
    },
}

#[derive(Clone, Debug)]
pub struct PortfolioPolicy {
    pub enabled: bool,
    max_total_gross_ticks: i128,
    total_limit_label: String,
    group_limits: HashMap<String, (i128, String)>,
    instrument_groups: HashMap<String, String>,
    prefix_groups: Vec<(String, String)>,
}

impl Default for PortfolioPolicy {
    fn default() -> Self {
        Self {
            enabled: false,
            max_total_gross_ticks: i128::MAX,
            total_limit_label: "disabled".into(),
            group_limits: HashMap::new(),
            instrument_groups: HashMap::new(),
            prefix_groups: Vec::new(),
        }
    }
}

impl PortfolioPolicy {
    pub fn disabled() -> Self {
        Self::default()
    }

    pub fn enabled(max_total_gross: impl Into<String>) -> Result<Self, GatewayError> {
        let label = max_total_gross.into();
        let limit = parse_decimal_ticks_i128(&label).map_err(|_| {
            GatewayError::DecisionDecode(format!("invalid portfolio max gross `{label}`"))
        })?;
        Ok(Self {
            enabled: true,
            max_total_gross_ticks: limit,
            total_limit_label: label,
            group_limits: HashMap::new(),
            instrument_groups: HashMap::new(),
            prefix_groups: Vec::new(),
        })
    }

    pub fn with_group_limit(
        mut self,
        group: impl Into<String>,
        limit: impl Into<String>,
    ) -> Result<Self, GatewayError> {
        let group = group.into();
        let label = limit.into();
        let ticks = parse_decimal_ticks_i128(&label).map_err(|_| {
            GatewayError::DecisionDecode(format!("invalid portfolio group limit `{label}`"))
        })?;
        self.group_limits.insert(group, (ticks, label));
        Ok(self)
    }

    pub fn with_instrument_group(
        mut self,
        instrument_id: impl Into<String>,
        group: impl Into<String>,
    ) -> Self {
        self.instrument_groups
            .insert(instrument_id.into(), group.into());
        self
    }

    pub fn with_prefix_group(
        mut self,
        instrument_prefix: impl Into<String>,
        group: impl Into<String>,
    ) -> Self {
        self.prefix_groups
            .push((instrument_prefix.into(), group.into()));
        self
    }

    fn group_for(&self, instrument_id: &str) -> String {
        if let Some(group) = self.instrument_groups.get(instrument_id) {
            return group.clone();
        }
        self.prefix_groups
            .iter()
            .find_map(|(prefix, group)| instrument_id.starts_with(prefix).then(|| group.clone()))
            .unwrap_or_else(|| "ungrouped".into())
    }
}

#[derive(Clone, Debug, Default)]
pub struct PortfolioGuard {
    pub policy: PortfolioPolicy,
    open_order_reservations: HashMap<String, PortfolioReservation>,
}

#[derive(Clone, Debug)]
struct PortfolioReservation {
    instrument_id: String,
    outcome_side: OutcomeSide,
    side: Side,
    price_ticks: i128,
    remaining_qty_ticks: i128,
}

#[derive(Clone, Copy)]
struct PortfolioOrderProjection<'a> {
    instrument_id: &'a str,
    outcome_side: OutcomeSide,
    side: Side,
    price_ticks: i128,
    quantity_ticks: i128,
}

impl PortfolioGuard {
    pub fn disabled() -> Self {
        Self::default()
    }

    pub fn new(policy: PortfolioPolicy) -> Self {
        Self {
            policy,
            open_order_reservations: HashMap::new(),
        }
    }

    pub fn sync_open_orders<'a, I>(&mut self, orders: I) -> Result<(), GatewayError>
    where
        I: IntoIterator<Item = &'a Order>,
    {
        self.open_order_reservations.clear();
        for order in orders {
            let price = parse_decimal_ticks_i128(&order.price).map_err(|_| {
                GatewayError::DecisionDecode(format!("invalid order price `{}`", order.price))
            })?;
            let quantity = parse_decimal_ticks_i128(&order.quantity).map_err(|_| {
                GatewayError::DecisionDecode(format!("invalid order quantity `{}`", order.quantity))
            })?;
            let filled = parse_decimal_ticks_i128(&order.filled_quantity).map_err(|_| {
                GatewayError::DecisionDecode(format!(
                    "invalid filled quantity `{}`",
                    order.filled_quantity
                ))
            })?;
            let remaining = quantity.saturating_sub(filled).max(0);
            let notional = mul_ticks(price.abs(), remaining.abs());
            if notional > 0 {
                self.open_order_reservations.insert(
                    order.client_order_id.clone(),
                    PortfolioReservation {
                        instrument_id: order.instrument_id.clone(),
                        outcome_side: order.outcome_side,
                        side: order.side,
                        price_ticks: price,
                        remaining_qty_ticks: remaining,
                    },
                );
            }
        }
        Ok(())
    }

    pub fn evaluate(
        &self,
        state: &SleeveState,
        intent: &IntentSnapshot,
    ) -> Result<(), PortfolioRejection> {
        if !self.policy.enabled {
            return Ok(());
        }
        let price = parse_decimal_ticks_i128(&intent.price).unwrap_or(i128::MAX);
        let quantity = parse_decimal_ticks_i128(&intent.quantity).unwrap_or(i128::MAX);
        let projection = PortfolioOrderProjection {
            instrument_id: &intent.instrument_id,
            outcome_side: intent.outcome_side,
            side: intent.side,
            price_ticks: price,
            quantity_ticks: quantity,
        };
        let total = projected_portfolio_gross(
            &self.policy,
            state,
            &self.open_order_reservations,
            Some(projection),
        );
        if total > self.policy.max_total_gross_ticks {
            return Err(PortfolioRejection::TotalGrossExceeded {
                projected: format_decimal_ticks_i128(total),
                limit: self.policy.total_limit_label.clone(),
            });
        }

        let group = self.policy.group_for(&intent.instrument_id);
        if let Some((limit, label)) = self.policy.group_limits.get(&group) {
            let projected_group = projected_group_gross(
                &self.policy,
                state,
                &self.open_order_reservations,
                &group,
                Some(projection),
            );
            if projected_group > *limit {
                return Err(PortfolioRejection::GroupGrossExceeded {
                    group,
                    projected: format_decimal_ticks_i128(projected_group),
                    limit: label.clone(),
                });
            }
        }
        Ok(())
    }

    pub fn evaluate_current_state(&self, state: &SleeveState) -> Result<(), PortfolioRejection> {
        if !self.policy.enabled {
            return Ok(());
        }
        let total =
            projected_portfolio_gross(&self.policy, state, &self.open_order_reservations, None);
        if total > self.policy.max_total_gross_ticks {
            return Err(PortfolioRejection::TotalGrossExceeded {
                projected: format_decimal_ticks_i128(total),
                limit: self.policy.total_limit_label.clone(),
            });
        }
        for (group, (limit, label)) in &self.policy.group_limits {
            let projected_group = projected_group_gross(
                &self.policy,
                state,
                &self.open_order_reservations,
                group,
                None,
            );
            if projected_group > *limit {
                return Err(PortfolioRejection::GroupGrossExceeded {
                    group: group.clone(),
                    projected: format_decimal_ticks_i128(projected_group),
                    limit: label.clone(),
                });
            }
        }
        Ok(())
    }
}

// ---------- reconciliation/adoption ----------

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RestingOrderSnapshot {
    pub client_order_id: String,
    pub venue_order_id: Option<String>,
    pub instrument_id: String,
    pub outcome_side: OutcomeSide,
    pub side: Side,
    pub price: String,
    pub quantity: String,
    pub filled_quantity: String,
    pub time_in_force: TimeInForce,
    pub state: OrderState,
    pub updated_at: String,
    /// Wall-clock time at which this venue snapshot was observed by the
    /// reconciliation reader. `updated_at` can be hours old for a resting
    /// order; adoption freshness must be based on when we saw venue truth.
    pub observed_at: String,
    pub reject_reason: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct LedgerEntry {
    pub ts: String,
    pub client_order_id: String,
    pub instrument_id: String,
    pub outcome_side: OutcomeSide,
    pub position_delta: i64,
    pub cash_delta_ticks: i64,
    pub fee_ticks: i64,
}

#[derive(Clone, Debug)]
pub struct RateBudget {
    pub max_orders_per_second: u32,
    pub max_cancels_per_second: u32,
    order_timestamps: VecDeque<i64>,
    cancel_timestamps: VecDeque<i64>,
}

impl Default for RateBudget {
    fn default() -> Self {
        Self {
            max_orders_per_second: 5,
            max_cancels_per_second: 10,
            order_timestamps: VecDeque::new(),
            cancel_timestamps: VecDeque::new(),
        }
    }
}

impl RateBudget {
    fn check(&mut self, is_cancel: bool, now: &str) -> Result<(), GatewayError> {
        let current = epoch_seconds(now).unwrap_or(0);
        let (limit, timestamps, action) = if is_cancel {
            (
                self.max_cancels_per_second,
                &mut self.cancel_timestamps,
                "cancel",
            )
        } else {
            (
                self.max_orders_per_second,
                &mut self.order_timestamps,
                "order",
            )
        };
        while timestamps
            .front()
            .is_some_and(|ts| current.saturating_sub(*ts) >= 1)
        {
            timestamps.pop_front();
        }
        if timestamps.len() as u32 >= limit {
            return Err(GatewayError::RiskRejected(
                RiskRejection::RateLimitExceeded {
                    action: action.into(),
                    limit_per_second: limit,
                },
            ));
        }
        timestamps.push_back(current);
        Ok(())
    }
}

impl<C: VenueClient> DryRunGateway<C> {
    pub fn new(risk: RiskGate, venue: C) -> Self {
        Self {
            scheduler: PriorityScheduler::new(),
            idempotency: IdempotencyStore::new(),
            oms: InMemoryOms::new(),
            risk,
            sleeve_state: SleeveState::default(),
            venue,
            ledger: Vec::new(),
            rate_budget: RateBudget::default(),
            max_intent_age_secs: 30,
            max_cancel_age_secs: 5,
            last_look: LastLookConfig::default(),
            portfolio: PortfolioGuard::disabled(),
            toxicity: ToxicityCircuitBreaker::disabled(),
            market_state: HashMap::new(),
            require_market_state: false,
        }
    }

    /// Apply a venue lifecycle transition. Returns the previous + current
    /// state and the list of own orders enqueued for cancel because the
    /// market moved to a non-tradable state.
    ///
    /// Semantics:
    /// - On any transition into a non-tradable state, every own open order
    ///   on the affected instrument is enqueued as a high-priority cancel.
    ///   Resting orders cannot remain live against a paused/closed market.
    /// - On a tradable → non-tradable transition the cached BBO and mark
    ///   for the instrument are wiped so a stale quote cannot drive a
    ///   later trade if the market re-opens.
    /// - On a non-tradable → tradable transition the BBO cache is also
    ///   wiped: the gateway's last-look will require a fresh quote before
    ///   the next placement.
    pub fn apply_market_state(
        &mut self,
        instrument_id: &str,
        state: MarketState,
        reason: Option<&str>,
        now: &str,
    ) -> Result<MarketStateOutcome, GatewayError> {
        if state == MarketState::MetadataUpdated {
            return Ok(MarketStateOutcome {
                previous: self.market_state.get(instrument_id).copied(),
                current: state,
                canceled_client_order_ids: Vec::new(),
                bbo_invalidated_for_reopen: false,
            });
        }
        let previous = self.market_state.insert(instrument_id.to_string(), state);
        let was_tradable = previous.map(MarketState::is_tradable);

        let mut canceled = Vec::new();
        let mut bbo_invalidated_for_reopen = false;

        if !state.is_tradable() {
            // Cancel every open order on this instrument. We collect the
            // ids first so we don't hold an immutable borrow of `self.oms`
            // across the mutable enqueue calls.
            let to_cancel: Vec<String> = self
                .oms
                .open_orders()
                .filter(|order| order.instrument_id == instrument_id)
                .map(|order| order.client_order_id.clone())
                .collect();
            for client_order_id in to_cancel {
                let envelope = synthesize_market_state_cancel(&client_order_id, reason, now)?;
                let payload = DecisionPayload::CancelOrder {
                    client_order_id: client_order_id.clone(),
                };
                self.scheduler.enqueue(envelope, &payload)?;
                canceled.push(client_order_id);
            }
            // Wipe cached BBO/mark for the instrument so any later quote
            // staleness check fires correctly on re-open.
            self.invalidate_market_data(instrument_id);
        } else if matches!(was_tradable, Some(false)) {
            // Non-tradable → tradable. Require a fresh quote before the
            // gateway's last-look will pass: clear cached BBO/mark.
            self.invalidate_market_data(instrument_id);
            bbo_invalidated_for_reopen = true;
        }

        Ok(MarketStateOutcome {
            previous,
            current: state,
            canceled_client_order_ids: canceled,
            bbo_invalidated_for_reopen,
        })
    }

    fn invalidate_market_data(&mut self, instrument_id: &str) {
        // Both bare-instrument and outcome-keyed entries; the keys for
        // outcome-side maps are produced by `outcome_position_key` which
        // prefixes the instrument id with a `|` separator.
        let prefix = format!("{instrument_id}|");
        self.sleeve_state
            .last_quote_epoch_secs
            .retain(|key, _| key != instrument_id && !key.starts_with(&prefix));
        self.sleeve_state
            .mark_price_ticks
            .retain(|key, _| key != instrument_id && !key.starts_with(&prefix));
        self.sleeve_state
            .best_bid_ticks
            .retain(|key, _| key != instrument_id && !key.starts_with(&prefix));
        self.sleeve_state
            .best_ask_ticks
            .retain(|key, _| key != instrument_id && !key.starts_with(&prefix));
        self.sleeve_state
            .best_bid_qty_ticks
            .retain(|key, _| key != instrument_id && !key.starts_with(&prefix));
        self.sleeve_state
            .best_ask_qty_ticks
            .retain(|key, _| key != instrument_id && !key.starts_with(&prefix));
    }

    pub fn enqueue(
        &mut self,
        envelope: IntentEnvelopeRecord,
    ) -> Result<EnqueueOutcome, GatewayError> {
        let payload: DecisionPayload = serde_json::from_str(&envelope.decision_json)
            .map_err(|e| GatewayError::DecisionDecode(e.to_string()))?;
        self.scheduler.enqueue(envelope, &payload)
    }

    /// Drain up to `limit` items, applying full processing pipeline. Returns
    /// the list of acks paired with the originating correlation_id.
    pub fn process_batch(
        &mut self,
        now: &str,
        limit: usize,
    ) -> Vec<(String, Result<GatewayAck, GatewayError>)> {
        let mut out = Vec::with_capacity(limit);
        for _ in 0..limit {
            let Some(envelope) = self.scheduler.pop_next() else {
                break;
            };
            let cid = envelope.correlation_id.clone();
            let result = self.process_one(envelope, now);
            out.push((cid, result));
        }
        out
    }

    fn process_one(
        &mut self,
        envelope: IntentEnvelopeRecord,
        now: &str,
    ) -> Result<GatewayAck, GatewayError> {
        match self.prepare_one(envelope, now)? {
            PreparedExecution::Place {
                envelope,
                payload_for_venue,
                client_order_id,
            } => {
                let result = self.venue.submit(&envelope, &payload_for_venue);
                self.finalize_place(
                    &client_order_id,
                    &envelope.correlation_id,
                    &envelope.emitted_at,
                    result,
                    now,
                )
            }
            PreparedExecution::Cancel {
                envelope,
                client_order_id,
            } => {
                let result = self.venue.cancel(&envelope, &client_order_id);
                self.finalize_cancel(&envelope.correlation_id, result, now)
            }
        }
    }

    /// Run every pre-venue check (rate budget, staleness, idempotency,
    /// toxicity, market state, risk, portfolio, self-cross, last-look) and
    /// — for places — open the OMS and transition to `Submitted`. Returns
    /// the data the caller needs to dispatch the actual venue call.
    ///
    /// Shared by both the sync `process_one` and the async
    /// `process_one_async` so the prep pipeline lives in one place.
    fn prepare_one(
        &mut self,
        envelope: IntentEnvelopeRecord,
        now: &str,
    ) -> Result<PreparedExecution, GatewayError> {
        let payload: DecisionPayload = serde_json::from_str(&envelope.decision_json)
            .map_err(|e| GatewayError::DecisionDecode(e.to_string()))?;

        let is_cancel = matches!(payload, DecisionPayload::CancelOrder { .. });
        self.rate_budget.check(is_cancel, now)?;
        // P3: TTL precedence is
        //   (1) envelope.audit.metadata["expires_after_ms"] if present,
        //   (2) per-tier gateway default (fast=5s, standard/relaxed=30s),
        //   (3) hard fall-back to `max_intent_age_secs` / `max_cancel_age_secs`.
        // Cancels keep their own short ceiling regardless.
        let max_age_secs: i64 = if is_cancel {
            self.max_cancel_age_secs
        } else if let Some(ms) = envelope
            .audit
            .metadata
            .get("expires_after_ms")
            .and_then(|v| v.parse::<u64>().ok())
        {
            (ms.saturating_div(1000) as i64).max(1)
        } else {
            match envelope.priority_tier.as_str() {
                "fast" => 5,
                _ => self.max_intent_age_secs,
            }
        };
        let age = age_secs(&envelope.emitted_at, now);
        if age > max_age_secs {
            return Err(GatewayError::Stale {
                age_secs: age,
                limit_secs: max_age_secs,
            });
        }

        self.idempotency.reserve(&envelope.correlation_id, now)?;

        match payload {
            DecisionPayload::PlaceOrder {
                client_order_id,
                instrument_id,
                outcome_side,
                side,
                price,
                quantity,
                fair_price,
                min_executable_edge_ticks,
                fee_rate_bps,
                time_in_force,
            } => {
                let snap = IntentSnapshot {
                    client_order_id: client_order_id.clone(),
                    instrument_id: instrument_id.clone(),
                    outcome_side,
                    side,
                    price: price.clone(),
                    quantity: quantity.clone(),
                    fair_price: fair_price.clone(),
                    min_executable_edge_ticks,
                    fee_rate_bps,
                };
                let now_epoch = epoch_seconds(now).unwrap_or(0);
                if let Some((until_epoch_millis, reason)) =
                    self.toxicity.reject_reason(epoch_millis(now).unwrap_or(0))
                {
                    return Err(GatewayError::ToxicityCircuitOpen {
                        until_epoch_millis,
                        reason,
                    });
                }
                // Market lifecycle gate. An explicit non-tradable state
                // blocks new placements unconditionally. An absent state
                // entry is treated as "unknown — fall through" unless the
                // operator opted in to strict mode via `require_market_state`,
                // in which case it's treated as `Listed` (pre-trading).
                match self.market_state.get(&instrument_id).copied() {
                    Some(state) if !state.is_tradable() => {
                        return Err(GatewayError::MarketSuspended {
                            instrument: instrument_id,
                            state,
                        });
                    }
                    None if self.require_market_state => {
                        return Err(GatewayError::MarketSuspended {
                            instrument: instrument_id,
                            state: MarketState::Listed,
                        });
                    }
                    _ => {}
                }
                match self.risk.evaluate(&self.sleeve_state, &snap, now_epoch) {
                    RiskDecision::Approved => {}
                    RiskDecision::Rejected(reason) => {
                        return Err(GatewayError::RiskRejected(reason));
                    }
                }
                if let Err(reason) = self.portfolio.evaluate(&self.sleeve_state, &snap) {
                    return Err(GatewayError::PortfolioRejected(reason));
                }
                self.self_cross_check(&instrument_id, outcome_side, side, &price)?;
                // P2: send-time last-look fires BEFORE any OMS mutation so
                // a stale-book reject leaves the OMS clean. Risk has already
                // approved; the only thing that can change between risk and
                // submit is the book itself.
                self.last_look_check(&instrument_id, outcome_side, side, &price, &quantity, now)?;
                let order = Order::new_with_outcome(
                    &client_order_id,
                    &instrument_id,
                    outcome_side,
                    side,
                    &price,
                    &quantity,
                    time_in_force,
                    &envelope.emitted_at,
                )?;
                self.oms.open_order(order)?;
                self.oms.transition(
                    &client_order_id,
                    OrderState::Submitted,
                    &envelope.emitted_at,
                    None,
                )?;
                let payload_for_venue = DecisionPayload::PlaceOrder {
                    client_order_id: client_order_id.clone(),
                    instrument_id,
                    outcome_side,
                    side,
                    price,
                    quantity,
                    fair_price,
                    min_executable_edge_ticks,
                    fee_rate_bps,
                    time_in_force,
                };
                Ok(PreparedExecution::Place {
                    envelope,
                    payload_for_venue,
                    client_order_id,
                })
            }
            DecisionPayload::CancelOrder { client_order_id } => Ok(PreparedExecution::Cancel {
                envelope,
                client_order_id,
            }),
        }
    }

    /// Apply post-submit OMS transitions and complete the idempotency
    /// reservation. Mirrors the tail of the original `process_one` so the
    /// sync and async paths converge on identical state changes.
    fn finalize_place(
        &mut self,
        client_order_id: &str,
        correlation_id: &str,
        emitted_at: &str,
        result: Result<GatewayAck, GatewayError>,
        now: &str,
    ) -> Result<GatewayAck, GatewayError> {
        let _ = emitted_at; // kept for future hooks (e.g., audit annotations).
        let ack = match result {
            Ok(ack) => ack,
            Err(err) => {
                let reason = format!("submit_transport_unknown:{err}");
                self.oms.transition(
                    client_order_id,
                    OrderState::SubmitUnknown,
                    now,
                    Some(&reason),
                )?;
                self.sync_runtime_state()?;
                return Err(err);
            }
        };
        if ack.accepted {
            if let Some(venue_id) = &ack.venue_order_id {
                self.oms.set_venue_order_id(client_order_id, venue_id)?;
            }
            // N4: HTTP 2xx from submit only means "transmitted / received",
            // not "resting in book". The order stays `Submitted` until the
            // venue's own_order channel confirms via apply_order_update
            // ("resting"/"open"/...). This matches venue truth and prevents
            // OMS state from diverging if the matching engine rejects in
            // flight.
        } else {
            self.oms.transition(
                client_order_id,
                OrderState::Rejected,
                &ack.acknowledged_at,
                Some(&ack.reasons.join(",")),
            )?;
        }
        // open_orders is always the OMS-derived count — never a manual
        // delta that can drift from order-state truth.
        self.sync_runtime_state()?;
        self.idempotency
            .mark_complete(correlation_id, ack.clone(), now)?;
        Ok(ack)
    }

    fn finalize_cancel(
        &mut self,
        correlation_id: &str,
        result: Result<GatewayAck, GatewayError>,
        now: &str,
    ) -> Result<GatewayAck, GatewayError> {
        let ack = result?;
        // A cancel ACK only means the venue accepted the cancel request.
        // Open risk is released only after an own-order update confirms a
        // terminal state.
        self.idempotency
            .mark_complete(correlation_id, ack.clone(), now)?;
        Ok(ack)
    }
}

/// Async dispatch path. Available only when the venue client implements
/// [`AsyncVenueClient`]. The shape mirrors `process_batch` / `process_one`
/// but awaits the venue call so the runtime can drive other tasks (such
/// as a spawned WS reader) during the in-flight REST request.
impl<C: VenueClient + AsyncVenueClient> DryRunGateway<C> {
    pub async fn process_batch_async(
        &mut self,
        now: &str,
        limit: usize,
    ) -> Vec<(String, Result<GatewayAck, GatewayError>)> {
        let mut out = Vec::with_capacity(limit);
        for _ in 0..limit {
            let Some(envelope) = self.scheduler.pop_next() else {
                break;
            };
            let cid = envelope.correlation_id.clone();
            let result = self.process_one_async(envelope, now).await;
            out.push((cid, result));
        }
        out
    }

    async fn process_one_async(
        &mut self,
        envelope: IntentEnvelopeRecord,
        now: &str,
    ) -> Result<GatewayAck, GatewayError> {
        match self.prepare_one(envelope, now)? {
            PreparedExecution::Place {
                envelope,
                payload_for_venue,
                client_order_id,
            } => {
                let result = self.venue.submit_async(&envelope, &payload_for_venue).await;
                self.finalize_place(
                    &client_order_id,
                    &envelope.correlation_id,
                    &envelope.emitted_at,
                    result,
                    now,
                )
            }
            PreparedExecution::Cancel {
                envelope,
                client_order_id,
            } => {
                let result = self.venue.cancel_async(&envelope, &client_order_id).await;
                self.finalize_cancel(&envelope.correlation_id, result, now)
            }
        }
    }
}

impl<C: VenueClient> DryRunGateway<C> {
    /// Mark a fill received from the venue. Idempotent via the OMS dedupe.
    ///
    /// On apply:
    /// - OMS state advances per the fill (Partial/Filled, etc.).
    /// - `sleeve_state.positions[instrument]` is updated **incrementally**:
    ///   - same-side fills update VWAP using **fill price** (not the order's
    ///     limit price — fix for N3).
    ///   - opposite-side fills crystallize realized P&L on the closed portion
    ///     at `(fill_price - entry_price) * close_qty * sign(position)`.
    /// - `sleeve_state.daily_realized_loss` folds in realized loss + fees
    ///   (fix for N2: previously fees-only).
    /// - `open_orders` count is recomputed in O(open) using the OMS iterator.
    ///
    /// O(n_orders_ever) recompute is gone (fix for F4).
    pub fn apply_fill(&mut self, fill: Fill) -> Result<bool, GatewayError> {
        let client_order_id = fill.client_order_id.clone();
        let fill_price_ticks = parse_price_ticks(&fill.price)?;
        let fill_qty = parse_whole_qty(&fill.quantity)?;
        let fee_ticks = parse_price_ticks(&fill.fee)?;
        let trade_ts = fill.trade_ts.clone();
        let trade_epoch = epoch_seconds(&trade_ts).unwrap_or(0);
        let trade_epoch_millis = epoch_millis(&trade_ts).unwrap_or(0);
        let applied = self.oms.apply_fill(fill)?;
        if !applied {
            return Ok(false);
        }
        let Some(order) = self.oms.get(&client_order_id) else {
            // OMS accepted the fill but lost the order — shouldn't happen.
            return Ok(true);
        };
        let outcome_side = order.outcome_side;
        let signed_qty = match order.side {
            Side::Buy => fill_qty,
            Side::Sell => -fill_qty,
        };
        let cash_delta_ticks = -signed_qty.saturating_mul(fill_price_ticks);
        let instrument = order.instrument_id.clone();
        let position_key = outcome_position_key(&instrument, outcome_side);

        // Roll today's daily-loss bucket on UTC-day rollover BEFORE adding
        // this fill's contribution.
        let today = utc_day_from_epoch_secs(trade_epoch);
        if today != self.sleeve_state.daily_loss_day_utc {
            self.sleeve_state.daily_loss_day_utc = today;
            self.sleeve_state.daily_realized_loss = 0;
        }

        // Incremental position update + realized P&L (N2 + N3).
        let realized_ticks =
            self.update_position_for_fill(&position_key, signed_qty, fill_price_ticks);
        let loss_contribution = realized_ticks.min(0).saturating_neg();
        self.sleeve_state.daily_realized_loss = self
            .sleeve_state
            .daily_realized_loss
            .saturating_add(loss_contribution)
            .saturating_add(fee_ticks.max(0));

        // F6: keep available cash in sync when it is tracked (live, seeded from
        // the venue balance at reconcile). A buy debits cash, a sell credits it
        // (`cash_delta_ticks` already carries the sign), and fees always debit.
        if let Some(cash) = self.sleeve_state.available_cash_ticks.as_mut() {
            *cash = cash
                .saturating_add(cash_delta_ticks)
                .saturating_sub(fee_ticks.max(0));
        }

        self.ledger.push(LedgerEntry {
            ts: trade_ts,
            client_order_id,
            instrument_id: instrument,
            outcome_side,
            position_delta: signed_qty,
            cash_delta_ticks,
            fee_ticks,
        });
        if self.toxicity.record_fill(trade_epoch_millis).is_some() {
            self.sleeve_state.kill_switch_engaged = true;
        }
        // Open-order count tracks live (non-terminal) orders. Recompute in
        // O(open_orders), not O(all_orders_ever).
        self.sync_runtime_state()?;
        Ok(true)
    }

    pub fn apply_order_update(
        &mut self,
        client_order_id: &str,
        state: &str,
        updated_at: &str,
        reason: Option<&str>,
    ) -> Result<(), GatewayError> {
        let Some(to) = map_venue_state(state) else {
            return Ok(());
        };
        self.oms
            .transition(client_order_id, to, updated_at, reason)?;
        // Only the open-order count changes on a state transition; positions
        // only change on fills and are handled in apply_fill.
        self.sync_runtime_state()?;
        Ok(())
    }

    pub fn adopt_resting_order(
        &mut self,
        snapshot: RestingOrderSnapshot,
    ) -> Result<String, GatewayError> {
        let instrument_id = snapshot.instrument_id.clone();
        let outcome_side = snapshot.outcome_side;
        let observed_epoch = epoch_seconds(&snapshot.observed_at)
            .or_else(|| epoch_seconds(&snapshot.updated_at))
            .unwrap_or(0);
        let position_key = outcome_position_key(&snapshot.instrument_id, snapshot.outcome_side);
        let filled_qty = parse_whole_qty(&snapshot.filled_quantity)?;
        let fill_price_ticks = parse_price_ticks(&snapshot.price)?;
        let signed_filled_qty = match snapshot.side {
            Side::Buy => filled_qty,
            Side::Sell => -filled_qty,
        };
        let mut order = Order::new_with_outcome(
            &snapshot.client_order_id,
            &snapshot.instrument_id,
            snapshot.outcome_side,
            snapshot.side,
            &snapshot.price,
            &snapshot.quantity,
            snapshot.time_in_force,
            &snapshot.updated_at,
        )?;
        order.venue_order_id = snapshot.venue_order_id;
        order.filled_quantity = snapshot.filled_quantity;
        order.state = snapshot.state;
        order.last_update_at = snapshot.updated_at;
        order.last_reject_reason = snapshot.reject_reason;
        self.oms.adopt_order(order)?;
        if signed_filled_qty != 0 {
            self.update_position_for_fill(&position_key, signed_filled_qty, fill_price_ticks);
        }
        self.seed_adopted_market_data_epoch(&instrument_id, outcome_side, observed_epoch);
        self.sync_runtime_state()?;
        self.validate_adopted_state()?;
        Ok(instrument_id)
    }

    fn seed_adopted_market_data_epoch(
        &mut self,
        instrument_id: &str,
        outcome_side: OutcomeSide,
        observed_epoch: i64,
    ) {
        self.sleeve_state
            .last_quote_epoch_secs
            .insert(instrument_id.to_string(), observed_epoch);
        self.sleeve_state.last_quote_epoch_secs.insert(
            outcome_position_key(instrument_id, outcome_side),
            observed_epoch,
        );
    }

    fn sync_runtime_state(&mut self) -> Result<(), GatewayError> {
        self.sleeve_state.open_orders = self.oms.open_count();
        self.portfolio.sync_open_orders(self.oms.open_orders())?;
        Ok(())
    }

    fn validate_adopted_state(&mut self) -> Result<(), GatewayError> {
        let risk_projected_state = self.state_projected_with_open_orders()?;
        let risk_rejections = self.risk.evaluate_state_only(&risk_projected_state);
        let portfolio_rejection = self
            .portfolio
            .evaluate_current_state(&self.sleeve_state)
            .err();
        if !risk_rejections.is_empty() || portfolio_rejection.is_some() {
            self.sleeve_state.kill_switch_engaged = true;
        }
        Ok(())
    }

    fn state_projected_with_open_orders(&self) -> Result<SleeveState, GatewayError> {
        let mut state = self.sleeve_state.clone();
        for order in self.oms.open_orders() {
            let quantity = parse_decimal_ticks_i128(&order.quantity).map_err(|_| {
                GatewayError::DecisionDecode(format!("invalid order quantity `{}`", order.quantity))
            })?;
            let filled = parse_decimal_ticks_i128(&order.filled_quantity).map_err(|_| {
                GatewayError::DecisionDecode(format!(
                    "invalid filled quantity `{}`",
                    order.filled_quantity
                ))
            })?;
            let remaining = quantity.saturating_sub(filled).max(0);
            if remaining == 0 {
                continue;
            }
            if remaining % 10_000 != 0 {
                return Err(GatewayError::DecisionDecode(format!(
                    "invalid non-whole remaining quantity `{}`",
                    order.quantity
                )));
            }
            let qty_units = remaining / 10_000;
            if qty_units > i128::from(i64::MAX) {
                return Err(GatewayError::DecisionDecode(format!(
                    "order quantity too large `{}`",
                    order.quantity
                )));
            }
            let signed_qty = match order.side {
                Side::Buy => qty_units as i64,
                Side::Sell => -(qty_units as i64),
            };
            let key = outcome_position_key(&order.instrument_id, order.outcome_side);
            let price_ticks = parse_price_ticks(&order.price)?;
            project_position_for_state(&mut state, &key, signed_qty, price_ticks);
        }
        Ok(state)
    }

    fn self_cross_check(
        &self,
        instrument_id: &str,
        outcome_side: OutcomeSide,
        side: Side,
        price: &str,
    ) -> Result<(), GatewayError> {
        let incoming_ticks = parse_price_ticks(price)?;
        for order in self.oms.open_orders() {
            if order.instrument_id != instrument_id
                || order.outcome_side != outcome_side
                || order.side == side
            {
                continue;
            }
            let resting_ticks = parse_price_ticks(&order.price)?;
            let crosses = match side {
                Side::Buy => incoming_ticks >= resting_ticks,
                Side::Sell => incoming_ticks <= resting_ticks,
            };
            if crosses {
                return Err(GatewayError::SelfCross {
                    instrument: instrument_id.to_string(),
                    resting_client_order_id: order.client_order_id.clone(),
                    incoming_side: side,
                    incoming_ticks,
                    resting_ticks,
                });
            }
        }
        Ok(())
    }

    /// Send-time last-look: verify the latest mark + freshness against the
    /// intent right before transmit. Returns a `LastLook` error to roll the
    /// intent back without contacting the venue.
    fn last_look_check(
        &self,
        instrument_id: &str,
        outcome_side: OutcomeSide,
        side: Side,
        intent_price: &str,
        intent_quantity: &str,
        now: &str,
    ) -> Result<(), GatewayError> {
        // Parse the intent price into 4-decimal ticks. The strategy may have
        // written it as runner-scale dollars or as Kalshi cents — the helper
        // is permissive.
        let intent_ticks = parse_price_ticks(intent_price)?;
        let key = outcome_position_key(instrument_id, outcome_side);
        let mark = self
            .sleeve_state
            .mark_price_ticks
            .get(&key)
            .or_else(|| self.sleeve_state.mark_price_ticks.get(instrument_id))
            .copied();
        if self.last_look.require_mark_price && mark.is_none() {
            return Err(GatewayError::LastLook {
                instrument: instrument_id.to_string(),
                reason: "no_mark_price",
                intent_ticks,
                mark_ticks: None,
                quote_age_secs: None,
            });
        }
        let now_epoch = epoch_seconds(now).unwrap_or(0);
        let last_seen = self
            .sleeve_state
            .last_quote_epoch_secs
            .get(&key)
            .or_else(|| self.sleeve_state.last_quote_epoch_secs.get(instrument_id))
            .copied();
        let age = last_seen.map(|t| (now_epoch.saturating_sub(t)).max(0) as u32);
        if let Some(a) = age {
            if a > self.last_look.max_quote_age_secs {
                return Err(GatewayError::LastLook {
                    instrument: instrument_id.to_string(),
                    reason: "quote_stale",
                    intent_ticks,
                    mark_ticks: mark,
                    quote_age_secs: Some(a),
                });
            }
        }
        let executable_reference = match side {
            Side::Buy => self
                .sleeve_state
                .best_ask_ticks
                .get(&key)
                .or_else(|| self.sleeve_state.best_ask_ticks.get(instrument_id))
                .copied(),
            Side::Sell => self
                .sleeve_state
                .best_bid_ticks
                .get(&key)
                .or_else(|| self.sleeve_state.best_bid_ticks.get(instrument_id))
                .copied(),
        };
        if self.last_look.require_executable_bbo && executable_reference.is_none() {
            return Err(GatewayError::LastLook {
                instrument: instrument_id.to_string(),
                reason: "no_executable_bbo",
                intent_ticks,
                mark_ticks: mark,
                quote_age_secs: age,
            });
        }
        if let Some(reference) = executable_reference {
            let immediately_executable = match side {
                Side::Buy => intent_ticks >= reference,
                Side::Sell => intent_ticks <= reference,
            };
            if self.last_look.require_l1_depth && immediately_executable {
                let available_qty_ticks = match side {
                    Side::Buy => self
                        .sleeve_state
                        .best_ask_qty_ticks
                        .get(&key)
                        .or_else(|| self.sleeve_state.best_ask_qty_ticks.get(instrument_id))
                        .copied(),
                    Side::Sell => self
                        .sleeve_state
                        .best_bid_qty_ticks
                        .get(&key)
                        .or_else(|| self.sleeve_state.best_bid_qty_ticks.get(instrument_id))
                        .copied(),
                };
                let order_qty_ticks = parse_decimal_ticks_i128(intent_quantity).map_err(|_| {
                    GatewayError::DecisionDecode(format!("invalid quantity `{intent_quantity}`"))
                })?;
                let Some(available_qty_ticks) = available_qty_ticks else {
                    return Err(GatewayError::LastLook {
                        instrument: instrument_id.to_string(),
                        reason: "missing_l1_depth",
                        intent_ticks,
                        mark_ticks: Some(reference),
                        quote_age_secs: age,
                    });
                };
                if order_qty_ticks > available_qty_ticks {
                    return Err(GatewayError::LastLook {
                        instrument: instrument_id.to_string(),
                        reason: "l1_depth_insufficient",
                        intent_ticks,
                        mark_ticks: Some(reference),
                        quote_age_secs: age,
                    });
                }
            }
        }
        if let Some(reference) = executable_reference.or(mark) {
            let movement = (intent_ticks - reference).abs();
            if movement > self.last_look.max_mark_movement_ticks {
                return Err(GatewayError::LastLook {
                    instrument: instrument_id.to_string(),
                    reason: if executable_reference.is_some() {
                        "bbo_moved_beyond_collar"
                    } else {
                        "mark_moved_beyond_collar"
                    },
                    intent_ticks,
                    mark_ticks: Some(reference),
                    quote_age_secs: age,
                });
            }
        }
        Ok(())
    }

    /// Apply a signed fill to the position state, returning the realized
    /// P&L in ticks (positive = profit, negative = loss). VWAP for the
    /// remaining position uses the fill price.
    ///
    /// Cases:
    /// - Empty or same-side: add to position, update VWAP at fill price.
    /// - Opposite side, partial close: realize P&L on the closed portion,
    ///   leave the rest of the position untouched at its existing avg.
    /// - Opposite side, full close: position goes flat, realize all.
    /// - Opposite side, flip: realize on the closed portion, the residual
    ///   opens a new position on the opposite side at the fill price.
    fn update_position_for_fill(
        &mut self,
        instrument: &str,
        signed_qty: i64,
        fill_price_ticks: i64,
    ) -> i64 {
        let prev = self
            .sleeve_state
            .positions
            .get(instrument)
            .cloned()
            .unwrap_or_default();
        let prev_qty = prev.quantity;
        let prev_avg = prev.avg_price_ticks;
        let new_qty = prev_qty.saturating_add(signed_qty);

        // No prior position: open at fill price; nothing realized.
        if prev_qty == 0 {
            self.sleeve_state.positions.insert(
                instrument.to_string(),
                Position {
                    quantity: new_qty,
                    avg_price_ticks: fill_price_ticks,
                },
            );
            return 0;
        }

        let same_side = (prev_qty.signum() == signed_qty.signum()) || signed_qty == 0;
        if same_side {
            // Pure accumulation. VWAP at fill price.
            let prev_notional = i128::from(prev_qty).saturating_mul(i128::from(prev_avg));
            let add_notional = i128::from(signed_qty).saturating_mul(i128::from(fill_price_ticks));
            let total = prev_notional.saturating_add(add_notional);
            let avg = if new_qty == 0 {
                0_i64
            } else {
                (total / i128::from(new_qty)).clamp(i128::from(i64::MIN), i128::from(i64::MAX))
                    as i64
            };
            if new_qty == 0 {
                self.sleeve_state.positions.remove(instrument);
            } else {
                self.sleeve_state.positions.insert(
                    instrument.to_string(),
                    Position {
                        quantity: new_qty,
                        avg_price_ticks: avg,
                    },
                );
            }
            return 0;
        }

        // Opposite-side fill: close some quantity, possibly flip.
        let close_qty_abs = signed_qty.abs().min(prev_qty.abs());
        let position_sign: i64 = prev_qty.signum();
        // Realized = sign(position) * close_qty * (exit - entry).
        // For a long (prev_qty > 0), closing means a sell, exit = fill_price.
        // For a short (prev_qty < 0), closing means a buy, exit = fill_price.
        let realized_per_unit = fill_price_ticks.saturating_sub(prev_avg);
        let realized = position_sign
            .saturating_mul(close_qty_abs)
            .saturating_mul(realized_per_unit);

        if new_qty == 0 {
            self.sleeve_state.positions.remove(instrument);
        } else if new_qty.signum() == prev_qty.signum() {
            // Partial close — same side, smaller. Avg unchanged.
            self.sleeve_state.positions.insert(
                instrument.to_string(),
                Position {
                    quantity: new_qty,
                    avg_price_ticks: prev_avg,
                },
            );
        } else {
            // Flip — new position opposite side, at fill price.
            self.sleeve_state.positions.insert(
                instrument.to_string(),
                Position {
                    quantity: new_qty,
                    avg_price_ticks: fill_price_ticks,
                },
            );
        }
        realized
    }
}

// ---------- helpers ----------

fn projected_portfolio_gross(
    policy: &PortfolioPolicy,
    state: &SleeveState,
    reservations: &HashMap<String, PortfolioReservation>,
    extra: Option<PortfolioOrderProjection<'_>>,
) -> i128 {
    projected_positions(policy, state, reservations, extra)
        .values()
        .map(|pos| mul_ticks(pos.qty_ticks.abs(), pos.price_ticks.abs()))
        .sum()
}

fn projected_group_gross(
    policy: &PortfolioPolicy,
    state: &SleeveState,
    reservations: &HashMap<String, PortfolioReservation>,
    group: &str,
    extra: Option<PortfolioOrderProjection<'_>>,
) -> i128 {
    projected_positions(policy, state, reservations, extra)
        .values()
        .filter(|pos| policy.group_for(&pos.instrument_id) == group)
        .map(|pos| mul_ticks(pos.qty_ticks.abs(), pos.price_ticks.abs()))
        .sum()
}

#[derive(Clone, Debug)]
struct ProjectedPortfolioPosition {
    instrument_id: String,
    qty_ticks: i128,
    price_ticks: i128,
}

fn projected_positions(
    _policy: &PortfolioPolicy,
    state: &SleeveState,
    reservations: &HashMap<String, PortfolioReservation>,
    extra: Option<PortfolioOrderProjection<'_>>,
) -> HashMap<String, ProjectedPortfolioPosition> {
    let mut projected: HashMap<String, ProjectedPortfolioPosition> = state
        .positions
        .iter()
        .map(|(key, pos)| {
            let price = state
                .mark_price_ticks
                .get(key)
                .copied()
                .unwrap_or(pos.avg_price_ticks);
            (
                key.clone(),
                ProjectedPortfolioPosition {
                    instrument_id: instrument_from_position_key(key).to_string(),
                    qty_ticks: i128::from(pos.quantity).saturating_mul(10_000),
                    price_ticks: i128::from(price),
                },
            )
        })
        .collect();

    for reservation in reservations.values() {
        let order = PortfolioOrderProjection {
            instrument_id: &reservation.instrument_id,
            outcome_side: reservation.outcome_side,
            side: reservation.side,
            price_ticks: reservation.price_ticks,
            quantity_ticks: reservation.remaining_qty_ticks,
        };
        apply_order_projection(&mut projected, order);
    }
    if let Some(order) = extra {
        apply_order_projection(&mut projected, order);
    }
    projected
}

fn apply_order_projection(
    projected: &mut HashMap<String, ProjectedPortfolioPosition>,
    order: PortfolioOrderProjection<'_>,
) {
    let key = outcome_position_key(order.instrument_id, order.outcome_side);
    let entry = projected
        .entry(key)
        .or_insert_with(|| ProjectedPortfolioPosition {
            instrument_id: order.instrument_id.to_string(),
            qty_ticks: 0,
            price_ticks: order.price_ticks,
        });
    let signed_qty = match order.side {
        Side::Buy => order.quantity_ticks,
        Side::Sell => -order.quantity_ticks,
    };
    let before_abs = entry.qty_ticks.abs();
    entry.qty_ticks = entry.qty_ticks.saturating_add(signed_qty);
    let after_abs = entry.qty_ticks.abs();
    if after_abs > before_abs {
        entry.price_ticks = entry.price_ticks.abs().max(order.price_ticks.abs());
    }
}

fn project_position_for_state(
    state: &mut SleeveState,
    position_key: &str,
    signed_qty: i64,
    price_ticks: i64,
) {
    let prev = state
        .positions
        .get(position_key)
        .cloned()
        .unwrap_or_default();
    let new_qty = prev.quantity.saturating_add(signed_qty);
    if new_qty == 0 {
        state.positions.remove(position_key);
        return;
    }
    let before_abs = prev.quantity.abs();
    let after_abs = new_qty.abs();
    let avg_price_ticks = if before_abs == 0 || after_abs > before_abs {
        prev.avg_price_ticks.abs().max(price_ticks.abs())
    } else {
        prev.avg_price_ticks
    };
    state.positions.insert(
        position_key.to_string(),
        Position {
            quantity: new_qty,
            avg_price_ticks,
        },
    );
}

fn instrument_from_position_key(key: &str) -> &str {
    key.rsplit_once('|')
        .map_or(key, |(instrument, _outcome)| instrument)
}

/// Build the synthetic cancel envelope the gateway uses when a venue
/// lifecycle transition forces resting orders off the book. The strategy
/// owner of the order is preserved indirectly via `client_order_id`
/// (the OMS still tracks it); the producer here is the gateway itself.
fn synthesize_market_state_cancel(
    client_order_id: &str,
    reason: Option<&str>,
    now: &str,
) -> Result<IntentEnvelopeRecord, GatewayError> {
    let payload = DecisionPayload::CancelOrder {
        client_order_id: client_order_id.to_string(),
    };
    let decision_json =
        serde_json::to_string(&payload).map_err(|e| GatewayError::DecisionDecode(e.to_string()))?;
    // Unique-per-call correlation id — the idempotency store rejects
    // duplicates. `now` carries to-the-second resolution; mixing in the
    // client_order_id makes repeat invocations in the same second safe.
    let correlation_id = format!("gateway-market-state-cancel:{client_order_id}:{now}");
    let mut metadata = Metadata::new();
    if let Some(reason) = reason {
        metadata.insert("market_state_reason".into(), reason.to_string());
    }
    Ok(IntentEnvelopeRecord {
        strategy_id: "gateway".into(),
        sleeve_id: String::new(),
        correlation_id: correlation_id.clone(),
        emitted_at: now.to_string(),
        decision_kind: "cancel_order".into(),
        decision_json,
        priority_tier: "fast".into(),
        audit: AuditStamp {
            object_id: correlation_id,
            object_kind: "intent".into(),
            schema_version: "intent-envelope-v1".into(),
            produced_at: now.to_string(),
            producer: "gateway-market-state".into(),
            // Synthetic envelopes do not flow through the canonical-JSON
            // chain; gateway internal validation does not require it.
            canonical_sha256: "0".repeat(64),
            parent_ids: vec![],
            trace_id: None,
            metadata,
        },
    })
}

fn parse_decimal_ticks_i128(value: &str) -> Result<i128, ()> {
    parse_fixed_4(value).map(i128::from)
}

fn mul_ticks(left: i128, right: i128) -> i128 {
    left.saturating_mul(right) / 10_000
}

fn format_decimal_ticks_i128(value: i128) -> String {
    let sign = if value < 0 { "-" } else { "" };
    let abs = value.abs();
    let whole = abs / 10_000;
    let cents = ((abs % 10_000) * 100) / 10_000;
    format!("{sign}{whole}.{cents:02}")
}

/// RFC3339 difference in seconds; positive when `now > emitted_at`. Both inputs
/// must start with `YYYY-MM-DDTHH:MM:SS`; sub-second precision and offset are
/// ignored. Sufficient for the gateway's age check — exact timestamp math
/// lives in the runner.
fn age_secs(emitted: &str, now: &str) -> i64 {
    let e = epoch_seconds(emitted).unwrap_or(0);
    let n = epoch_seconds(now).unwrap_or(0);
    n - e
}

fn epoch_seconds(ts: &str) -> Option<i64> {
    if ts.len() < 19 {
        return None;
    }
    let year: i64 = ts[0..4].parse().ok()?;
    let month: i64 = ts[5..7].parse().ok()?;
    let day: i64 = ts[8..10].parse().ok()?;
    let hour: i64 = ts[11..13].parse().ok()?;
    let minute: i64 = ts[14..16].parse().ok()?;
    let second: i64 = ts[17..19].parse().ok()?;
    // civil-from-days, after Howard Hinnant.
    let y = if month <= 2 { year - 1 } else { year };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let m = month;
    let doy = (153 * (if m > 2 { m - 3 } else { m + 9 }) + 2) / 5 + day - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    let days = era * 146097 + doe - 719468;
    Some(days * 86_400 + hour * 3600 + minute * 60 + second)
}

fn epoch_millis(ts: &str) -> Option<i64> {
    let seconds = epoch_seconds(ts)?;
    let mut millis = 0_i64;
    if ts.as_bytes().get(19).copied() == Some(b'.') {
        let mut digits = 0_u8;
        for byte in ts.as_bytes().iter().skip(20).copied() {
            if !byte.is_ascii_digit() || digits >= 3 {
                break;
            }
            millis = millis
                .saturating_mul(10)
                .saturating_add(i64::from(byte - b'0'));
            digits += 1;
        }
        while digits < 3 {
            millis = millis.saturating_mul(10);
            digits += 1;
        }
    }
    Some(seconds.saturating_mul(1_000).saturating_add(millis))
}

fn map_venue_state(state: &str) -> Option<OrderState> {
    match state.trim().to_ascii_lowercase().as_str() {
        "acked" | "accepted" | "open" | "resting" | "live" => Some(OrderState::Acked),
        "partially_filled" | "partially-filled" | "partial" => Some(OrderState::PartiallyFilled),
        "filled" => Some(OrderState::Filled),
        "canceled" | "cancelled" => Some(OrderState::Canceled),
        "rejected" => Some(OrderState::Rejected),
        "expired" => Some(OrderState::Expired),
        _ => None,
    }
}

fn parse_price_ticks(value: &str) -> Result<i64, GatewayError> {
    parse_fixed_4(value)
        .map_err(|_| GatewayError::DecisionDecode(format!("invalid price `{value}`")))
}

fn parse_whole_qty(value: &str) -> Result<i64, GatewayError> {
    let (whole, frac) = value
        .split_once('.')
        .map_or((value, ""), |(whole, frac)| (whole, frac));
    if whole.is_empty()
        || !whole.bytes().all(|b| b.is_ascii_digit())
        || !frac.bytes().all(|b| b == b'0')
    {
        return Err(GatewayError::DecisionDecode(format!(
            "invalid whole quantity `{value}`"
        )));
    }
    whole
        .parse::<i64>()
        .map_err(|_| GatewayError::DecisionDecode(format!("invalid whole quantity `{value}`")))
}

fn parse_fixed_4(value: &str) -> Result<i64, ()> {
    const SCALE: i64 = 10_000;
    if value.is_empty() {
        return Err(());
    }
    let negative = value.starts_with('-');
    let body = if negative { &value[1..] } else { value };
    if body.is_empty() {
        return Err(());
    }
    let (whole_raw, frac_raw) = body.split_once('.').unwrap_or((body, ""));
    if whole_raw.is_empty()
        || !whole_raw.bytes().all(|b| b.is_ascii_digit())
        || frac_raw.len() > 4
        || !frac_raw.bytes().all(|b| b.is_ascii_digit())
    {
        return Err(());
    }
    let whole = whole_raw.parse::<i64>().map_err(|_| ())?;
    let mut frac = frac_raw.to_string();
    while frac.len() < 4 {
        frac.push('0');
    }
    let frac = if frac.is_empty() {
        0
    } else {
        frac.parse::<i64>().map_err(|_| ())?
    };
    let ticks = whole
        .checked_mul(SCALE)
        .and_then(|v| v.checked_add(frac))
        .ok_or(())?;
    Ok(if negative { -ticks } else { ticks })
}

#[cfg(test)]
mod tests {
    use super::*;
    use eventcontracts_contracts::{AuditStamp, Metadata};
    use eventcontracts_risk::RiskLimits;

    fn audit() -> AuditStamp {
        AuditStamp {
            object_id: "intent-1".into(),
            object_kind: "intent".into(),
            schema_version: "intent-envelope-v1".into(),
            produced_at: "2026-05-26T12:00:00Z".into(),
            producer: "runner".into(),
            canonical_sha256: "a".repeat(64),
            parent_ids: vec![],
            trace_id: None,
            metadata: Metadata::new(),
        }
    }

    fn place_intent(
        corr: &str,
        cid: &str,
        tier: &str,
        price: &str,
        qty: &str,
        emitted_at: &str,
    ) -> IntentEnvelopeRecord {
        place_intent_with_side(
            corr,
            cid,
            tier,
            OutcomeSide::Yes,
            Side::Buy,
            price,
            qty,
            emitted_at,
        )
    }

    fn place_intent_with_outcome(
        corr: &str,
        cid: &str,
        tier: &str,
        outcome_side: OutcomeSide,
        price: &str,
        qty: &str,
        emitted_at: &str,
    ) -> IntentEnvelopeRecord {
        place_intent_with_side(
            corr,
            cid,
            tier,
            outcome_side,
            Side::Buy,
            price,
            qty,
            emitted_at,
        )
    }

    #[allow(clippy::too_many_arguments)]
    fn place_intent_with_side(
        corr: &str,
        cid: &str,
        tier: &str,
        outcome_side: OutcomeSide,
        side: Side,
        price: &str,
        qty: &str,
        emitted_at: &str,
    ) -> IntentEnvelopeRecord {
        let payload = DecisionPayload::PlaceOrder {
            client_order_id: cid.into(),
            instrument_id: "kalshi:M-1".into(),
            outcome_side,
            side,
            price: price.into(),
            quantity: qty.into(),
            fair_price: None,
            min_executable_edge_ticks: None,
            fee_rate_bps: None,
            time_in_force: TimeInForce::Gtc,
        };
        IntentEnvelopeRecord {
            strategy_id: "weather-v1".into(),
            sleeve_id: "weather-kalshi-paper-a".into(),
            correlation_id: corr.into(),
            emitted_at: emitted_at.into(),
            decision_kind: "place_order".into(),
            decision_json: serde_json::to_string(&payload).unwrap(),
            priority_tier: tier.into(),
            audit: audit(),
        }
    }

    fn cancel_intent(corr: &str, cid: &str, emitted_at: &str) -> IntentEnvelopeRecord {
        let payload = DecisionPayload::CancelOrder {
            client_order_id: cid.into(),
        };
        IntentEnvelopeRecord {
            strategy_id: "weather-v1".into(),
            sleeve_id: "weather-kalshi-paper-a".into(),
            correlation_id: corr.into(),
            emitted_at: emitted_at.into(),
            decision_kind: "cancel_order".into(),
            decision_json: serde_json::to_string(&payload).unwrap(),
            priority_tier: "standard".into(),
            audit: audit(),
        }
    }

    fn limits() -> RiskLimits {
        RiskLimits {
            max_order_notional: "500".into(),
            max_position_notional: "2500".into(),
            max_daily_loss: "250".into(),
            max_open_orders: 10,
            max_gross_exposure: "5000".into(),
            currency: "USD".into(),
            max_market_data_age_secs: 30,
        }
    }

    fn fresh_gateway() -> DryRunGateway<RecordingVenueClient> {
        let venue = RecordingVenueClient::new();
        let mut gw = DryRunGateway::new(RiskGate::new(limits()), venue);
        // Same epoch as the "now" the tests below pass to process_batch.
        let now_epoch = epoch_seconds("2026-05-26T12:00:00Z").unwrap_or(0);
        gw.sleeve_state
            .last_quote_epoch_secs
            .insert("kalshi:M-1".into(), now_epoch);
        // Default fixture sees mid=$0.50 (5000 ticks) so last-look passes
        // with the default 200-tick collar (intent prices in tests are
        // typically $0.5). Last-look-specific tests override this.
        gw.sleeve_state
            .mark_price_ticks
            .insert("kalshi:M-1".into(), 5000);
        gw
    }

    #[derive(Debug, Default)]
    struct FailingVenueClient;

    impl VenueClient for FailingVenueClient {
        fn submit(
            &mut self,
            _envelope: &IntentEnvelopeRecord,
            _payload: &DecisionPayload,
        ) -> Result<GatewayAck, GatewayError> {
            Err(GatewayError::Transport("fixture_submit_timeout".into()))
        }

        fn cancel(
            &mut self,
            _envelope: &IntentEnvelopeRecord,
            _client_order_id: &str,
        ) -> Result<GatewayAck, GatewayError> {
            Err(GatewayError::Transport("fixture_cancel_timeout".into()))
        }
    }

    #[test]
    fn scheduler_drains_cancels_before_alpha() {
        let mut s = PriorityScheduler::new();
        let p = place_intent("p1", "c-1", "standard", "0.5", "10", "2026-05-26T12:00:00Z");
        let c = cancel_intent("c1", "c-1", "2026-05-26T12:00:00Z");
        let p_payload: DecisionPayload = serde_json::from_str(&p.decision_json).unwrap();
        let c_payload: DecisionPayload = serde_json::from_str(&c.decision_json).unwrap();
        s.enqueue(p, &p_payload).unwrap();
        s.enqueue(c, &c_payload).unwrap();
        let first = s.pop_next().unwrap();
        assert_eq!(first.correlation_id, "c1");
    }

    #[test]
    fn scheduler_drains_fast_before_standard_before_relaxed() {
        let mut s = PriorityScheduler::new();
        let r = place_intent("r", "c-r", "relaxed", "0.5", "1", "2026-05-26T12:00:00Z");
        let st = place_intent("s", "c-s", "standard", "0.5", "1", "2026-05-26T12:00:00Z");
        let f = place_intent("f", "c-f", "fast", "0.5", "1", "2026-05-26T12:00:00Z");
        for env in [r, st, f] {
            let payload: DecisionPayload = serde_json::from_str(&env.decision_json).unwrap();
            s.enqueue(env, &payload).unwrap();
        }
        assert_eq!(s.pop_next().unwrap().correlation_id, "f");
        assert_eq!(s.pop_next().unwrap().correlation_id, "s");
        assert_eq!(s.pop_next().unwrap().correlation_id, "r");
    }

    #[test]
    fn scheduler_sheds_oldest_non_cancel_when_full_but_keeps_cancels() {
        let mut s = PriorityScheduler::with_non_cancel_capacity(2);
        let old = place_intent("old", "c-old", "fast", "0.5", "1", "2026-05-26T12:00:00Z");
        let newer = place_intent(
            "newer",
            "c-newer",
            "relaxed",
            "0.5",
            "1",
            "2026-05-26T12:00:01Z",
        );
        let newest = place_intent(
            "newest",
            "c-newest",
            "standard",
            "0.5",
            "1",
            "2026-05-26T12:00:02Z",
        );
        for env in [old, newer] {
            let payload: DecisionPayload = serde_json::from_str(&env.decision_json).unwrap();
            assert_eq!(s.enqueue(env, &payload).unwrap(), EnqueueOutcome::Enqueued);
        }

        let payload: DecisionPayload = serde_json::from_str(&newest.decision_json).unwrap();
        assert_eq!(
            s.enqueue(newest, &payload).unwrap(),
            EnqueueOutcome::DroppedOldestNonCancel {
                correlation_id: "old".into(),
                client_order_id: Some("c-old".into()),
            }
        );
        assert_eq!(s.shed_non_cancel_intents(), 1);

        let cancel = cancel_intent("cancel", "c-old", "2026-05-26T12:00:03Z");
        let payload: DecisionPayload = serde_json::from_str(&cancel.decision_json).unwrap();
        assert_eq!(
            s.enqueue(cancel, &payload).unwrap(),
            EnqueueOutcome::Enqueued
        );
        assert_eq!(s.pop_next().unwrap().correlation_id, "cancel");
        assert_eq!(s.pop_next().unwrap().correlation_id, "newest");
        assert_eq!(s.pop_next().unwrap().correlation_id, "newer");
    }

    #[test]
    fn idempotency_blocks_duplicate_correlation_id() {
        let mut store = IdempotencyStore::new();
        store.reserve("corr-1", "2026-05-26T12:00:00Z").unwrap();
        let err = store.reserve("corr-1", "2026-05-26T12:00:01Z").unwrap_err();
        assert!(matches!(err, GatewayError::Idempotency(_)));
    }

    #[test]
    fn end_to_end_place_routes_through_risk_oms_and_records_in_venue() {
        let mut gw = fresh_gateway();
        let intent = place_intent("p1", "c-1", "standard", "0.5", "10", "2026-05-26T12:00:00Z");
        gw.enqueue(intent).unwrap();
        let acks = gw.process_batch("2026-05-26T12:00:00Z", 10);
        assert_eq!(acks.len(), 1);
        let (_cid, res) = &acks[0];
        let ack = res.as_ref().unwrap();
        assert!(ack.accepted);
        assert_eq!(gw.venue.submitted.len(), 1);
        let order = gw.oms.get("c-1").unwrap();
        // After HTTP submit ack the order is Submitted, NOT Acked — Acked is
        // reserved for the venue WS confirmation.
        assert_eq!(order.state, OrderState::Submitted);
        assert!(order.venue_order_id.is_some());
        assert_eq!(gw.sleeve_state.open_orders, 1);

        // Simulate the venue's WS confirming the order is resting:
        gw.apply_order_update("c-1", "resting", "2026-05-26T12:00:00Z", None)
            .unwrap();
        assert_eq!(gw.oms.get("c-1").unwrap().state, OrderState::Acked);
    }

    #[test]
    fn risk_rejection_short_circuits_before_venue_submit() {
        let mut gw = fresh_gateway();
        // 10000 * 0.5 = 5000 notional, max_order_notional is 500
        let intent = place_intent(
            "p1",
            "c-1",
            "standard",
            "0.5",
            "10000",
            "2026-05-26T12:00:00Z",
        );
        gw.enqueue(intent).unwrap();
        let acks = gw.process_batch("2026-05-26T12:00:00Z", 10);
        assert_eq!(acks.len(), 1);
        let (_cid, res) = &acks[0];
        assert!(matches!(
            res.as_ref().unwrap_err(),
            GatewayError::RiskRejected(_)
        ));
        assert_eq!(gw.venue.submitted.len(), 0);
        assert!(gw.oms.get("c-1").is_none());
    }

    #[test]
    fn stale_intent_dropped_before_venue() {
        let mut gw = fresh_gateway();
        gw.max_intent_age_secs = 5;
        let intent = place_intent("p1", "c-1", "standard", "0.5", "10", "2026-05-26T12:00:00Z");
        gw.enqueue(intent).unwrap();
        let acks = gw.process_batch("2026-05-26T12:00:30Z", 10);
        assert!(matches!(
            acks[0].1.as_ref().unwrap_err(),
            GatewayError::Stale {
                age_secs: 30,
                limit_secs: 5
            }
        ));
        assert_eq!(gw.venue.submitted.len(), 0);
    }

    #[test]
    fn duplicate_correlation_id_blocked_at_idempotency_layer() {
        let mut gw = fresh_gateway();
        gw.enqueue(place_intent(
            "p1",
            "c-1",
            "standard",
            "0.5",
            "10",
            "2026-05-26T12:00:00Z",
        ))
        .unwrap();
        gw.enqueue(place_intent(
            "p1",
            "c-2",
            "standard",
            "0.5",
            "10",
            "2026-05-26T12:00:00Z",
        ))
        .unwrap();
        let acks = gw.process_batch("2026-05-26T12:00:00Z", 10);
        assert!(acks[0].1.is_ok());
        assert!(matches!(
            acks[1].1.as_ref().unwrap_err(),
            GatewayError::Idempotency(_)
        ));
        assert_eq!(gw.venue.submitted.len(), 1);
    }

    #[test]
    fn cancel_after_ack_terminates_order_and_releases_open_count() {
        let mut gw = fresh_gateway();
        gw.enqueue(place_intent(
            "p1",
            "c-1",
            "standard",
            "0.5",
            "10",
            "2026-05-26T12:00:00Z",
        ))
        .unwrap();
        gw.process_batch("2026-05-26T12:00:00Z", 10);
        assert_eq!(gw.sleeve_state.open_orders, 1);
        gw.enqueue(cancel_intent("c-c1", "c-1", "2026-05-26T12:00:01Z"))
            .unwrap();
        gw.process_batch("2026-05-26T12:00:01Z", 10);
        // Cancel was transmitted but venue hasn't confirmed yet — order is
        // still in Submitted (post-N4: HTTP ack ≠ resting).
        assert_eq!(gw.oms.get("c-1").unwrap().state, OrderState::Submitted);
        assert_eq!(gw.sleeve_state.open_orders, 1);
        gw.apply_order_update("c-1", "canceled", "2026-05-26T12:00:02Z", None)
            .unwrap();
        let order = gw.oms.get("c-1").unwrap();
        assert_eq!(order.state, OrderState::Canceled);
        assert_eq!(gw.sleeve_state.open_orders, 0);
    }

    #[test]
    fn fill_applied_after_ack_promotes_to_filled_and_decrements_open_count() {
        let mut gw = fresh_gateway();
        gw.enqueue(place_intent(
            "p1",
            "c-1",
            "standard",
            "0.5",
            "10",
            "2026-05-26T12:00:00Z",
        ))
        .unwrap();
        gw.process_batch("2026-05-26T12:00:00Z", 10);
        let applied = gw
            .apply_fill(Fill {
                fill_id: "f-1".into(),
                client_order_id: "c-1".into(),
                price: "0.5".into(),
                quantity: "10".into(),
                fee: "0".into(),
                trade_ts: "2026-05-26T12:00:01Z".into(),
            })
            .unwrap();
        assert!(applied);
        assert_eq!(gw.oms.get("c-1").unwrap().state, OrderState::Filled);
        assert_eq!(gw.sleeve_state.open_orders, 0);
        assert_eq!(gw.ledger.len(), 1);
        assert_eq!(
            gw.sleeve_state
                .positions
                .get(&outcome_position_key("kalshi:M-1", OutcomeSide::Yes)),
            Some(&Position {
                quantity: 10,
                avg_price_ticks: 5000
            })
        );
    }

    #[tokio::test]
    async fn live_path_async_submit_then_fills_track_cash_and_daily_loss() {
        // F7 + F6 end-to-end: the async submit path (submit_async → ack → OMS
        // Submitted), then venue fills through apply_fill the way the live runner
        // feeds the WS `fill` channel — asserting the available-cash gauge and
        // daily realized loss both track venue truth.
        let mut gw = fresh_gateway();
        gw.sleeve_state.available_cash_ticks = Some(100 * 10_000); // $100 funded

        // Open: BUY 10 YES @ $0.50 via the async path.
        gw.enqueue(place_intent(
            "p1",
            "c-1",
            "standard",
            "0.50",
            "10",
            "2026-05-26T12:00:00Z",
        ))
        .unwrap();
        let acks = gw.process_batch_async("2026-05-26T12:00:00Z", 4).await;
        assert!(acks[0].1.is_ok());
        assert_eq!(gw.oms.get("c-1").unwrap().state, OrderState::Submitted);
        // A submit is not a spend: cash is unchanged until the fill lands.
        assert_eq!(gw.sleeve_state.available_cash_ticks, Some(100 * 10_000));

        // Fill the buy fully at $0.50 with a $0.01 fee: cash debits $5.00 + fee.
        gw.apply_fill(Fill {
            fill_id: "f-1".into(),
            client_order_id: "c-1".into(),
            price: "0.50".into(),
            quantity: "10".into(),
            fee: "0.01".into(),
            trade_ts: "2026-05-26T12:00:00Z".into(),
        })
        .unwrap();
        assert_eq!(gw.sleeve_state.available_cash_ticks, Some(949_900));
        assert_eq!(gw.sleeve_state.daily_realized_loss, 100); // opening fill: fee only
        let yes_key = outcome_position_key("kalshi:M-1", OutcomeSide::Yes);
        assert_eq!(gw.sleeve_state.positions[&yes_key].quantity, 10);

        // Close at a loss: SELL 10 YES @ $0.49 (inside the last-look collar).
        gw.enqueue(place_intent_with_side(
            "p2",
            "c-2",
            "standard",
            OutcomeSide::Yes,
            Side::Sell,
            "0.49",
            "10",
            "2026-05-26T12:00:00Z",
        ))
        .unwrap();
        assert!(gw.process_batch_async("2026-05-26T12:00:00Z", 4).await[0]
            .1
            .is_ok());
        gw.apply_fill(Fill {
            fill_id: "f-2".into(),
            client_order_id: "c-2".into(),
            price: "0.49".into(),
            quantity: "10".into(),
            fee: "0.01".into(),
            trade_ts: "2026-05-26T12:00:00Z".into(),
        })
        .unwrap();
        // Sell credits $4.90, debits the $0.01 fee: 949_900 + 49_000 − 100.
        assert_eq!(gw.sleeve_state.available_cash_ticks, Some(998_800));
        // Realized loss = $0.10 on the close + $0.02 total fees = 0.12 → 1200.
        assert_eq!(gw.sleeve_state.daily_realized_loss, 1200);
        assert_eq!(
            gw.sleeve_state
                .positions
                .get(&yes_key)
                .map_or(0, |p| p.quantity),
            0
        );
    }

    #[test]
    fn toxicity_circuit_trips_on_fill_velocity_and_blocks_new_orders() {
        let mut gw = fresh_gateway();
        gw.toxicity = ToxicityCircuitBreaker::enabled(2, 100, 30_000);
        gw.enqueue(place_intent(
            "p1",
            "c-1",
            "standard",
            "0.5",
            "2",
            "2026-05-26T12:00:00Z",
        ))
        .unwrap();
        assert!(gw.process_batch("2026-05-26T12:00:00Z", 10)[0].1.is_ok());
        for (fill_id, trade_ts) in [
            ("f-1", "2026-05-26T12:00:01.000Z"),
            ("f-2", "2026-05-26T12:00:01.050Z"),
        ] {
            gw.apply_fill(Fill {
                fill_id: fill_id.into(),
                client_order_id: "c-1".into(),
                price: "0.5".into(),
                quantity: "1".into(),
                fee: "0".into(),
                trade_ts: trade_ts.into(),
            })
            .unwrap();
        }
        assert!(gw.sleeve_state.kill_switch_engaged);

        gw.enqueue(place_intent(
            "p2",
            "c-2",
            "standard",
            "0.5",
            "1",
            "2026-05-26T12:00:02Z",
        ))
        .unwrap();
        let acks = gw.process_batch("2026-05-26T12:00:02Z", 10);
        match &acks[0].1 {
            Err(GatewayError::ToxicityCircuitOpen { reason, .. }) => {
                assert!(reason.contains("fill_velocity"));
            }
            other => panic!("expected toxicity circuit, got {other:?}"),
        }
    }

    #[test]
    fn fill_accounting_keeps_yes_and_no_positions_separate() {
        let mut gw = fresh_gateway();
        gw.last_look.max_mark_movement_ticks = 3_000;
        gw.enqueue(place_intent_with_outcome(
            "p-yes",
            "c-yes",
            "standard",
            OutcomeSide::Yes,
            "0.4",
            "10",
            "2026-05-26T12:00:00Z",
        ))
        .unwrap();
        gw.enqueue(place_intent_with_outcome(
            "p-no",
            "c-no",
            "standard",
            OutcomeSide::No,
            "0.6",
            "7",
            "2026-05-26T12:00:00Z",
        ))
        .unwrap();
        let acks = gw.process_batch("2026-05-26T12:00:00Z", 10);
        assert!(acks.iter().all(|(_, res)| res.is_ok()));

        for (client_order_id, price, quantity) in [("c-yes", "0.4", "10"), ("c-no", "0.6", "7")] {
            gw.apply_fill(Fill {
                fill_id: format!("f-{client_order_id}"),
                client_order_id: client_order_id.into(),
                price: price.into(),
                quantity: quantity.into(),
                fee: "0".into(),
                trade_ts: "2026-05-26T12:00:01Z".into(),
            })
            .unwrap();
        }

        assert_eq!(
            gw.sleeve_state
                .positions
                .get(&outcome_position_key("kalshi:M-1", OutcomeSide::Yes)),
            Some(&Position {
                quantity: 10,
                avg_price_ticks: 4000,
            })
        );
        assert_eq!(
            gw.sleeve_state
                .positions
                .get(&outcome_position_key("kalshi:M-1", OutcomeSide::No)),
            Some(&Position {
                quantity: 7,
                avg_price_ticks: 6000,
            })
        );
        assert_eq!(gw.ledger[0].outcome_side, OutcomeSide::Yes);
        assert_eq!(gw.ledger[1].outcome_side, OutcomeSide::No);
    }

    #[test]
    fn portfolio_allows_sell_to_close_when_buy_would_exceed_cap() {
        let guard = PortfolioGuard::new(PortfolioPolicy::enabled("500.00").unwrap());
        let mut state = SleeveState::default();
        let key = outcome_position_key("kalshi:M-1", OutcomeSide::Yes);
        state.positions.insert(
            key.clone(),
            Position {
                quantity: 1000,
                avg_price_ticks: 5000,
            },
        );
        state.mark_price_ticks.insert(key, 5000);

        let buy = IntentSnapshot {
            client_order_id: "buy-more".into(),
            instrument_id: "kalshi:M-1".into(),
            outcome_side: OutcomeSide::Yes,
            side: Side::Buy,
            price: "0.50".into(),
            quantity: "1".into(),
            fair_price: None,
            min_executable_edge_ticks: None,
            fee_rate_bps: None,
        };
        assert!(matches!(
            guard.evaluate(&state, &buy),
            Err(PortfolioRejection::TotalGrossExceeded { .. })
        ));

        let sell = IntentSnapshot {
            client_order_id: "sell-close".into(),
            side: Side::Sell,
            ..buy
        };
        assert_eq!(guard.evaluate(&state, &sell), Ok(()));
    }

    #[test]
    fn portfolio_sell_into_short_increments_gross() {
        let guard = PortfolioGuard::new(PortfolioPolicy::enabled("79.00").unwrap());
        let mut state = SleeveState::default();
        let key = outcome_position_key("kalshi:M-1", OutcomeSide::Yes);
        state.positions.insert(
            key.clone(),
            Position {
                quantity: -100,
                avg_price_ticks: 4000,
            },
        );
        state.mark_price_ticks.insert(key, 4000);

        let sell = IntentSnapshot {
            client_order_id: "sell-more".into(),
            instrument_id: "kalshi:M-1".into(),
            outcome_side: OutcomeSide::Yes,
            side: Side::Sell,
            price: "0.40".into(),
            quantity: "100".into(),
            fair_price: None,
            min_executable_edge_ticks: None,
            fee_rate_bps: None,
        };
        assert!(matches!(
            guard.evaluate(&state, &sell),
            Err(PortfolioRejection::TotalGrossExceeded { .. })
        ));
    }

    #[test]
    fn adoption_that_breaches_projected_risk_engages_kill_switch() {
        let mut lim = limits();
        lim.max_position_notional = "50".into();
        let mut gw = DryRunGateway::new(RiskGate::new(lim), RecordingVenueClient::new());
        gw.adopt_resting_order(RestingOrderSnapshot {
            client_order_id: "adopted-risky".into(),
            venue_order_id: Some("venue-1".into()),
            instrument_id: "kalshi:M-1".into(),
            outcome_side: OutcomeSide::Yes,
            side: Side::Buy,
            price: "0.50".into(),
            quantity: "200".into(),
            filled_quantity: "0".into(),
            time_in_force: TimeInForce::Gtc,
            state: OrderState::Acked,
            updated_at: "2026-05-26T12:00:00Z".into(),
            observed_at: "2026-05-26T12:00:02Z".into(),
            reject_reason: None,
        })
        .unwrap();

        assert!(gw.sleeve_state.kill_switch_engaged);
        assert_eq!(gw.sleeve_state.open_orders, 1);
    }

    #[test]
    fn adoption_within_projected_risk_does_not_engage_kill_switch() {
        let mut gw = fresh_gateway();
        gw.adopt_resting_order(RestingOrderSnapshot {
            client_order_id: "adopted-safe".into(),
            venue_order_id: Some("venue-1".into()),
            instrument_id: "kalshi:M-1".into(),
            outcome_side: OutcomeSide::Yes,
            side: Side::Buy,
            price: "0.50".into(),
            quantity: "10".into(),
            filled_quantity: "0".into(),
            time_in_force: TimeInForce::Gtc,
            state: OrderState::Acked,
            updated_at: "2026-05-26T12:00:00Z".into(),
            observed_at: "2026-05-26T12:00:02Z".into(),
            reject_reason: None,
        })
        .unwrap();

        assert!(!gw.sleeve_state.kill_switch_engaged);
        assert_eq!(gw.sleeve_state.open_orders, 1);
    }

    #[test]
    fn adopted_order_does_not_block_intent_for_stale_quote() {
        let mut gw = fresh_gateway();
        let observed_at = "2026-05-26T12:00:02Z";
        let adopted_instrument = gw
            .adopt_resting_order(RestingOrderSnapshot {
                client_order_id: "adopted-fresh".into(),
                venue_order_id: Some("venue-1".into()),
                instrument_id: "kalshi:M-1".into(),
                outcome_side: OutcomeSide::Yes,
                side: Side::Buy,
                price: "0.50".into(),
                quantity: "1".into(),
                filled_quantity: "0".into(),
                time_in_force: TimeInForce::Gtc,
                state: OrderState::Acked,
                updated_at: "2026-05-26T10:00:00Z".into(),
                observed_at: observed_at.into(),
                reject_reason: None,
            })
            .unwrap();
        assert_eq!(adopted_instrument, "kalshi:M-1");

        let intent = IntentSnapshot {
            client_order_id: "fresh-after-adopt".into(),
            instrument_id: "kalshi:M-1".into(),
            outcome_side: OutcomeSide::Yes,
            side: Side::Buy,
            price: "0.50".into(),
            quantity: "1".into(),
            fair_price: None,
            min_executable_edge_ticks: None,
            fee_rate_bps: None,
        };
        let now_epoch = epoch_seconds(observed_at).unwrap();
        assert!(!matches!(
            gw.risk.evaluate(&gw.sleeve_state, &intent, now_epoch),
            RiskDecision::Rejected(
                RiskRejection::MissingMarketData { .. } | RiskRejection::StaleMarketData { .. }
            )
        ));
    }

    #[test]
    fn gateway_rejects_self_crossing_buy_against_own_sell() {
        let mut gw = fresh_gateway();
        gw.enqueue(place_intent_with_side(
            "sell-1",
            "c-sell",
            "standard",
            OutcomeSide::Yes,
            Side::Sell,
            "0.50",
            "10",
            "2026-05-26T12:00:00Z",
        ))
        .unwrap();
        let first = gw.process_batch("2026-05-26T12:00:00Z", 10);
        assert!(first[0].1.is_ok());

        gw.enqueue(place_intent_with_side(
            "buy-1",
            "c-buy",
            "standard",
            OutcomeSide::Yes,
            Side::Buy,
            "0.50",
            "10",
            "2026-05-26T12:00:01Z",
        ))
        .unwrap();
        let second = gw.process_batch("2026-05-26T12:00:01Z", 10);
        assert!(matches!(
            second[0].1.as_ref().unwrap_err(),
            GatewayError::SelfCross {
                resting_client_order_id,
                ..
            } if resting_client_order_id == "c-sell"
        ));
        assert!(gw.oms.get("c-buy").is_none());
        assert_eq!(gw.venue.submitted.len(), 1);
    }

    #[test]
    fn submit_transport_error_keeps_risk_reserved_as_submit_unknown() {
        let mut gw = DryRunGateway::new(RiskGate::new(limits()), FailingVenueClient);
        let now_epoch = epoch_seconds("2026-05-26T12:00:00Z").unwrap_or(0);
        gw.sleeve_state
            .last_quote_epoch_secs
            .insert("kalshi:M-1".into(), now_epoch);
        gw.sleeve_state
            .mark_price_ticks
            .insert("kalshi:M-1".into(), 5000);
        gw.enqueue(place_intent(
            "p1",
            "c-unknown",
            "standard",
            "0.5",
            "10",
            "2026-05-26T12:00:00Z",
        ))
        .unwrap();

        let acks = gw.process_batch("2026-05-26T12:00:00Z", 10);
        assert!(matches!(
            acks[0].1.as_ref().unwrap_err(),
            GatewayError::Transport(reason) if reason == "fixture_submit_timeout"
        ));
        let order = gw.oms.get("c-unknown").unwrap();
        assert_eq!(order.state, OrderState::SubmitUnknown);
        assert_eq!(gw.sleeve_state.open_orders, 1);
        let idem = gw.idempotency.record("p1").unwrap();
        assert!(idem.completed_at.is_none());
        assert!(idem.ack.is_none());
    }

    #[test]
    fn duplicate_fill_is_idempotent_through_gateway() {
        let mut gw = fresh_gateway();
        gw.enqueue(place_intent(
            "p1",
            "c-1",
            "standard",
            "0.5",
            "10",
            "2026-05-26T12:00:00Z",
        ))
        .unwrap();
        gw.process_batch("2026-05-26T12:00:00Z", 10);
        let fill = Fill {
            fill_id: "f-1".into(),
            client_order_id: "c-1".into(),
            price: "0.5".into(),
            quantity: "10".into(),
            fee: "0".into(),
            trade_ts: "2026-05-26T12:00:01Z".into(),
        };
        assert!(gw.apply_fill(fill.clone()).unwrap());
        assert!(!gw.apply_fill(fill).unwrap());
    }

    #[test]
    fn last_look_rejects_when_mark_moves_beyond_collar() {
        let mut gw = fresh_gateway();
        // Tight collar — any movement >1 tick (0.0001) trips the reject.
        gw.last_look.max_mark_movement_ticks = 1;
        gw.sleeve_state
            .mark_price_ticks
            .insert("kalshi:M-1".into(), 5000); // mark = $0.50
                                                // Intent priced at $0.42 — 800 ticks below mark; exceeds 1-tick collar.
        let intent = place_intent(
            "p1",
            "c-1",
            "standard",
            "0.42",
            "10",
            "2026-05-26T12:00:00Z",
        );
        gw.enqueue(intent).unwrap();
        let acks = gw.process_batch("2026-05-26T12:00:00Z", 10);
        match &acks[0].1 {
            Err(GatewayError::LastLook { reason, .. }) => {
                assert_eq!(*reason, "mark_moved_beyond_collar");
            }
            other => panic!("expected LastLook reject, got {other:?}"),
        }
        // OMS untouched — the order was never opened.
        assert!(gw.oms.get("c-1").is_none());
        assert_eq!(gw.venue.submitted.len(), 0);
    }

    #[test]
    fn last_look_compares_buy_against_side_specific_ask_not_mid() {
        let mut gw = fresh_gateway();
        let now_epoch = epoch_seconds("2026-05-26T12:00:00Z").unwrap_or(0);
        eventcontracts_risk::record_quote_bbo(
            &mut gw.sleeve_state,
            "kalshi:M-1",
            1000,
            9000,
            now_epoch,
        );
        gw.last_look.max_mark_movement_ticks = 1;
        let intent = place_intent("p1", "c-1", "standard", "0.9", "10", "2026-05-26T12:00:00Z");
        gw.enqueue(intent).unwrap();
        let acks = gw.process_batch("2026-05-26T12:00:00Z", 10);
        assert!(acks[0].1.is_ok());
    }

    #[test]
    fn last_look_rejects_live_without_executable_bbo() {
        let mut gw = fresh_gateway();
        gw.last_look.require_executable_bbo = true;
        let intent = place_intent("p1", "c-1", "standard", "0.5", "10", "2026-05-26T12:00:00Z");
        gw.enqueue(intent).unwrap();
        let acks = gw.process_batch("2026-05-26T12:00:00Z", 10);
        match &acks[0].1 {
            Err(GatewayError::LastLook { reason, .. }) => {
                assert_eq!(*reason, "no_executable_bbo");
            }
            other => panic!("expected no_executable_bbo, got {other:?}"),
        }
        assert!(gw.oms.get("c-1").is_none());
    }

    #[test]
    fn last_look_rejects_executable_order_larger_than_l1_depth() {
        let mut gw = fresh_gateway();
        let now_epoch = epoch_seconds("2026-05-26T12:00:00Z").unwrap_or(0);
        eventcontracts_risk::record_book_bbo(
            &mut gw.sleeve_state,
            "kalshi:M-1",
            4900,
            100,
            5100,
            5,
            now_epoch,
        );
        gw.last_look.require_executable_bbo = true;
        gw.last_look.require_l1_depth = true;
        let intent = place_intent(
            "p1",
            "c-1",
            "standard",
            "0.51",
            "10",
            "2026-05-26T12:00:00Z",
        );
        gw.enqueue(intent).unwrap();
        let acks = gw.process_batch("2026-05-26T12:00:00Z", 10);
        match &acks[0].1 {
            Err(GatewayError::LastLook { reason, .. }) => {
                assert_eq!(*reason, "l1_depth_insufficient");
            }
            other => panic!("expected l1_depth_insufficient, got {other:?}"),
        }
        assert!(gw.oms.get("c-1").is_none());
    }

    #[test]
    fn last_look_rejects_when_mark_is_absent() {
        let mut gw = fresh_gateway();
        gw.last_look.require_mark_price = true;
        // Strip the mark; the default fixture sets one.
        gw.sleeve_state.mark_price_ticks.clear();
        let intent = place_intent(
            "p1",
            "c-1",
            "standard",
            "0.42",
            "10",
            "2026-05-26T12:00:00Z",
        );
        gw.enqueue(intent).unwrap();
        let acks = gw.process_batch("2026-05-26T12:00:00Z", 10);
        match &acks[0].1 {
            Err(GatewayError::LastLook { reason, .. }) => assert_eq!(*reason, "no_mark_price"),
            other => panic!("expected LastLook no_mark_price, got {other:?}"),
        }
    }

    #[test]
    fn last_look_allows_when_mark_within_collar() {
        let mut gw = fresh_gateway();
        gw.sleeve_state
            .mark_price_ticks
            .insert("kalshi:M-1".into(), 4500); // mark = $0.45
                                                // Intent at $0.42 — 300 ticks below; default collar is 200 ticks. Set to 500.
        gw.last_look.max_mark_movement_ticks = 500;
        let intent = place_intent(
            "p1",
            "c-1",
            "standard",
            "0.42",
            "10",
            "2026-05-26T12:00:00Z",
        );
        gw.enqueue(intent).unwrap();
        let acks = gw.process_batch("2026-05-26T12:00:00Z", 10);
        assert!(acks[0].1.is_ok());
    }

    #[test]
    fn fee_adjusted_edge_fields_reach_gateway_risk_gate() {
        let mut gw = fresh_gateway();
        let payload = DecisionPayload::PlaceOrder {
            client_order_id: "c-fee-edge".into(),
            instrument_id: "kalshi:M-1".into(),
            outcome_side: OutcomeSide::Yes,
            side: Side::Buy,
            price: "0.50".into(),
            quantity: "1".into(),
            fair_price: Some("0.51".into()),
            min_executable_edge_ticks: Some(0),
            fee_rate_bps: Some(700),
            time_in_force: TimeInForce::Ioc,
        };
        gw.enqueue(IntentEnvelopeRecord {
            strategy_id: "weather-v1".into(),
            sleeve_id: "weather-kalshi-paper-a".into(),
            correlation_id: "fee-edge-reject".into(),
            emitted_at: "2026-05-26T12:00:00Z".into(),
            decision_kind: "place_order".into(),
            decision_json: serde_json::to_string(&payload).unwrap(),
            priority_tier: "standard".into(),
            audit: audit(),
        })
        .unwrap();

        let acks = gw.process_batch("2026-05-26T12:00:00Z", 10);
        assert!(matches!(
            acks[0].1.as_ref().unwrap_err(),
            GatewayError::RiskRejected(RiskRejection::NegativeEdgeAfterFees { .. })
        ));
        assert_eq!(gw.venue.submitted.len(), 0);
    }

    #[test]
    fn fee_adjusted_positive_edge_submits() {
        let mut gw = fresh_gateway();
        let payload = DecisionPayload::PlaceOrder {
            client_order_id: "c-fee-edge-ok".into(),
            instrument_id: "kalshi:M-1".into(),
            outcome_side: OutcomeSide::Yes,
            side: Side::Buy,
            price: "0.50".into(),
            quantity: "1".into(),
            fair_price: Some("0.53".into()),
            min_executable_edge_ticks: Some(0),
            fee_rate_bps: Some(700),
            time_in_force: TimeInForce::Ioc,
        };
        gw.enqueue(IntentEnvelopeRecord {
            strategy_id: "weather-v1".into(),
            sleeve_id: "weather-kalshi-paper-a".into(),
            correlation_id: "fee-edge-accept".into(),
            emitted_at: "2026-05-26T12:00:00Z".into(),
            decision_kind: "place_order".into(),
            decision_json: serde_json::to_string(&payload).unwrap(),
            priority_tier: "standard".into(),
            audit: audit(),
        })
        .unwrap();

        let acks = gw.process_batch("2026-05-26T12:00:00Z", 10);
        assert!(acks[0].1.is_ok(), "got {:?}", acks[0].1);
        assert_eq!(gw.venue.submitted.len(), 1);
    }

    #[test]
    fn age_secs_handles_year_boundary() {
        let a = age_secs("2025-12-31T23:59:55Z", "2026-01-01T00:00:05Z");
        assert_eq!(a, 10);
    }

    #[test]
    fn epoch_millis_preserves_subsecond_precision() {
        let base = epoch_millis("2026-05-26T12:00:01Z").unwrap();
        let with_ms = epoch_millis("2026-05-26T12:00:01.057Z").unwrap();
        assert_eq!(with_ms - base, 57);
    }

    // ---------- market state ----------

    #[test]
    fn place_order_on_paused_market_rejects_with_market_suspended() {
        let mut gw = fresh_gateway();
        let outcome = gw
            .apply_market_state(
                "kalshi:M-1",
                MarketState::Paused,
                Some("deactivated"),
                "2026-05-26T11:59:59Z",
            )
            .unwrap();
        assert_eq!(outcome.current, MarketState::Paused);
        assert!(outcome.canceled_client_order_ids.is_empty());

        let intent = place_intent("p1", "c-1", "standard", "0.5", "10", "2026-05-26T12:00:00Z");
        gw.enqueue(intent).unwrap();
        let acks = gw.process_batch("2026-05-26T12:00:00Z", 10);
        match &acks[0].1 {
            Err(GatewayError::MarketSuspended { instrument, state }) => {
                assert_eq!(instrument, "kalshi:M-1");
                assert_eq!(*state, MarketState::Paused);
            }
            other => panic!("expected MarketSuspended, got {other:?}"),
        }
        // OMS must remain untouched — the order was never opened.
        assert!(gw.oms.get("c-1").is_none());
        assert_eq!(gw.venue.submitted.len(), 0);
    }

    #[test]
    fn place_order_on_opened_market_succeeds() {
        let mut gw = fresh_gateway();
        gw.apply_market_state(
            "kalshi:M-1",
            MarketState::Opened,
            Some("activated"),
            "2026-05-26T11:59:59Z",
        )
        .unwrap();
        // BBO was wiped on the (None → tradable) transition because there
        // was no previous state; restore it so last-look passes.
        let now_epoch = epoch_seconds("2026-05-26T12:00:00Z").unwrap_or(0);
        gw.sleeve_state
            .last_quote_epoch_secs
            .insert("kalshi:M-1".into(), now_epoch);
        gw.sleeve_state
            .mark_price_ticks
            .insert("kalshi:M-1".into(), 5000);

        let intent = place_intent("p1", "c-1", "standard", "0.5", "10", "2026-05-26T12:00:00Z");
        gw.enqueue(intent).unwrap();
        let acks = gw.process_batch("2026-05-26T12:00:00Z", 10);
        assert!(acks[0].1.is_ok(), "got {:?}", acks[0].1);
        assert_eq!(gw.venue.submitted.len(), 1);
    }

    #[test]
    fn place_order_without_known_state_passes_unless_strict_mode() {
        let mut gw = fresh_gateway();
        // Default: market_state map empty → fall through.
        let intent = place_intent("p1", "c-1", "standard", "0.5", "10", "2026-05-26T12:00:00Z");
        gw.enqueue(intent).unwrap();
        let acks = gw.process_batch("2026-05-26T12:00:00Z", 10);
        assert!(acks[0].1.is_ok());

        // Strict mode: an unknown market must be treated as not-yet-trading.
        let mut gw2 = fresh_gateway();
        gw2.require_market_state = true;
        let intent2 = place_intent("p2", "c-2", "standard", "0.5", "10", "2026-05-26T12:00:00Z");
        gw2.enqueue(intent2).unwrap();
        let acks2 = gw2.process_batch("2026-05-26T12:00:00Z", 10);
        assert!(matches!(
            acks2[0].1.as_ref().unwrap_err(),
            GatewayError::MarketSuspended {
                state: MarketState::Listed,
                ..
            }
        ));
    }

    #[test]
    fn pause_transition_cancels_open_orders_for_only_that_instrument() {
        let mut gw = fresh_gateway();
        // Seed quote freshness for two instruments so both place attempts
        // clear risk.
        let now_epoch = epoch_seconds("2026-05-26T12:00:00Z").unwrap_or(0);
        for inst in ["kalshi:M-1", "kalshi:M-OTHER"] {
            gw.sleeve_state
                .last_quote_epoch_secs
                .insert(inst.into(), now_epoch);
            gw.sleeve_state.mark_price_ticks.insert(inst.into(), 5000);
        }
        // Place one order on each instrument.
        gw.enqueue(place_intent(
            "p1",
            "c-target",
            "standard",
            "0.5",
            "10",
            "2026-05-26T12:00:00Z",
        ))
        .unwrap();
        let other = {
            let payload = DecisionPayload::PlaceOrder {
                client_order_id: "c-other".into(),
                instrument_id: "kalshi:M-OTHER".into(),
                outcome_side: OutcomeSide::Yes,
                side: Side::Buy,
                price: "0.5".into(),
                quantity: "10".into(),
                fair_price: None,
                min_executable_edge_ticks: None,
                fee_rate_bps: None,
                time_in_force: TimeInForce::Gtc,
            };
            IntentEnvelopeRecord {
                strategy_id: "weather-v1".into(),
                sleeve_id: "weather-kalshi-paper-a".into(),
                correlation_id: "p-other".into(),
                emitted_at: "2026-05-26T12:00:00Z".into(),
                decision_kind: "place_order".into(),
                decision_json: serde_json::to_string(&payload).unwrap(),
                priority_tier: "standard".into(),
                audit: audit(),
            }
        };
        gw.enqueue(other).unwrap();
        let initial = gw.process_batch("2026-05-26T12:00:00Z", 10);
        assert!(initial.iter().all(|(_, r)| r.is_ok()));
        // OMS confirms both resting.
        gw.apply_order_update("c-target", "resting", "2026-05-26T12:00:00Z", None)
            .unwrap();
        gw.apply_order_update("c-other", "resting", "2026-05-26T12:00:00Z", None)
            .unwrap();
        assert_eq!(gw.sleeve_state.open_orders, 2);

        // Pause the target market — the other instrument's order must not
        // be touched.
        let outcome = gw
            .apply_market_state(
                "kalshi:M-1",
                MarketState::Paused,
                Some("deactivated"),
                "2026-05-26T12:01:00Z",
            )
            .unwrap();
        assert_eq!(
            outcome.canceled_client_order_ids,
            vec!["c-target".to_string()]
        );

        // The synthesized cancel must drain ahead of anything else.
        let acks = gw.process_batch("2026-05-26T12:01:00Z", 10);
        assert_eq!(acks.len(), 1);
        assert!(acks[0].1.is_ok());
        assert_eq!(gw.venue.canceled.len(), 1);
        assert_eq!(gw.venue.canceled[0].1, "c-target");
    }

    #[test]
    fn pause_invalidates_bbo_and_mark_for_instrument() {
        let mut gw = fresh_gateway();
        gw.apply_market_state(
            "kalshi:M-1",
            MarketState::Paused,
            None,
            "2026-05-26T12:00:00Z",
        )
        .unwrap();
        assert!(!gw.sleeve_state.mark_price_ticks.contains_key("kalshi:M-1"));
        assert!(!gw
            .sleeve_state
            .last_quote_epoch_secs
            .contains_key("kalshi:M-1"));
    }

    #[test]
    fn metadata_update_does_not_suspend_or_cancel_resting_orders() {
        let mut gw = fresh_gateway();
        gw.apply_market_state(
            "kalshi:M-1",
            MarketState::Opened,
            Some("activated"),
            "2026-05-26T11:59:59Z",
        )
        .unwrap();
        gw.sleeve_state
            .best_bid_ticks
            .insert("kalshi:M-1".into(), 4900);
        gw.sleeve_state
            .best_ask_ticks
            .insert("kalshi:M-1".into(), 5100);

        gw.enqueue(place_intent(
            "p1",
            "c-resting",
            "standard",
            "0.5",
            "10",
            "2026-05-26T12:00:00Z",
        ))
        .unwrap();
        let initial = gw.process_batch("2026-05-26T12:00:00Z", 10);
        assert!(initial[0].1.is_ok(), "got {:?}", initial[0].1);
        gw.apply_order_update("c-resting", "resting", "2026-05-26T12:00:00Z", None)
            .unwrap();
        assert_eq!(gw.sleeve_state.open_orders, 1);

        let outcome = gw
            .apply_market_state(
                "kalshi:M-1",
                MarketState::MetadataUpdated,
                Some("close_date_updated"),
                "2026-05-26T12:01:00Z",
            )
            .unwrap();
        assert_eq!(outcome.previous, Some(MarketState::Opened));
        assert_eq!(outcome.current, MarketState::MetadataUpdated);
        assert!(outcome.canceled_client_order_ids.is_empty());
        assert_eq!(
            gw.market_state.get("kalshi:M-1"),
            Some(&MarketState::Opened)
        );
        assert_eq!(gw.sleeve_state.open_orders, 1);
        assert!(gw.venue.canceled.is_empty());
        assert!(gw.sleeve_state.mark_price_ticks.contains_key("kalshi:M-1"));
        assert!(gw
            .sleeve_state
            .last_quote_epoch_secs
            .contains_key("kalshi:M-1"));
    }

    // ---------- async path ----------

    #[tokio::test]
    async fn process_batch_async_drives_the_same_pipeline_as_the_sync_path() {
        let mut gw = fresh_gateway();
        gw.enqueue(place_intent(
            "p1",
            "c-1",
            "standard",
            "0.5",
            "10",
            "2026-05-26T12:00:00Z",
        ))
        .unwrap();
        let acks = gw.process_batch_async("2026-05-26T12:00:00Z", 10).await;
        assert_eq!(acks.len(), 1);
        let ack = acks[0].1.as_ref().expect("ack");
        assert!(ack.accepted);
        assert_eq!(gw.venue.submitted.len(), 1);
        let order = gw.oms.get("c-1").unwrap();
        assert_eq!(order.state, OrderState::Submitted);
        assert!(order.venue_order_id.is_some());
        assert_eq!(gw.sleeve_state.open_orders, 1);
    }

    #[tokio::test]
    async fn process_batch_async_propagates_risk_rejection_without_touching_venue() {
        let mut gw = fresh_gateway();
        gw.enqueue(place_intent(
            "p1",
            "c-1",
            "standard",
            "0.5",
            "10000", // notional = 5000 > max_order_notional = 500
            "2026-05-26T12:00:00Z",
        ))
        .unwrap();
        let acks = gw.process_batch_async("2026-05-26T12:00:00Z", 10).await;
        assert!(matches!(
            acks[0].1.as_ref().unwrap_err(),
            GatewayError::RiskRejected(_)
        ));
        assert_eq!(gw.venue.submitted.len(), 0);
        assert!(gw.oms.get("c-1").is_none());
    }

    #[tokio::test]
    async fn process_batch_async_blocks_suspended_market_at_prepare_stage() {
        let mut gw = fresh_gateway();
        gw.apply_market_state(
            "kalshi:M-1",
            MarketState::Paused,
            Some("deactivated"),
            "2026-05-26T11:59:59Z",
        )
        .unwrap();
        gw.enqueue(place_intent(
            "p1",
            "c-1",
            "standard",
            "0.5",
            "10",
            "2026-05-26T12:00:00Z",
        ))
        .unwrap();
        let acks = gw.process_batch_async("2026-05-26T12:00:00Z", 10).await;
        assert!(matches!(
            acks[0].1.as_ref().unwrap_err(),
            GatewayError::MarketSuspended {
                state: MarketState::Paused,
                ..
            }
        ));
        assert_eq!(gw.venue.submitted.len(), 0);
    }

    #[test]
    fn reopen_transition_invalidates_bbo_so_last_look_blocks_until_fresh_quote() {
        let mut gw = fresh_gateway();
        // Force a known prior state of Paused so the reopen sees a real
        // non-tradable → tradable transition.
        gw.apply_market_state(
            "kalshi:M-1",
            MarketState::Paused,
            None,
            "2026-05-25T00:00:00Z",
        )
        .unwrap();
        // Operator/operator-fixture sets a (now stale) mark+quote.
        gw.sleeve_state
            .mark_price_ticks
            .insert("kalshi:M-1".into(), 5000);
        let stale_epoch = epoch_seconds("2026-05-26T11:00:00Z").unwrap();
        gw.sleeve_state
            .last_quote_epoch_secs
            .insert("kalshi:M-1".into(), stale_epoch);

        let outcome = gw
            .apply_market_state(
                "kalshi:M-1",
                MarketState::Resumed,
                Some("resumed"),
                "2026-05-26T12:00:00Z",
            )
            .unwrap();
        assert!(outcome.bbo_invalidated_for_reopen);
        // Cached BBO/mark must be wiped — fresh quote required before
        // last-look passes.
        assert!(!gw.sleeve_state.mark_price_ticks.contains_key("kalshi:M-1"));
        assert!(!gw
            .sleeve_state
            .last_quote_epoch_secs
            .contains_key("kalshi:M-1"));
    }
}
