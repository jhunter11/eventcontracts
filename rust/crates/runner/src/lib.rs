//! Sleeve runner. Connects EventSource → StrategyRuntime → RiskGate →
//! IntentSink, drains until the source is exhausted, and produces a
//! `RunSummary`.
//!
//! ## Python research → Rust execution
//!
//! `spec::StrategySpecArtifact` parses the same `configs/strategies/*.toml`
//! file the Python framework consumes. `registry::default_registry()` maps
//! the spec's `name` field to a registered Rust factory. To promote a new
//! Python strategy:
//!
//! 1. Implement `FromSpec + StrategyRuntime` in a new file under
//!    `runner/src/` (or another crate).
//! 2. Register it with one `r.register("python_name", ...)` line in
//!    `registry::default_registry()`.
//! 3. Add parity cases under `contracts/parity/<name>/`.
//!
//! The `ThresholdStrategy` below doubles as the Rust impl for the example
//! `weather_threshold` and `example_threshold` strategy names — it shows the
//! pattern in <50 lines.

pub mod registry;
pub mod spec;

pub use registry::{default_registry, FromSpec, StrategyRegistry};
pub use spec::{SpecError, StrategySpecArtifact};

use eventcontracts_contracts::{
    AuditStamp, Contract, IntentEnvelopeRecord, Metadata, NormalizedEventRecord,
};
use eventcontracts_gateway::{DecisionPayload, DryRunGateway, GatewayError, VenueClient};
use eventcontracts_oms::{Side, TimeInForce};
use eventcontracts_risk::{IntentSnapshot, RiskDecision, RiskGate, RiskRejection, SleeveState};
use serde::{Deserialize, Serialize};
use std::collections::VecDeque;
use thiserror::Error;

#[derive(Debug, Error, Clone, PartialEq)]
pub enum RunnerError {
    #[error("event source error: {0}")]
    EventSource(String),
    #[error("strategy error: {0}")]
    Strategy(String),
    #[error("decision payload error: {0}")]
    Decision(String),
    #[error("sink error: {0}")]
    Sink(String),
    #[error("gateway error: {0}")]
    Gateway(String),
}

impl From<GatewayError> for RunnerError {
    fn from(value: GatewayError) -> Self {
        RunnerError::Gateway(value.to_string())
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct RunSummary {
    pub sleeve_id: String,
    pub strategy_id: String,
    pub events_processed: u64,
    pub decisions_emitted: u64,
    pub intents_approved: u64,
    pub intents_rejected_by_risk: u64,
    pub gateway_acks: u64,
    pub gateway_errors: u64,
}

// ---------- traits ----------

pub trait EventSource {
    fn next_event(&mut self) -> Result<Option<NormalizedEventRecord>, RunnerError>;
}

pub trait StrategyRuntime {
    fn strategy_id(&self) -> &str;
    fn sleeve_id(&self) -> &str;
    fn on_event(
        &mut self,
        event: &NormalizedEventRecord,
    ) -> Result<Vec<DecisionPayload>, RunnerError>;
}

pub trait IntentSink {
    fn emit(&mut self, envelope: IntentEnvelopeRecord) -> Result<(), RunnerError>;
}

// ---------- in-memory bus ----------

#[derive(Debug, Default)]
pub struct InMemoryBus {
    queue: VecDeque<NormalizedEventRecord>,
    pub emitted: Vec<IntentEnvelopeRecord>,
}

impl InMemoryBus {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn publish(&mut self, event: NormalizedEventRecord) {
        self.queue.push_back(event);
    }
}

impl EventSource for InMemoryBus {
    fn next_event(&mut self) -> Result<Option<NormalizedEventRecord>, RunnerError> {
        Ok(self.queue.pop_front())
    }
}

impl IntentSink for InMemoryBus {
    fn emit(&mut self, envelope: IntentEnvelopeRecord) -> Result<(), RunnerError> {
        self.emitted.push(envelope);
        Ok(())
    }
}

// ---------- threshold strategy (Rust impl for the python "weather_threshold"
// and "example_threshold" specs) ----------

/// On each `quote` event, emits a single `PlaceOrder`:
/// - **Buy** at `ask` when `mid < buy_below`.
/// - **Sell** at `bid` when `mid > sell_above`.
/// - `NoAction` otherwise.
///
/// Parameters are read from the TOML spec (`buy_below`, `sell_above`,
/// `size`), matching the Python reference at
/// `contracts/examples/weather_threshold/strategy_spec.toml`. The instrument
/// is taken from the event itself, so one instance handles many tickers.
pub struct ThresholdStrategy {
    pub strategy_id: String,
    pub sleeve_id: String,
    pub buy_below: f64,
    pub sell_above: f64,
    pub size: String,
    next_client_order: u64,
}

impl ThresholdStrategy {
    pub fn new(
        strategy_id: impl Into<String>,
        sleeve_id: impl Into<String>,
        buy_below: f64,
        sell_above: f64,
        size: impl Into<String>,
    ) -> Self {
        Self {
            strategy_id: strategy_id.into(),
            sleeve_id: sleeve_id.into(),
            buy_below,
            sell_above,
            size: size.into(),
            next_client_order: 0,
        }
    }
}

impl FromSpec for ThresholdStrategy {
    fn from_spec(spec: &StrategySpecArtifact) -> Result<Self, SpecError> {
        // `buy_below = 0` and `sell_above = 1` both effectively disable a
        // side; that's how a researcher silences a leg without removing it.
        let buy_below = spec.param_f64_or("buy_below", 0.0)?;
        let sell_above = spec.param_f64_or("sell_above", 1.0)?;
        let size = spec.param_str_or("size", "1");
        // `sleeve_id` for live runs is supplied by the caller; default to a
        // research-friendly synthetic value derived from strategy_id so the
        // strategy still works when no sleeve is provided.
        let sleeve_id = format!("{}-sleeve", spec.strategy_id);
        Ok(Self::new(
            spec.strategy_id.clone(),
            sleeve_id,
            buy_below,
            sell_above,
            size,
        ))
    }
}

impl StrategyRuntime for ThresholdStrategy {
    fn strategy_id(&self) -> &str {
        &self.strategy_id
    }
    fn sleeve_id(&self) -> &str {
        &self.sleeve_id
    }
    fn on_event(
        &mut self,
        event: &NormalizedEventRecord,
    ) -> Result<Vec<DecisionPayload>, RunnerError> {
        if event.event_kind != "quote" {
            return Ok(vec![]);
        }
        let payload: serde_json::Value = serde_json::from_str(&event.payload_json)
            .map_err(|e| RunnerError::Strategy(e.to_string()))?;
        let bid = parse_decimal_field(&payload, "bid")?;
        let ask = parse_decimal_field(&payload, "ask")?;
        if !(bid > 0.0 && ask > 0.0) {
            return Ok(vec![]);
        }
        let mid = (bid + ask) / 2.0;
        let instrument = payload
            .get("instrument")
            .and_then(|v| v.as_str())
            .unwrap_or("unknown")
            .to_string();

        if self.buy_below > 0.0 && mid < self.buy_below {
            self.next_client_order += 1;
            return Ok(vec![DecisionPayload::PlaceOrder {
                client_order_id: format!("c-buy-{:08}", self.next_client_order),
                instrument_id: instrument,
                side: Side::Buy,
                price: format!("{ask}"),
                quantity: self.size.clone(),
                time_in_force: TimeInForce::Ioc,
            }]);
        }
        if self.sell_above < 1.0 && mid > self.sell_above {
            self.next_client_order += 1;
            return Ok(vec![DecisionPayload::PlaceOrder {
                client_order_id: format!("c-sell-{:08}", self.next_client_order),
                instrument_id: instrument,
                side: Side::Sell,
                price: format!("{bid}"),
                quantity: self.size.clone(),
                time_in_force: TimeInForce::Ioc,
            }]);
        }
        Ok(vec![])
    }
}

fn parse_decimal_field(value: &serde_json::Value, key: &str) -> Result<f64, RunnerError> {
    match value.get(key) {
        Some(serde_json::Value::String(s)) => s.parse::<f64>().map_err(|e| {
            RunnerError::Strategy(format!("field `{key}` not a decimal: {e}"))
        }),
        Some(serde_json::Value::Number(n)) => n.as_f64().ok_or_else(|| {
            RunnerError::Strategy(format!("field `{key}` number not representable as f64"))
        }),
        _ => Err(RunnerError::Strategy(format!("missing field `{key}`"))),
    }
}

// ---------- sleeve runner ----------

pub struct SleeveRunner<'a, S: StrategyRuntime, E: EventSource, K: VenueClient> {
    pub strategy: &'a mut S,
    pub source: &'a mut E,
    pub risk: &'a RiskGate,
    pub sleeve_state: &'a mut SleeveState,
    pub sink: &'a mut dyn IntentSink,
    pub gateway: Option<&'a mut DryRunGateway<K>>,
    pub now_provider: Box<dyn FnMut() -> String + 'a>,
    next_corr: u64,
}

impl<'a, S: StrategyRuntime, E: EventSource, K: VenueClient> SleeveRunner<'a, S, E, K> {
    pub fn new(
        strategy: &'a mut S,
        source: &'a mut E,
        risk: &'a RiskGate,
        sleeve_state: &'a mut SleeveState,
        sink: &'a mut dyn IntentSink,
        gateway: Option<&'a mut DryRunGateway<K>>,
        now_provider: Box<dyn FnMut() -> String + 'a>,
    ) -> Self {
        Self {
            strategy,
            source,
            risk,
            sleeve_state,
            sink,
            gateway,
            now_provider,
            next_corr: 0,
        }
    }

    pub fn run(&mut self) -> Result<RunSummary, RunnerError> {
        let mut summary = RunSummary {
            sleeve_id: self.strategy.sleeve_id().to_string(),
            strategy_id: self.strategy.strategy_id().to_string(),
            events_processed: 0,
            decisions_emitted: 0,
            intents_approved: 0,
            intents_rejected_by_risk: 0,
            gateway_acks: 0,
            gateway_errors: 0,
        };

        while let Some(event) = self.source.next_event()? {
            summary.events_processed += 1;
            let decisions = self.strategy.on_event(&event)?;
            summary.decisions_emitted += decisions.len() as u64;

            for decision in decisions {
                let envelope = self.wrap_envelope(&decision)?;
                let approved = match &decision {
                    DecisionPayload::PlaceOrder {
                        client_order_id,
                        instrument_id,
                        side,
                        price,
                        quantity,
                        ..
                    } => {
                        let snap = IntentSnapshot {
                            client_order_id: client_order_id.clone(),
                            instrument_id: instrument_id.clone(),
                            side: *side,
                            price: price.clone(),
                            quantity: quantity.clone(),
                        };
                        match self.risk.evaluate(self.sleeve_state, &snap) {
                            RiskDecision::Approved => true,
                            RiskDecision::Rejected(reason) => {
                                summary.intents_rejected_by_risk += 1;
                                self.note_rejection(&envelope, &reason);
                                false
                            }
                        }
                    }
                    DecisionPayload::CancelOrder { .. } => true,
                };

                if !approved {
                    continue;
                }

                summary.intents_approved += 1;
                self.sink.emit(envelope.clone())?;
                if let Some(gw) = self.gateway.as_deref_mut() {
                    gw.enqueue(envelope)?;
                    let acks = gw.process_batch(&(self.now_provider)(), 64);
                    for (_cid, res) in acks {
                        match res {
                            Ok(ack) if ack.accepted => {
                                summary.gateway_acks += 1;
                                self.sleeve_state.open_orders = gw.sleeve_state.open_orders;
                            }
                            Ok(_) => summary.gateway_errors += 1,
                            Err(_) => summary.gateway_errors += 1,
                        }
                    }
                }
            }
        }
        Ok(summary)
    }

    fn wrap_envelope(
        &mut self,
        decision: &DecisionPayload,
    ) -> Result<IntentEnvelopeRecord, RunnerError> {
        self.next_corr += 1;
        let now = (self.now_provider)();
        let decision_json = serde_json::to_string(decision)
            .map_err(|e| RunnerError::Decision(e.to_string()))?;
        let decision_kind = match decision {
            DecisionPayload::PlaceOrder { .. } => "place_order",
            DecisionPayload::CancelOrder { .. } => "cancel_order",
        }
        .to_string();
        let envelope = IntentEnvelopeRecord {
            strategy_id: self.strategy.strategy_id().to_string(),
            sleeve_id: self.strategy.sleeve_id().to_string(),
            correlation_id: format!(
                "{}-{:08}",
                self.strategy.sleeve_id(),
                self.next_corr
            ),
            emitted_at: now.clone(),
            decision_kind,
            decision_json,
            priority_tier: "standard".into(),
            audit: AuditStamp {
                object_id: format!("intent-{:08}", self.next_corr),
                object_kind: "intent_envelope".into(),
                schema_version: "intent-envelope-v1".into(),
                produced_at: now,
                producer: "runner".into(),
                canonical_sha256: "0".repeat(64),
                parent_ids: vec![],
                trace_id: None,
                metadata: Metadata::new(),
            },
        };
        envelope
            .validate()
            .map_err(|e| RunnerError::Decision(e.to_string()))?;
        Ok(envelope)
    }

    fn note_rejection(&self, _envelope: &IntentEnvelopeRecord, _reason: &RiskRejection) {
        // Future: emit a risk-rejection audit record. For now the count goes
        // into RunSummary; the runner itself does not log to stdout.
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use eventcontracts_contracts::EventProvenance;
    use eventcontracts_gateway::RecordingVenueClient;
    use eventcontracts_risk::RiskLimits;

    fn audit() -> AuditStamp {
        AuditStamp {
            object_id: "event-1".into(),
            object_kind: "normalized_event".into(),
            schema_version: "normalized-event-v1".into(),
            produced_at: "2026-05-26T12:00:00Z".into(),
            producer: "fixture".into(),
            canonical_sha256: "0".repeat(64),
            parent_ids: vec![],
            trace_id: None,
            metadata: Metadata::new(),
        }
    }

    fn provenance() -> EventProvenance {
        EventProvenance {
            source: "kalshi-ws".into(),
            channel: "market_data".into(),
            schema_version: "normalized-event-v1".into(),
            venue: Some("kalshi".into()),
            source_sequence: None,
            normalization_version: "1".into(),
            metadata: Metadata::new(),
        }
    }

    fn quote(bid: &str, ask: &str) -> NormalizedEventRecord {
        let payload = serde_json::json!({
            "instrument": "kalshi:M-1",
            "bid": bid,
            "ask": ask,
        });
        NormalizedEventRecord {
            event_id: format!("e-{bid}-{ask}"),
            event_kind: "quote".into(),
            payload_json: payload.to_string(),
            provenance: provenance(),
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
    fn threshold_strategy_emits_intent_when_mid_below_threshold() {
        let mut strat = ThresholdStrategy::new(
            "weather-v1",
            "weather-kalshi-paper-a",
            0.5,
            1.0,
            "10",
        );
        let decisions = strat.on_event(&quote("0.30", "0.32")).unwrap();
        assert_eq!(decisions.len(), 1);
        assert!(matches!(
            decisions[0],
            DecisionPayload::PlaceOrder { .. }
        ));
    }

    #[test]
    fn threshold_strategy_emits_nothing_when_mid_above_threshold() {
        let mut strat = ThresholdStrategy::new(
            "weather-v1",
            "weather-kalshi-paper-a",
            0.5,
            1.0,
            "10",
        );
        let decisions = strat.on_event(&quote("0.55", "0.57")).unwrap();
        assert_eq!(decisions.len(), 0);
    }

    #[test]
    fn end_to_end_run_emits_intent_routes_to_gateway_and_records_summary() {
        let mut bus = InMemoryBus::new();
        bus.publish(quote("0.30", "0.32"));
        bus.publish(quote("0.55", "0.57"));
        bus.publish(quote("0.20", "0.22"));

        let mut strat = ThresholdStrategy::new(
            "weather-v1",
            "weather-kalshi-paper-a",
            0.5,
            1.0,
            "10",
        );
        let risk = RiskGate::new(limits());
        let mut state = SleeveState::default();
        let venue = RecordingVenueClient::new();
        let mut gw = DryRunGateway::new(RiskGate::new(limits()), venue);

        let mut sink = InMemoryBus::new();
        let mut t = 0i64;
        let now = Box::new(move || {
            t += 1;
            format!("2026-05-26T12:00:{:02}Z", t)
        });

        let mut bus_src = std::mem::take(&mut bus);
        let summary = SleeveRunner::new(
            &mut strat,
            &mut bus_src,
            &risk,
            &mut state,
            &mut sink,
            Some(&mut gw),
            now,
        )
        .run()
        .unwrap();

        assert_eq!(summary.events_processed, 3);
        assert_eq!(summary.decisions_emitted, 2);
        assert_eq!(summary.intents_approved, 2);
        assert_eq!(summary.intents_rejected_by_risk, 0);
        assert_eq!(summary.gateway_acks, 2);
        assert_eq!(sink.emitted.len(), 2);
        assert_eq!(gw.venue.submitted.len(), 2);
    }

    #[test]
    fn end_to_end_run_records_risk_rejection_when_size_too_large() {
        let mut bus = InMemoryBus::new();
        bus.publish(quote("0.30", "0.32"));

        let mut strat = ThresholdStrategy::new(
            "weather-v1",
            "weather-kalshi-paper-a",
            0.5,
            1.0,
            "10000", // 10000 * 0.32 = 3200, > max_order_notional 500
        );
        let risk = RiskGate::new(limits());
        let mut state = SleeveState::default();
        let venue = RecordingVenueClient::new();
        let mut gw = DryRunGateway::new(RiskGate::new(limits()), venue);
        let mut sink = InMemoryBus::new();
        let mut t = 0i64;
        let now = Box::new(move || {
            t += 1;
            format!("2026-05-26T12:00:{:02}Z", t)
        });

        let summary = SleeveRunner::new(
            &mut strat,
            &mut bus,
            &risk,
            &mut state,
            &mut sink,
            Some(&mut gw),
            now,
        )
        .run()
        .unwrap();

        assert_eq!(summary.events_processed, 1);
        assert_eq!(summary.decisions_emitted, 1);
        assert_eq!(summary.intents_approved, 0);
        assert_eq!(summary.intents_rejected_by_risk, 1);
        assert_eq!(summary.gateway_acks, 0);
        assert_eq!(gw.venue.submitted.len(), 0);
    }

    #[test]
    fn end_to_end_run_with_no_gateway_still_emits_to_sink() {
        let mut bus = InMemoryBus::new();
        bus.publish(quote("0.30", "0.32"));
        let mut strat = ThresholdStrategy::new(
            "weather-v1",
            "weather-kalshi-paper-a",
            0.5,
            1.0,
            "10",
        );
        let risk = RiskGate::new(limits());
        let mut state = SleeveState::default();
        let mut sink = InMemoryBus::new();
        let mut t = 0i64;
        let now = Box::new(move || {
            t += 1;
            format!("2026-05-26T12:00:{:02}Z", t)
        });
        let summary: RunSummary = SleeveRunner::<_, _, RecordingVenueClient>::new(
            &mut strat,
            &mut bus,
            &risk,
            &mut state,
            &mut sink,
            None,
            now,
        )
        .run()
        .unwrap();
        assert_eq!(summary.intents_approved, 1);
        assert_eq!(summary.gateway_acks, 0);
        assert_eq!(sink.emitted.len(), 1);
    }
}
