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

use eventcontracts_contracts::IntentEnvelopeRecord;
use eventcontracts_oms::{Fill, InMemoryOms, OmsError, Order, OrderState, Side, TimeInForce};
use eventcontracts_risk::{IntentSnapshot, RiskDecision, RiskGate, RiskRejection, SleeveState};
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
    #[error("oms transition failed: {0}")]
    Oms(#[from] OmsError),
    #[error("venue transport error: {0}")]
    Transport(String),
}

// ---------- decision payload ----------

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case", tag = "kind")]
pub enum DecisionPayload {
    PlaceOrder {
        client_order_id: String,
        instrument_id: String,
        side: Side,
        price: String,
        quantity: String,
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

/// Three bounded FIFOs by tier, plus a separate cancel queue that drains
/// ahead of everything else.
#[derive(Debug, Default)]
pub struct PriorityScheduler {
    cancels: VecDeque<IntentEnvelopeRecord>,
    fast: VecDeque<IntentEnvelopeRecord>,
    standard: VecDeque<IntentEnvelopeRecord>,
    relaxed: VecDeque<IntentEnvelopeRecord>,
}

impl PriorityScheduler {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn enqueue(
        &mut self,
        envelope: IntentEnvelopeRecord,
        payload: &DecisionPayload,
    ) -> Result<(), GatewayError> {
        if matches!(payload, DecisionPayload::CancelOrder { .. }) {
            self.cancels.push_back(envelope);
            return Ok(());
        }
        let tier = PriorityTier::from_wire(&envelope.priority_tier)?;
        match tier {
            PriorityTier::Fast => self.fast.push_back(envelope),
            PriorityTier::Standard => self.standard.push_back(envelope),
            PriorityTier::Relaxed => self.relaxed.push_back(envelope),
        }
        Ok(())
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

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
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
}

// ---------- dry-run gateway ----------

pub struct DryRunGateway<C: VenueClient> {
    pub scheduler: PriorityScheduler,
    pub idempotency: IdempotencyStore,
    pub oms: InMemoryOms,
    pub risk: RiskGate,
    pub sleeve_state: SleeveState,
    pub venue: C,
    pub max_intent_age_secs: i64,
    pub max_cancel_age_secs: i64,
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
            max_intent_age_secs: 30,
            max_cancel_age_secs: 5,
        }
    }

    pub fn enqueue(&mut self, envelope: IntentEnvelopeRecord) -> Result<(), GatewayError> {
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
        let payload: DecisionPayload = serde_json::from_str(&envelope.decision_json)
            .map_err(|e| GatewayError::DecisionDecode(e.to_string()))?;

        let is_cancel = matches!(payload, DecisionPayload::CancelOrder { .. });
        let max_age = if is_cancel {
            self.max_cancel_age_secs
        } else {
            self.max_intent_age_secs
        };
        let age = age_secs(&envelope.emitted_at, now);
        if age > max_age {
            return Err(GatewayError::Stale {
                age_secs: age,
                limit_secs: max_age,
            });
        }

        self.idempotency.reserve(&envelope.correlation_id, now)?;

        match payload {
            DecisionPayload::PlaceOrder {
                client_order_id,
                instrument_id,
                side,
                price,
                quantity,
                time_in_force,
            } => {
                let snap = IntentSnapshot {
                    client_order_id: client_order_id.clone(),
                    instrument_id: instrument_id.clone(),
                    side,
                    price: price.clone(),
                    quantity: quantity.clone(),
                };
                match self.risk.evaluate(&self.sleeve_state, &snap) {
                    RiskDecision::Approved => {}
                    RiskDecision::Rejected(reason) => {
                        return Err(GatewayError::RiskRejected(reason));
                    }
                }
                let order = Order::new(
                    &client_order_id,
                    &instrument_id,
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
                    side,
                    price,
                    quantity,
                    time_in_force,
                };
                let ack = self.venue.submit(&envelope, &payload_for_venue)?;
                if ack.accepted {
                    if let Some(venue_id) = &ack.venue_order_id {
                        self.oms.set_venue_order_id(&client_order_id, venue_id)?;
                    }
                    self.oms.transition(
                        &client_order_id,
                        OrderState::Acked,
                        &ack.acknowledged_at,
                        None,
                    )?;
                    self.sleeve_state.open_orders += 1;
                } else {
                    self.oms.transition(
                        &client_order_id,
                        OrderState::Rejected,
                        &ack.acknowledged_at,
                        Some(&ack.reasons.join(",")),
                    )?;
                }
                self.idempotency
                    .mark_complete(&envelope.correlation_id, ack.clone(), now)?;
                Ok(ack)
            }
            DecisionPayload::CancelOrder { client_order_id } => {
                let ack = self.venue.cancel(&envelope, &client_order_id)?;
                self.oms.transition(
                    &client_order_id,
                    OrderState::Canceled,
                    &ack.acknowledged_at,
                    None,
                )?;
                if self.sleeve_state.open_orders > 0 {
                    self.sleeve_state.open_orders -= 1;
                }
                self.idempotency
                    .mark_complete(&envelope.correlation_id, ack.clone(), now)?;
                Ok(ack)
            }
        }
    }

    /// Mark a fill received from the venue. Idempotent via the OMS dedupe.
    pub fn apply_fill(&mut self, fill: Fill) -> Result<bool, GatewayError> {
        let applied = self.oms.apply_fill(fill)?;
        if applied {
            // Open-order count tracks live (non-terminal) orders. After a fill,
            // an order may have become terminal — recompute from OMS.
            self.sleeve_state.open_orders = self.oms.open_orders().count() as u32;
        }
        Ok(applied)
    }
}

// ---------- helpers ----------

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
        let payload = DecisionPayload::PlaceOrder {
            client_order_id: cid.into(),
            instrument_id: "kalshi:M-1".into(),
            side: Side::Buy,
            price: price.into(),
            quantity: qty.into(),
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
    fn idempotency_blocks_duplicate_correlation_id() {
        let mut store = IdempotencyStore::new();
        store.reserve("corr-1", "2026-05-26T12:00:00Z").unwrap();
        let err = store.reserve("corr-1", "2026-05-26T12:00:01Z").unwrap_err();
        assert!(matches!(err, GatewayError::Idempotency(_)));
    }

    #[test]
    fn end_to_end_place_routes_through_risk_oms_and_records_in_venue() {
        let venue = RecordingVenueClient::new();
        let mut gw = DryRunGateway::new(RiskGate::new(limits()), venue);
        let intent = place_intent("p1", "c-1", "standard", "0.5", "10", "2026-05-26T12:00:00Z");
        gw.enqueue(intent).unwrap();
        let acks = gw.process_batch("2026-05-26T12:00:00Z", 10);
        assert_eq!(acks.len(), 1);
        let (_cid, res) = &acks[0];
        let ack = res.as_ref().unwrap();
        assert!(ack.accepted);
        assert_eq!(gw.venue.submitted.len(), 1);
        let order = gw.oms.get("c-1").unwrap();
        assert_eq!(order.state, OrderState::Acked);
        assert!(order.venue_order_id.is_some());
        assert_eq!(gw.sleeve_state.open_orders, 1);
    }

    #[test]
    fn risk_rejection_short_circuits_before_venue_submit() {
        let venue = RecordingVenueClient::new();
        let mut gw = DryRunGateway::new(RiskGate::new(limits()), venue);
        // 10000 * 0.5 = 5000 notional, max_order_notional is 500
        let intent = place_intent("p1", "c-1", "standard", "0.5", "10000", "2026-05-26T12:00:00Z");
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
        let venue = RecordingVenueClient::new();
        let mut gw = DryRunGateway::new(RiskGate::new(limits()), venue);
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
        let venue = RecordingVenueClient::new();
        let mut gw = DryRunGateway::new(RiskGate::new(limits()), venue);
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
        let venue = RecordingVenueClient::new();
        let mut gw = DryRunGateway::new(RiskGate::new(limits()), venue);
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
        let order = gw.oms.get("c-1").unwrap();
        assert_eq!(order.state, OrderState::Canceled);
        assert_eq!(gw.sleeve_state.open_orders, 0);
    }

    #[test]
    fn fill_applied_after_ack_promotes_to_filled_and_decrements_open_count() {
        let venue = RecordingVenueClient::new();
        let mut gw = DryRunGateway::new(RiskGate::new(limits()), venue);
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
    }

    #[test]
    fn duplicate_fill_is_idempotent_through_gateway() {
        let venue = RecordingVenueClient::new();
        let mut gw = DryRunGateway::new(RiskGate::new(limits()), venue);
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
    fn age_secs_handles_year_boundary() {
        let a = age_secs("2025-12-31T23:59:55Z", "2026-01-01T00:00:05Z");
        assert_eq!(a, 10);
    }
}
