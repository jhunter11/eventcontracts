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

pub mod pricing;
pub mod registry;
pub mod spec;

pub use registry::{default_registry, FromSpec, StrategyRegistry};
pub use spec::{SpecError, StrategySpecArtifact};

use eventcontracts_contracts::{
    canonical_sha256, AuditStamp, Contract, IntentEnvelopeRecord, Metadata, NormalizedEventRecord,
    PredictionRecord,
};
use eventcontracts_feature_builder::{
    quote_features_raw, quote_features_rolling_raw, RollingQuoteState, Scorer, QUOTE_FEATURE_WIDTH,
    ROLLING_QUOTE_FEATURE_WIDTH,
};
use eventcontracts_gateway::{
    DecisionPayload, DryRunGateway, GatewayError, OutcomeSide, VenueClient,
};
use eventcontracts_oms::{Side, TimeInForce};
use eventcontracts_risk::{
    epoch_seconds_from_rfc3339, outcome_position_key, record_quote_bbo, IntentSnapshot,
    RiskDecision, RiskGate, RiskRejection, SleeveState,
};
use eventcontracts_runtime_hot::{project_event, HotEvent, ProjectError};
use serde::{Deserialize, Serialize};
use std::collections::{HashMap, VecDeque};
use thiserror::Error;

// Single source of truth shared with the `pricing` discretisation helpers, so
// the runner's `FixedPrice` scale and the tick helpers can never drift apart
// (a drift would make `pricing::buy_limit_from_fair` clamp every price to ~$0.01).
const PRICE_SCALE: i64 = pricing::PRICE_ONE;

/// Conversion factor: `runtime-hot` uses 4-decimal fixed-point (1 tick =
/// $0.0001), runner's local `FixedPrice` uses 6-decimal fixed-point
/// (1 tick = $0.000001). The boundary is exact: every runtime-hot tick maps
/// to exactly 100 runner ticks.
const HOT_TO_RUNNER_SCALE: i64 = 100;

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

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct FixedPrice(i64);

impl FixedPrice {
    pub fn parse(value: &serde_json::Value, key: &str) -> Result<Self, RunnerError> {
        match value.get(key) {
            Some(serde_json::Value::String(raw)) => parse_decimal_ticks(raw).map(FixedPrice),
            Some(serde_json::Value::Number(raw)) => {
                parse_decimal_ticks(&raw.to_string()).map(FixedPrice)
            }
            _ => Err(RunnerError::Strategy(format!("missing field `{key}`"))),
        }
    }

    pub fn from_f64(value: f64) -> Self {
        Self((value * PRICE_SCALE as f64).round() as i64)
    }

    /// Convert from the bus-boundary `runtime-hot` representation (4-decimal
    /// fixed-point) into the runner's 6-decimal internal representation.
    /// Exact, integer-only, no rounding.
    pub fn from_hot(price: eventcontracts_runtime_hot::FixedPrice) -> Self {
        Self(price.raw().saturating_mul(HOT_TO_RUNNER_SCALE))
    }

    pub fn ticks(self) -> i64 {
        self.0
    }

    pub fn format(self) -> String {
        format_decimal_ticks(self.0)
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum StrategyEvent {
    Quote {
        instrument: String,
        market_id: String,
        bid: FixedPrice,
        ask: FixedPrice,
    },
    Trade {
        instrument: String,
        market_id: String,
        price: FixedPrice,
        size: u32,
    },
    Book {
        instrument: String,
        market_id: String,
        bids: Vec<(FixedPrice, u32)>,
        asks: Vec<(FixedPrice, u32)>,
        is_snapshot: bool,
    },
    OwnFill {
        client_order_id: String,
        instrument: String,
        price: FixedPrice,
        quantity: u32,
        remaining_quantity: u32,
    },
    OwnOrderUpdate {
        client_order_id: String,
        instrument: String,
        state: String,
        reason: Option<String>,
    },
    TennisPrediction {
        source: String,
        market_id: String,
        probability: FixedPrice,
        confidence: Option<FixedPrice>,
        odds_present: Option<bool>,
    },
    ExternalProbability {
        source: String,
        market_id: String,
        probability: FixedPrice,
        /// Optional model confidence in [0,1] (as a price-scaled fixed point).
        /// Strategies with a configured `min_confidence` suppress signals below
        /// it; absent confidence is treated as zero.
        confidence: Option<FixedPrice>,
        /// Producer forecast lead in days (0 = settles today). Strategies whose
        /// model is only valid at lead 0 (e.g. weather KXHIGH, whose calibration
        /// sigma is nowcast-lead) refuse `lead_days != 0`. Absent = unconstrained.
        lead_days: Option<i64>,
        /// Market close time (RFC3339), used for a "no new trades within N seconds
        /// of close" gate recomputed against `ctx.now`. Absent = no close gate.
        close_time: Option<String>,
    },
    /// Generic external signal carrying the raw payload, for bespoke custom
    /// strategies that compute their own model probability in-runtime (e.g.
    /// box-office seat-occupancy/ticket-velocity -> implied gross). Emitted for
    /// `external` events that carry a `market_id` but no recognized probability
    /// key, so the existing probability/tennis paths are unaffected.
    ExternalSignal {
        source: String,
        market_id: String,
        payload_json: String,
    },
    Other {
        event_kind: String,
    },
}

impl StrategyEvent {
    /// Build a `StrategyEvent` from a normalized record.
    ///
    /// Hot market-data kinds (`quote`, eventually `trade`/`book`) are
    /// projected through [`runtime_hot::project_event`] — the same parse the
    /// bus subscriber would run — so adding a new venue means writing a
    /// normalizer that emits the agreed `payload_json` shape, not touching
    /// strategy code.
    ///
    /// Non-hot kinds (`external` predictions, lifecycle, etc.) keep their
    /// existing slow-path parsers — they don't run per-tick and don't need
    /// the alloc-free representation.
    pub fn from_record(event: &NormalizedEventRecord) -> Result<Self, RunnerError> {
        match event.event_kind.as_str() {
            "quote" => from_hot_quote(event),
            "trade" => from_hot_trade(event),
            "book" => from_hot_book(event),
            "own_fill" => from_hot_own_fill(event),
            "own_order_update" | "own_order_reject" => from_hot_own_order_update(event),
            "external" => Ok(parse_external(event).unwrap_or(Self::Other {
                event_kind: event.event_kind.clone(),
            })),
            other => Ok(Self::Other {
                event_kind: other.to_string(),
            }),
        }
    }
}

fn from_hot_quote(event: &NormalizedEventRecord) -> Result<StrategyEvent, RunnerError> {
    match project_event(event) {
        Ok(HotEvent::Quote(q)) => Ok(StrategyEvent::Quote {
            market_id: market_id_from_instrument(q.instrument.as_str()),
            instrument: q.instrument.as_str().to_string(),
            bid: FixedPrice::from_hot(q.bid),
            ask: FixedPrice::from_hot(q.ask),
        }),
        Ok(_) => Err(RunnerError::Strategy(
            "runtime-hot projection returned non-quote variant for event_kind=quote".into(),
        )),
        Err(ProjectError::Json(e)) => Err(RunnerError::Strategy(e.to_string())),
        Err(e) => Err(RunnerError::Strategy(e.to_string())),
    }
}

fn from_hot_trade(event: &NormalizedEventRecord) -> Result<StrategyEvent, RunnerError> {
    match project_event(event) {
        Ok(HotEvent::Trade(t)) => Ok(StrategyEvent::Trade {
            market_id: market_id_from_instrument(t.instrument.as_str()),
            instrument: t.instrument.as_str().to_string(),
            price: FixedPrice::from_hot(t.price),
            size: t.size.raw(),
        }),
        Ok(_) => Err(RunnerError::Strategy(
            "runtime-hot projection returned non-trade variant for event_kind=trade".into(),
        )),
        Err(e) => Err(RunnerError::Strategy(e.to_string())),
    }
}

fn from_hot_book(event: &NormalizedEventRecord) -> Result<StrategyEvent, RunnerError> {
    match project_event(event) {
        Ok(HotEvent::Book(b)) => Ok(StrategyEvent::Book {
            market_id: market_id_from_instrument(b.instrument.as_str()),
            instrument: b.instrument.as_str().to_string(),
            bids: b
                .bids
                .iter()
                .map(|level| (FixedPrice::from_hot(level.price), level.size.raw()))
                .collect(),
            asks: b
                .asks
                .iter()
                .map(|level| (FixedPrice::from_hot(level.price), level.size.raw()))
                .collect(),
            is_snapshot: b.is_snapshot,
        }),
        Ok(_) => Err(RunnerError::Strategy(
            "runtime-hot projection returned non-book variant for event_kind=book".into(),
        )),
        Err(e) => Err(RunnerError::Strategy(e.to_string())),
    }
}

fn from_hot_own_fill(event: &NormalizedEventRecord) -> Result<StrategyEvent, RunnerError> {
    match project_event(event) {
        Ok(HotEvent::OwnFill(f)) => Ok(StrategyEvent::OwnFill {
            client_order_id: f.client_order_id.to_string(),
            instrument: f.instrument.to_string(),
            price: FixedPrice::from_hot(f.price),
            quantity: f.quantity.raw(),
            remaining_quantity: f.remaining_quantity.raw(),
        }),
        Ok(_) => Err(RunnerError::Strategy(
            "runtime-hot projection returned non-own-fill variant for event_kind=own_fill".into(),
        )),
        Err(e) => Err(RunnerError::Strategy(e.to_string())),
    }
}

fn from_hot_own_order_update(event: &NormalizedEventRecord) -> Result<StrategyEvent, RunnerError> {
    match project_event(event) {
        Ok(HotEvent::OwnOrderUpdate(u)) | Ok(HotEvent::OwnOrderReject(u)) => {
            Ok(StrategyEvent::OwnOrderUpdate {
                client_order_id: u.client_order_id.to_string(),
                instrument: u.instrument.to_string(),
                state: u.state.to_string(),
                reason: u.reason.map(|r| r.to_string()),
            })
        }
        Ok(_) => Err(RunnerError::Strategy(
            "runtime-hot projection returned non-own-order variant for private order event".into(),
        )),
        Err(e) => Err(RunnerError::Strategy(e.to_string())),
    }
}

fn parse_external(event: &NormalizedEventRecord) -> Option<StrategyEvent> {
    let payload: serde_json::Value = serde_json::from_str(&event.payload_json).ok()?;
    let market_id = payload.get("market_id").and_then(|v| v.as_str())?;
    let source = payload
        .get("source")
        .and_then(|v| v.as_str())
        .unwrap_or(event.provenance.source.as_str())
        .to_string();
    if let Ok(probability) = FixedPrice::parse(&payload, "player_1_win_probability") {
        return Some(StrategyEvent::TennisPrediction {
            source,
            market_id: market_id.to_string(),
            probability,
            confidence: FixedPrice::parse(&payload, "model_confidence")
                .ok()
                .or_else(|| FixedPrice::parse(&payload, "confidence").ok()),
            odds_present: parse_optional_bool(payload.get("odds_present")),
        });
    }
    for key in [
        "probability",
        "implied_prob",
        "surge_probability",
        "yield_reversion_probability",
    ] {
        if let Ok(probability) = FixedPrice::parse(&payload, key) {
            return Some(StrategyEvent::ExternalProbability {
                source,
                market_id: market_id.to_string(),
                probability,
                confidence: FixedPrice::parse(&payload, "confidence").ok(),
                lead_days: payload.get("lead_days").and_then(parse_optional_i64),
                close_time: payload
                    .get("close_time")
                    .and_then(|v| v.as_str())
                    .map(str::to_string),
            });
        }
    }
    // No recognized probability key: surface the raw payload so a bespoke
    // custom strategy (e.g. box-office) can parse its own model inputs and
    // compute the probability in-runtime. Requires `market_id` (checked above).
    Some(StrategyEvent::ExternalSignal {
        source,
        market_id: market_id.to_string(),
        payload_json: event.payload_json.clone(),
    })
}

fn parse_optional_i64(value: &serde_json::Value) -> Option<i64> {
    match value {
        serde_json::Value::Number(n) => n.as_i64(),
        serde_json::Value::String(s) => s.trim().parse::<i64>().ok(),
        _ => None,
    }
}

fn parse_optional_bool(value: Option<&serde_json::Value>) -> Option<bool> {
    match value? {
        serde_json::Value::Bool(value) => Some(*value),
        serde_json::Value::Number(value) => value.as_u64().and_then(|n| match n {
            0 => Some(false),
            1 => Some(true),
            _ => None,
        }),
        serde_json::Value::String(value) => match value.trim().to_ascii_lowercase().as_str() {
            "true" | "1" | "yes" | "y" | "on" => Some(true),
            "false" | "0" | "no" | "n" | "off" => Some(false),
            _ => None,
        },
        _ => None,
    }
}

// ---------- traits ----------

pub trait EventSource {
    fn next_event(&mut self) -> Result<Option<NormalizedEventRecord>, RunnerError>;
}

#[derive(Clone, Debug)]
pub struct StrategyContext {
    pub now: String,
    pub sleeve_state: SleeveState,
    pub features: HashMap<String, String>,
    /// `event_id` of the most recent normalized event being processed. Used
    /// by strategies that emit audit records (e.g., predictions) to link the
    /// audit chain back to the input event. Empty when no event applies
    /// (e.g., synthetic startup context).
    pub source_event_id: String,
}

impl StrategyContext {
    pub fn from_sleeve_state(now: impl Into<String>, sleeve_state: &SleeveState) -> Self {
        Self {
            now: now.into(),
            sleeve_state: sleeve_state.clone(),
            features: HashMap::new(),
            source_event_id: String::new(),
        }
    }

    pub fn with_source_event(mut self, event_id: impl Into<String>) -> Self {
        self.source_event_id = event_id.into();
        self
    }

    pub fn open_orders(&self) -> u64 {
        self.sleeve_state.open_orders
    }

    pub fn position_quantity(&self, instrument_id: &str) -> Option<f64> {
        self.position_quantity_for(instrument_id, OutcomeSide::Yes)
            .or_else(|| {
                self.sleeve_state
                    .positions
                    .get(instrument_id)
                    .map(|position| position.quantity as f64)
            })
    }

    pub fn position_quantity_for(
        &self,
        instrument_id: &str,
        outcome_side: OutcomeSide,
    ) -> Option<f64> {
        let key = outcome_position_key(instrument_id, outcome_side);
        self.sleeve_state
            .positions
            .get(&key)
            .map(|position| position.quantity as f64)
    }

    pub fn feature(&self, name: &str) -> Option<&str> {
        self.features.get(name).map(String::as_str)
    }
}

pub trait StrategyRuntime {
    fn strategy_id(&self) -> &str;
    fn sleeve_id(&self) -> &str;
    fn on_event(
        &mut self,
        event: &StrategyEvent,
        _ctx: &StrategyContext,
    ) -> Result<Vec<DecisionPayload>, RunnerError>;

    /// Default execution priority tier this strategy's intents ride on.
    /// Must be one of `"fast"`, `"standard"`, `"relaxed"` (gateway rejects
    /// other values at enqueue). Default `"standard"`.
    fn priority_tier(&self) -> &str {
        "standard"
    }

    /// Intent TTL in milliseconds, sourced from the spec's
    /// `default_execution_priority.expires_after_ms`. `None` means "use the
    /// gateway's tier default" (30s for standard, 5s for fast).
    fn expires_after_ms(&self) -> Option<u64> {
        None
    }

    /// Feedback from the gateway when an emitted intent never reached a live
    /// venue state (risk reject, stale drop, last-look reject, transport
    /// unknown/reject). Strategies must clear emit-time pending assumptions
    /// here; venue fills/order updates still arrive through `on_event`.
    fn on_intent_rejected(&mut self, _client_order_id: &str, _reason: &str) {}
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

// `InMemoryEventBus` + `InMemoryIntentBus` (split duplicates of `InMemoryBus`)
// removed — `InMemoryBus` above already serves both `EventSource` and
// `IntentSink`. The split versions were never consumed.

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
    buy_below: FixedPrice,
    sell_above: FixedPrice,
    pub size: String,
    next_client_order: u64,
    priority_tier: String,
    expires_after_ms: Option<u64>,
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
            buy_below: FixedPrice::from_f64(buy_below),
            sell_above: FixedPrice::from_f64(sell_above),
            size: size.into(),
            next_client_order: 0,
            priority_tier: "standard".to_string(),
            expires_after_ms: None,
        }
    }

    pub fn with_priority(mut self, tier: impl Into<String>, expires_after_ms: Option<u64>) -> Self {
        self.priority_tier = tier.into();
        self.expires_after_ms = expires_after_ms;
        self
    }
}

impl FromSpec for ThresholdStrategy {
    fn from_spec(spec: &StrategySpecArtifact) -> Result<Self, SpecError> {
        // `buy_below = 0` and `sell_above = 1` both effectively disable a
        // side; that's how a researcher silences a leg without removing it.
        let buy_below = spec.param_f64_or("buy_below", 0.0)?;
        let sell_above = spec.param_f64_or("sell_above", 1.0)?;
        let size = spec.param_str_or("size", "1");
        let sleeve_id = format!("{}-sleeve", spec.strategy_id);
        let (tier, expires_after_ms) = priority_from_spec(spec);
        Ok(Self::new(
            spec.strategy_id.clone(),
            sleeve_id,
            buy_below,
            sell_above,
            size,
        )
        .with_priority(tier, expires_after_ms))
    }
}

/// Extract `(priority_tier, expires_after_ms)` from a strategy spec.
/// Validates the tier against the gateway's accepted set; unknown values
/// fall back to `"standard"`.
pub fn priority_from_spec(spec: &StrategySpecArtifact) -> (String, Option<u64>) {
    let exec = spec.default_execution_priority.as_ref();
    let tier = exec
        .map(|e| e.tier.as_str())
        .filter(|t| matches!(*t, "fast" | "standard" | "relaxed"))
        .unwrap_or("standard")
        .to_string();
    let expires_after_ms = exec.and_then(|e| e.expires_after_ms);
    (tier, expires_after_ms)
}

impl StrategyRuntime for ThresholdStrategy {
    fn strategy_id(&self) -> &str {
        &self.strategy_id
    }
    fn sleeve_id(&self) -> &str {
        &self.sleeve_id
    }
    fn priority_tier(&self) -> &str {
        &self.priority_tier
    }
    fn expires_after_ms(&self) -> Option<u64> {
        self.expires_after_ms
    }
    fn on_event(
        &mut self,
        event: &StrategyEvent,
        _ctx: &StrategyContext,
    ) -> Result<Vec<DecisionPayload>, RunnerError> {
        let StrategyEvent::Quote {
            instrument,
            bid,
            ask,
            ..
        } = event
        else {
            return Ok(vec![]);
        };
        if !(bid.ticks() > 0 && ask.ticks() > 0) {
            return Ok(vec![]);
        }
        let mid = (bid.ticks() + ask.ticks()) / 2;

        if self.buy_below.ticks() > 0 && mid < self.buy_below.ticks() {
            self.next_client_order += 1;
            return Ok(vec![DecisionPayload::PlaceOrder {
                client_order_id: format!("c-buy-{:08}", self.next_client_order),
                instrument_id: instrument.clone(),
                outcome_side: OutcomeSide::Yes,
                side: Side::Buy,
                price: ask.format(),
                quantity: self.size.clone(),
                fair_price: None,
                min_executable_edge_ticks: None,
                fee_rate_bps: None,
                time_in_force: TimeInForce::Ioc,
            }]);
        }
        if self.sell_above.ticks() < PRICE_SCALE && mid > self.sell_above.ticks() {
            self.next_client_order += 1;
            return Ok(vec![DecisionPayload::PlaceOrder {
                client_order_id: format!("c-sell-{:08}", self.next_client_order),
                instrument_id: instrument.clone(),
                outcome_side: OutcomeSide::Yes,
                side: Side::Sell,
                price: bid.format(),
                quantity: self.size.clone(),
                fair_price: None,
                min_executable_edge_ticks: None,
                fee_rate_bps: None,
                time_in_force: TimeInForce::Ioc,
            }]);
        }
        Ok(vec![])
    }
}

// ---------- spec-driven external edge archetype ----------

#[derive(Clone, Debug, Default)]
struct ExternalEdgeMarketState {
    instrument: Option<String>,
    mid: Option<FixedPrice>,
}

/// Config-only Rust runtime for slow external probability signals.
///
/// This covers paper/research strategies whose Python implementation stores a
/// quote mid, receives a calibrated external probability, and buys YES/NO when
/// the absolute edge clears `min_edge_bps`.
pub struct ExternalEdgeStrategy {
    pub strategy_id: String,
    pub sleeve_id: String,
    signal_source: String,
    min_edge_ticks: i64,
    size: String,
    next_client_order: u64,
    markets: HashMap<String, ExternalEdgeMarketState>,
    priority_tier: String,
    expires_after_ms: Option<u64>,
    /// Minimum model confidence (price-scaled ticks) required to act. 0 == no
    /// gate. Mirrors the Python strategies' `min_confidence` so a low-confidence
    /// signal is suppressed identically in live (Rust) and research (Python).
    min_confidence_ticks: i64,
}

impl FromSpec for ExternalEdgeStrategy {
    fn from_spec(spec: &StrategySpecArtifact) -> Result<Self, SpecError> {
        let signal_source = spec.param_str_or("signal_source", "");
        let min_edge_bps = spec.param_f64_or("min_edge_bps", 100.0)?;
        let min_confidence = spec.param_f64_or("min_confidence", 0.0)?;
        let size = spec.param_str_or("size", "1");
        let (tier, expires_after_ms) = priority_from_spec(spec);
        Ok(Self {
            strategy_id: spec.strategy_id.clone(),
            sleeve_id: format!("{}-sleeve", spec.strategy_id),
            signal_source,
            min_edge_ticks: ((min_edge_bps / 10_000.0) * PRICE_SCALE as f64).round() as i64,
            size,
            next_client_order: 0,
            markets: HashMap::new(),
            priority_tier: tier,
            expires_after_ms,
            min_confidence_ticks: (min_confidence * PRICE_SCALE as f64).round() as i64,
        })
    }
}

impl StrategyRuntime for ExternalEdgeStrategy {
    fn strategy_id(&self) -> &str {
        &self.strategy_id
    }

    fn sleeve_id(&self) -> &str {
        &self.sleeve_id
    }

    fn priority_tier(&self) -> &str {
        &self.priority_tier
    }

    fn expires_after_ms(&self) -> Option<u64> {
        self.expires_after_ms
    }

    fn on_event(
        &mut self,
        event: &StrategyEvent,
        _ctx: &StrategyContext,
    ) -> Result<Vec<DecisionPayload>, RunnerError> {
        match event {
            StrategyEvent::Quote {
                market_id,
                instrument,
                bid,
                ask,
            } => {
                if bid.ticks() > 0 && ask.ticks() > 0 {
                    let state = self.markets.entry(market_id.clone()).or_default();
                    state.instrument = Some(instrument.clone());
                    state.mid = Some(FixedPrice((bid.ticks() + ask.ticks()) / 2));
                }
                Ok(vec![])
            }
            StrategyEvent::ExternalProbability {
                source,
                market_id,
                probability,
                confidence,
                ..
            } => {
                if !self.signal_source.is_empty() && source != &self.signal_source {
                    return Ok(vec![]);
                }
                // Confidence gate (Python↔Rust parity): suppress signals below the
                // configured `min_confidence`. A missing confidence is treated as
                // zero, matching the Python strategies — so a low- or no-confidence
                // signal cannot trade live when the author intended to block it.
                if self.min_confidence_ticks > 0 {
                    let confidence_ticks = confidence.as_ref().map(|c| c.ticks()).unwrap_or(0);
                    if confidence_ticks < self.min_confidence_ticks {
                        return Ok(vec![]);
                    }
                }
                let Some(state) = self.markets.get(market_id) else {
                    return Ok(vec![]);
                };
                let Some(mid) = state.mid else {
                    return Ok(vec![]);
                };
                let edge = probability.ticks() - mid.ticks();
                if edge.abs() < self.min_edge_ticks {
                    return Ok(vec![]);
                }
                self.next_client_order += 1;
                let outcome_side = if edge > 0 {
                    OutcomeSide::Yes
                } else {
                    OutcomeSide::No
                };
                // Edge-preserving discretisation (V6-C3): the order is always a
                // BUY (of YES or of NO), so floor the per-side price to the venue
                // tick. The raw mid can be a half-cent (odd tick-sum), which would
                // otherwise emit a sub-cent price Kalshi rejects; flooring both
                // preserves edge and keeps Python/Rust bit-identical.
                let raw_price_ticks = if outcome_side == OutcomeSide::Yes {
                    mid.ticks()
                } else {
                    PRICE_SCALE - mid.ticks()
                };
                let price_ticks = pricing::buy_limit_from_fair(raw_price_ticks, pricing::CENT_TICK);
                let fair_ticks = if outcome_side == OutcomeSide::Yes {
                    probability.ticks()
                } else {
                    PRICE_SCALE - probability.ticks()
                };
                Ok(vec![DecisionPayload::PlaceOrder {
                    client_order_id: format!("c-edge-{:08}", self.next_client_order),
                    instrument_id: state
                        .instrument
                        .clone()
                        .unwrap_or_else(|| format!("kalshi:{market_id}")),
                    outcome_side,
                    side: Side::Buy,
                    price: format_decimal_ticks(price_ticks),
                    quantity: self.size.clone(),
                    fair_price: Some(format_decimal_ticks(fair_ticks)),
                    min_executable_edge_ticks: Some(self.min_edge_ticks / 100),
                    fee_rate_bps: None,
                    time_in_force: TimeInForce::Gtc,
                }])
            }
            _ => Ok(vec![]),
        }
    }
}

// ---------- weather temperature arbitrage taker ----------

#[derive(Clone, Debug)]
struct WeatherSignalState {
    probability: FixedPrice,
    received_epoch_secs: i64,
    /// Market close as epoch seconds (parsed from the signal's RFC3339 close_time),
    /// for the near-close gate recomputed against `now`. None = no close gate.
    close_epoch_secs: Option<i64>,
}

#[derive(Clone, Debug, Default)]
struct WeatherMarketState {
    instrument: Option<String>,
    yes_bid: Option<FixedPrice>,
    yes_ask: Option<FixedPrice>,
    latest_signal: Option<WeatherSignalState>,
    last_order_price_ticks: Option<i64>,
    last_order_probability_ticks: Option<i64>,
}

/// Rust live implementation of `weather_temperature_arbitrage`.
///
/// It consumes quote/book ticks plus external probability events. Orders are
/// IOC taker buys on the side with the larger executable edge. This is the
/// promoted live path for the Python weather research loop: Python can keep
/// producing calibrated Open-Meteo probabilities, while Rust owns submission,
/// last-look, reconciliation, and kill-switch behavior.
pub struct WeatherTemperatureArbitrageStrategy {
    pub strategy_id: String,
    pub sleeve_id: String,
    signal_source: String,
    min_edge_ticks: i64,
    max_spread_ticks: i64,
    spread_edge_multiplier: f64,
    near_binary_price_ticks: i64,
    near_binary_edge_ticks: i64,
    max_signal_age_secs: i64,
    min_seconds_to_close_secs: i64,
    min_retrade_price_delta_ticks: i64,
    min_retrade_probability_delta_ticks: i64,
    quote_triggered_trading: bool,
    size: String,
    next_client_order: u64,
    markets: HashMap<String, WeatherMarketState>,
    priority_tier: String,
    expires_after_ms: Option<u64>,
}

impl WeatherTemperatureArbitrageStrategy {
    fn market_state_mut(
        &mut self,
        market_id: &str,
        instrument: Option<&str>,
    ) -> &mut WeatherMarketState {
        let state = self.markets.entry(market_id.to_string()).or_default();
        if let Some(instrument) = instrument {
            state.instrument = Some(instrument.to_string());
        }
        state
    }

    fn update_book_bbo(
        &mut self,
        market_id: &str,
        instrument: &str,
        bids: &[(FixedPrice, u32)],
        asks: &[(FixedPrice, u32)],
    ) {
        let (Some((yes_bid, _)), Some((no_bid, _))) = (bids.first(), asks.first()) else {
            return;
        };
        let yes_ask_ticks = PRICE_SCALE.saturating_sub(no_bid.ticks());
        if yes_bid.ticks() <= 0 || yes_ask_ticks <= 0 {
            return;
        }
        let state = self.market_state_mut(market_id, Some(instrument));
        state.yes_bid = Some(*yes_bid);
        state.yes_ask = Some(FixedPrice(yes_ask_ticks));
    }

    fn evaluate_market(
        &mut self,
        market_id: &str,
        now_epoch_secs: i64,
    ) -> Result<Vec<DecisionPayload>, RunnerError> {
        let Some(state) = self.markets.get(market_id) else {
            return Ok(vec![]);
        };
        let Some(signal) = state.latest_signal.clone() else {
            return Ok(vec![]);
        };
        if self.max_signal_age_secs > 0 {
            let age = now_epoch_secs.saturating_sub(signal.received_epoch_secs);
            if age < 0 || age > self.max_signal_age_secs {
                return Ok(vec![]);
            }
        }
        // Near-close gate (Python parity): don't open new positions within
        // `min_seconds_to_close` of market close. Recomputed against `now` so it
        // fires identically on signal arrival and on quote/book re-fires.
        if self.min_seconds_to_close_secs > 0 {
            if let Some(close_epoch) = signal.close_epoch_secs {
                if close_epoch.saturating_sub(now_epoch_secs) < self.min_seconds_to_close_secs {
                    return Ok(vec![]);
                }
            }
        }
        let (Some(instrument), Some(yes_bid), Some(yes_ask)) =
            (state.instrument.clone(), state.yes_bid, state.yes_ask)
        else {
            return Ok(vec![]);
        };
        if !(yes_bid.ticks() > 0 && yes_ask.ticks() > 0 && yes_ask.ticks() < PRICE_SCALE) {
            return Ok(vec![]);
        }
        let spread_ticks = yes_ask.ticks().saturating_sub(yes_bid.ticks());
        if yes_ask.ticks() <= yes_bid.ticks() || spread_ticks > self.max_spread_ticks {
            return Ok(vec![]);
        }

        let dynamic_edge_ticks = (spread_ticks as f64 * self.spread_edge_multiplier).round() as i64;
        let required_edge_ticks = self.min_edge_ticks.saturating_add(dynamic_edge_ticks);
        let mut yes_edge_ticks = signal.probability.ticks() - yes_ask.ticks();
        let no_ask_ticks = PRICE_SCALE.saturating_sub(yes_bid.ticks());
        let no_probability_ticks = PRICE_SCALE.saturating_sub(signal.probability.ticks());
        let mut no_edge_ticks = no_probability_ticks - no_ask_ticks;
        if yes_ask.ticks() >= self.near_binary_price_ticks {
            yes_edge_ticks = yes_edge_ticks.saturating_sub(self.near_binary_edge_ticks);
        }
        if no_ask_ticks >= self.near_binary_price_ticks {
            no_edge_ticks = no_edge_ticks.saturating_sub(self.near_binary_edge_ticks);
        }
        if yes_edge_ticks < required_edge_ticks && no_edge_ticks < required_edge_ticks {
            return Ok(vec![]);
        }

        let (outcome_side, price_ticks, fair_ticks) = if yes_edge_ticks >= no_edge_ticks {
            (
                OutcomeSide::Yes,
                yes_ask.ticks(),
                signal.probability.ticks(),
            )
        } else {
            (OutcomeSide::No, no_ask_ticks, no_probability_ticks)
        };
        if !self.can_retrade(market_id, price_ticks, signal.probability.ticks()) {
            return Ok(vec![]);
        }

        self.next_client_order += 1;
        let client_order_id = format!("c-weather-{:08}", self.next_client_order);
        let state = self.market_state_mut(market_id, Some(&instrument));
        state.last_order_price_ticks = Some(price_ticks);
        state.last_order_probability_ticks = Some(signal.probability.ticks());

        Ok(vec![DecisionPayload::PlaceOrder {
            client_order_id,
            instrument_id: instrument,
            outcome_side,
            side: Side::Buy,
            price: format_decimal_ticks(price_ticks),
            quantity: self.size.clone(),
            fair_price: Some(format_decimal_ticks(fair_ticks)),
            min_executable_edge_ticks: Some(self.min_edge_ticks / 100),
            fee_rate_bps: Some(700),
            time_in_force: TimeInForce::Ioc,
        }])
    }

    fn can_retrade(&self, market_id: &str, price_ticks: i64, probability_ticks: i64) -> bool {
        let Some(state) = self.markets.get(market_id) else {
            return true;
        };
        let price_moved = state
            .last_order_price_ticks
            .map(|last| (price_ticks - last).abs() >= self.min_retrade_price_delta_ticks)
            .unwrap_or(true);
        let probability_moved = state
            .last_order_probability_ticks
            .map(|last| {
                (probability_ticks - last).abs() >= self.min_retrade_probability_delta_ticks
            })
            .unwrap_or(true);
        price_moved || probability_moved
    }
}

impl FromSpec for WeatherTemperatureArbitrageStrategy {
    fn from_spec(spec: &StrategySpecArtifact) -> Result<Self, SpecError> {
        let signal_source = spec.param_str_or("signal_source", "open-meteo");
        let min_edge_bps = spec.param_f64_or("min_edge_bps", 150.0)?;
        let max_spread = spec.param_f64_or("max_spread", 0.80)?;
        let spread_edge_multiplier = spec.param_f64_or("spread_edge_multiplier", 0.0)?;
        let near_binary_price = spec.param_f64_or("near_binary_price", 0.95)?;
        let near_binary_min_edge_bps = spec.param_f64_or("near_binary_min_edge_bps", 600.0)?;
        let max_signal_age_seconds = spec.param_f64_or("max_signal_age_seconds", 180.0)?;
        let min_seconds_to_close = spec.param_f64_or("min_seconds_to_close", 0.0)?;
        let min_retrade_price_delta = spec.param_f64_or("min_retrade_price_delta", 0.03)?;
        let min_retrade_probability_delta =
            spec.param_f64_or("min_retrade_probability_delta", 0.04)?;
        let quote_triggered_trading = spec.param_bool_or("quote_triggered_trading", true)?;
        let max_size = spec.param_str_or("max_size", "1");
        let size = spec.param_str_or("size", &max_size);
        let sleeve_id = spec
            .tags
            .get("sleeve_id")
            .cloned()
            .unwrap_or_else(|| format!("{}-sleeve", spec.strategy_id));
        let (tier, expires_after_ms) = priority_from_spec(spec);
        Ok(Self {
            strategy_id: spec.strategy_id.clone(),
            sleeve_id,
            signal_source,
            min_edge_ticks: ((min_edge_bps / 10_000.0) * PRICE_SCALE as f64).round() as i64,
            max_spread_ticks: (max_spread * PRICE_SCALE as f64).round() as i64,
            spread_edge_multiplier,
            near_binary_price_ticks: (near_binary_price * PRICE_SCALE as f64).round() as i64,
            near_binary_edge_ticks: ((near_binary_min_edge_bps / 10_000.0) * PRICE_SCALE as f64)
                .round() as i64,
            max_signal_age_secs: max_signal_age_seconds.round().max(0.0) as i64,
            min_seconds_to_close_secs: min_seconds_to_close.round().max(0.0) as i64,
            min_retrade_price_delta_ticks: (min_retrade_price_delta * PRICE_SCALE as f64).round()
                as i64,
            min_retrade_probability_delta_ticks: (min_retrade_probability_delta
                * PRICE_SCALE as f64)
                .round() as i64,
            quote_triggered_trading,
            size,
            next_client_order: 0,
            markets: HashMap::new(),
            priority_tier: tier,
            expires_after_ms,
        })
    }
}

impl StrategyRuntime for WeatherTemperatureArbitrageStrategy {
    fn strategy_id(&self) -> &str {
        &self.strategy_id
    }

    fn sleeve_id(&self) -> &str {
        &self.sleeve_id
    }

    fn priority_tier(&self) -> &str {
        &self.priority_tier
    }

    fn expires_after_ms(&self) -> Option<u64> {
        self.expires_after_ms
    }

    fn on_event(
        &mut self,
        event: &StrategyEvent,
        ctx: &StrategyContext,
    ) -> Result<Vec<DecisionPayload>, RunnerError> {
        let now_epoch = epoch_seconds_from_rfc3339(&ctx.now);
        match event {
            StrategyEvent::Quote {
                market_id,
                instrument,
                bid,
                ask,
            } => {
                if bid.ticks() > 0 && ask.ticks() > 0 {
                    let state = self.market_state_mut(market_id, Some(instrument));
                    state.yes_bid = Some(*bid);
                    state.yes_ask = Some(*ask);
                }
                if self.quote_triggered_trading {
                    return self.evaluate_market(market_id, now_epoch);
                }
                Ok(vec![])
            }
            StrategyEvent::Book {
                market_id,
                instrument,
                bids,
                asks,
                ..
            } => {
                self.update_book_bbo(market_id, instrument, bids, asks);
                if self.quote_triggered_trading {
                    return self.evaluate_market(market_id, now_epoch);
                }
                Ok(vec![])
            }
            StrategyEvent::ExternalProbability {
                source,
                market_id,
                probability,
                lead_days,
                close_time,
                ..
            } => {
                if !self.signal_source.is_empty() && source != &self.signal_source {
                    return Ok(vec![]);
                }
                if probability.ticks() < 0 || probability.ticks() > PRICE_SCALE {
                    return Ok(vec![]);
                }
                // Lead gate (Python parity): the station calibration sigma is
                // nowcast-lead, so only same-day (lead==0) signals are trustworthy.
                // Refuse — and do NOT cache — lead!=0 so a later quote can't
                // re-fire a stale future-day signal. Absent lead = unconstrained.
                if matches!(lead_days, Some(lead) if *lead != 0) {
                    return Ok(vec![]);
                }
                let close_epoch_secs = close_time.as_deref().map(epoch_seconds_from_rfc3339);
                let state = self.market_state_mut(market_id, None);
                state.latest_signal = Some(WeatherSignalState {
                    probability: *probability,
                    received_epoch_secs: now_epoch,
                    close_epoch_secs,
                });
                self.evaluate_market(market_id, now_epoch)
            }
            _ => Ok(vec![]),
        }
    }
}

// ---------- entertainment box-office extrapolator ----------

#[derive(Clone, Debug, Default)]
struct BoxOfficeMarketState {
    instrument: Option<String>,
    mid: Option<FixedPrice>,
}

/// Read a JSON value as f64, tolerating both numbers and decimal strings.
fn json_f64(value: &serde_json::Value) -> Option<f64> {
    match value {
        serde_json::Value::Number(n) => n.as_f64(),
        serde_json::Value::String(s) => s.trim().parse::<f64>().ok(),
        _ => None,
    }
}

/// Map an extrapolated weekend gross (whole dollars) to an implied YES
/// probability in price ticks, via the same clamped-linear rule as the Python
/// `EntertainmentBoxOfficeStrategy._gross_to_prob`:
///   ratio = extrapolated / target_gross
///   ratio <= 0.5 -> 0.05 ; ratio >= 1.5 -> 0.95 ; else 0.05 + (ratio-0.5)*0.9
/// Integer-exact on the parity grid; finer ticks round away at the 4dp boundary
/// shared with `format_decimal_ticks` (and with Python's 4dp quantize).
fn gross_to_prob_ticks(extrapolated: i64, target_gross: i64) -> i64 {
    if target_gross <= 0 {
        return 50_000; // ratio 0 -> 0.05 floor
    }
    let ratio_ticks = (extrapolated as i128 * PRICE_SCALE as i128 / target_gross as i128) as i64;
    if ratio_ticks <= 500_000 {
        50_000
    } else if ratio_ticks >= 1_500_000 {
        950_000
    } else {
        50_000 + (ratio_ticks - 500_000) * 9 / 10
    }
}

/// Rust live implementation of `entertainment_box_office`.
///
/// Consumes quote ticks (for the market mid) plus a bespoke external signal
/// carrying `seat_occupancy_pct`, `ticket_velocity_per_hour`, and `confidence`.
/// It reconstructs the weekend-gross extrapolation, maps it to an implied YES
/// probability with the same clamped-linear rule as the Python strategy, and
/// buys YES/NO when the absolute edge clears `min_edge_bps` (and confidence
/// clears `confidence_floor`). The "Friday 8pm" gating that Python expressed as
/// a TimerEvent is the producer's job here — Rust has no timer, so the decision
/// fires on signal arrival (parity-replicable trigger).
///
/// Integer fixed-point throughout (price ticks are `PRICE_ONE` == $1.00, dollar
/// amounts are whole-dollar i64) so the decision is bit-identical to Python on
/// the parity fixtures.
pub struct EntertainmentBoxOfficeStrategy {
    pub strategy_id: String,
    pub sleeve_id: String,
    signal_source: String,
    market_id: String,
    target_gross: i64,
    baseline_gross: i64,
    extrapolation_hours: i64,
    min_edge_ticks: i64,
    confidence_floor_ticks: i64,
    size: String,
    next_client_order: u64,
    markets: HashMap<String, BoxOfficeMarketState>,
    priority_tier: String,
    expires_after_ms: Option<u64>,
}

impl FromSpec for EntertainmentBoxOfficeStrategy {
    fn from_spec(spec: &StrategySpecArtifact) -> Result<Self, SpecError> {
        let min_edge_bps = spec.param_f64_or("min_edge_bps", 500.0)?;
        let confidence_floor = spec.param_f64_or("confidence_floor", 0.8)?;
        Ok(Self {
            strategy_id: spec.strategy_id.clone(),
            sleeve_id: format!("{}-sleeve", spec.strategy_id),
            signal_source: spec.param_str_or("signal_source", "apify-fandango"),
            market_id: spec.param_str_or("market_id", ""),
            target_gross: spec.param_f64_or("target_gross_usd", 0.0)?.round() as i64,
            baseline_gross: spec
                .param_f64_or("baseline_gross_usd", 1_000_000.0)?
                .round() as i64,
            extrapolation_hours: spec.param_f64_or("extrapolation_hours", 48.0)?.round() as i64,
            min_edge_ticks: ((min_edge_bps / 10_000.0) * PRICE_SCALE as f64).round() as i64,
            confidence_floor_ticks: (confidence_floor * PRICE_SCALE as f64).round() as i64,
            size: spec.param_str_or("size", "10"),
            next_client_order: 0,
            markets: HashMap::new(),
            priority_tier: priority_from_spec(spec).0,
            expires_after_ms: priority_from_spec(spec).1,
        })
    }
}

impl StrategyRuntime for EntertainmentBoxOfficeStrategy {
    fn strategy_id(&self) -> &str {
        &self.strategy_id
    }

    fn sleeve_id(&self) -> &str {
        &self.sleeve_id
    }

    fn priority_tier(&self) -> &str {
        &self.priority_tier
    }

    fn expires_after_ms(&self) -> Option<u64> {
        self.expires_after_ms
    }

    fn on_event(
        &mut self,
        event: &StrategyEvent,
        _ctx: &StrategyContext,
    ) -> Result<Vec<DecisionPayload>, RunnerError> {
        match event {
            StrategyEvent::Quote {
                market_id,
                instrument,
                bid,
                ask,
            } => {
                if bid.ticks() > 0 && ask.ticks() > 0 {
                    let state = self.markets.entry(market_id.clone()).or_default();
                    state.instrument = Some(instrument.clone());
                    state.mid = Some(FixedPrice((bid.ticks() + ask.ticks()) / 2));
                }
                Ok(vec![])
            }
            StrategyEvent::ExternalSignal {
                source,
                market_id,
                payload_json,
            } => {
                if !self.signal_source.is_empty() && source != &self.signal_source {
                    return Ok(vec![]);
                }
                if !self.market_id.is_empty() && market_id != &self.market_id {
                    return Ok(vec![]);
                }
                let Ok(payload) = serde_json::from_str::<serde_json::Value>(payload_json) else {
                    return Ok(vec![]);
                };
                // Confidence gate (parity with Python `confidence_floor`): a
                // missing confidence is treated as zero, so it is suppressed.
                let confidence_ticks = FixedPrice::parse(&payload, "confidence")
                    .map(|c| c.ticks())
                    .unwrap_or(0);
                if confidence_ticks < self.confidence_floor_ticks {
                    return Ok(vec![]);
                }
                let Ok(occ) = FixedPrice::parse(&payload, "seat_occupancy_pct") else {
                    return Ok(vec![]);
                };
                let Some(vel) = payload
                    .get("ticket_velocity_per_hour")
                    .and_then(json_f64)
                    .map(|v| v.round() as i64)
                else {
                    return Ok(vec![]);
                };
                let Some(state) = self.markets.get(market_id) else {
                    return Ok(vec![]);
                };
                let Some(mid) = state.mid else {
                    return Ok(vec![]);
                };

                // Weekend-gross extrapolation in whole dollars (integer-exact):
                //   extrapolated = occ * baseline + velocity * hours
                // `occ` is a price-scaled fraction (PRICE_ONE == 1.0).
                let occ_term = (occ.ticks() as i128 * self.baseline_gross as i128
                    / PRICE_SCALE as i128) as i64;
                let extrapolated = occ_term + vel * self.extrapolation_hours;
                let implied_ticks = gross_to_prob_ticks(extrapolated, self.target_gross);

                let edge = implied_ticks - mid.ticks();
                if edge.abs() < self.min_edge_ticks {
                    return Ok(vec![]);
                }
                self.next_client_order += 1;
                let outcome_side = if edge > 0 {
                    OutcomeSide::Yes
                } else {
                    OutcomeSide::No
                };
                // BUY of YES or of NO -> floor the per-side price to the venue
                // cent (edge-preserving; matches Python `buy_limit_from_fair`).
                let raw_price_ticks = if outcome_side == OutcomeSide::Yes {
                    mid.ticks()
                } else {
                    PRICE_SCALE - mid.ticks()
                };
                let price_ticks = pricing::buy_limit_from_fair(raw_price_ticks, pricing::CENT_TICK);
                let fair_ticks = if outcome_side == OutcomeSide::Yes {
                    implied_ticks
                } else {
                    PRICE_SCALE - implied_ticks
                };
                Ok(vec![DecisionPayload::PlaceOrder {
                    client_order_id: format!("c-box-{:08}", self.next_client_order),
                    instrument_id: state
                        .instrument
                        .clone()
                        .unwrap_or_else(|| format!("kalshi:{market_id}")),
                    outcome_side,
                    side: Side::Buy,
                    price: format_decimal_ticks(price_ticks),
                    quantity: self.size.clone(),
                    fair_price: Some(format_decimal_ticks(fair_ticks)),
                    min_executable_edge_ticks: Some(self.min_edge_ticks / 100),
                    fee_rate_bps: None,
                    time_in_force: TimeInForce::Gtc,
                }])
            }
            _ => Ok(vec![]),
        }
    }
}

// ---------- tennis XGBoost value strategy ----------

#[derive(Clone, Debug, Default)]
struct TennisMarketState {
    probability: Option<FixedPrice>,
    confidence: Option<FixedPrice>,
    odds_present: Option<bool>,
    yes_bid: Option<FixedPrice>,
    yes_ask: Option<FixedPrice>,
    instrument: Option<String>,
    pending_client_order_id: Option<String>,
    filled: bool,
    // entry intent (recorded at emit) so a fill establishes the held side/size
    // deterministically — OwnFill carries no outcome_side, so we cannot read it
    // off the fill. Mirrors the Python strategy for cross-language parity.
    entry_client_order_id: Option<String>,
    pending_side: Option<OutcomeSide>,
    pending_qty: Option<String>,
    // open-position / trailing-stop management
    holding: bool,
    held_side: Option<OutcomeSide>,
    held_qty: Option<String>,
    peak_ticks: Option<i64>,
    exit_client_order_id: Option<String>,
    closed: bool,
}

/// Rust implementation of `sports_tennis_xgboost`.
///
/// It expects:
/// - quote events with `instrument`, `bid`, and `ask`;
/// - external events from `prediction_source` with `market_id` and
///   `player_1_win_probability`.
///
/// Low player-1 probability emits an explicit `Buy NO` payload, matching the
/// Python strategy semantics.
pub struct TennisXgboostStrategy {
    pub strategy_id: String,
    pub sleeve_id: String,
    prediction_source: String,
    min_edge_ticks: i64,
    min_model_confidence_ticks: i64,
    require_odds_present: bool,
    size: String,
    // Trailing stop-loss in price ticks (0 disables -> pure hold-to-completion).
    stop_loss_ticks: i64,
    next_client_order: u64,
    markets: HashMap<String, TennisMarketState>,
    priority_tier: String,
    expires_after_ms: Option<u64>,
}

impl TennisXgboostStrategy {
    pub fn new(
        strategy_id: impl Into<String>,
        sleeve_id: impl Into<String>,
        prediction_source: impl Into<String>,
        min_edge_bps: f64,
        size: impl Into<String>,
    ) -> Self {
        Self {
            strategy_id: strategy_id.into(),
            sleeve_id: sleeve_id.into(),
            prediction_source: prediction_source.into(),
            min_edge_ticks: ((min_edge_bps / 10_000.0) * PRICE_SCALE as f64).round() as i64,
            min_model_confidence_ticks: 0,
            require_odds_present: false,
            size: size.into(),
            stop_loss_ticks: (0.12 * PRICE_SCALE as f64).round() as i64,
            next_client_order: 0,
            markets: HashMap::new(),
            priority_tier: "standard".to_string(),
            expires_after_ms: None,
        }
    }

    pub fn with_priority(mut self, tier: impl Into<String>, expires_after_ms: Option<u64>) -> Self {
        self.priority_tier = tier.into();
        self.expires_after_ms = expires_after_ms;
        self
    }

    /// Trailing stop-loss as a probability fraction (0.12 == 12c; 0 disables).
    pub fn with_trailing_stop(mut self, drop: f64) -> Self {
        self.stop_loss_ticks = (drop.max(0.0) * PRICE_SCALE as f64).round() as i64;
        self
    }

    pub fn with_signal_gates(
        mut self,
        min_model_confidence: f64,
        require_odds_present: bool,
    ) -> Self {
        self.min_model_confidence_ticks =
            (min_model_confidence * PRICE_SCALE as f64).round() as i64;
        self.require_odds_present = require_odds_present;
        self
    }

    fn maybe_decide(&mut self, market_id: &str) -> Result<Vec<DecisionPayload>, RunnerError> {
        let Some(state) = self.markets.get_mut(market_id) else {
            return Ok(vec![]);
        };
        if state.filled || state.holding || state.closed || state.pending_client_order_id.is_some()
        {
            return Ok(vec![]);
        }
        let (Some(probability), Some(yes_bid), Some(yes_ask)) =
            (state.probability, state.yes_bid, state.yes_ask)
        else {
            return Ok(vec![]);
        };
        if self.require_odds_present && state.odds_present != Some(true) {
            return Ok(vec![]);
        }
        let confidence_ticks = state
            .confidence
            .map(|confidence| confidence.ticks())
            .unwrap_or_else(|| probability.ticks().max(PRICE_SCALE - probability.ticks()));
        if confidence_ticks < self.min_model_confidence_ticks {
            return Ok(vec![]);
        }
        let yes_edge = probability.ticks() - yes_ask.ticks();
        let no_ask = PRICE_SCALE - yes_bid.ticks();
        let no_edge = (PRICE_SCALE - probability.ticks()) - no_ask;
        if yes_edge < self.min_edge_ticks && no_edge < self.min_edge_ticks {
            return Ok(vec![]);
        }
        self.next_client_order += 1;
        let client_order_id = if yes_edge >= no_edge {
            format!("c-tennis-buy-{:08}", self.next_client_order)
        } else {
            format!("c-tennis-buy-no-{:08}", self.next_client_order)
        };
        state.pending_client_order_id = Some(client_order_id.clone());
        // Record the entry intent so a fill establishes the held position+side.
        state.entry_client_order_id = Some(client_order_id.clone());
        state.pending_side = Some(if yes_edge >= no_edge {
            OutcomeSide::Yes
        } else {
            OutcomeSide::No
        });
        state.pending_qty = Some(self.size.clone());
        let instrument = state
            .instrument
            .clone()
            .unwrap_or_else(|| format!("kalshi:{market_id}"));
        if yes_edge >= no_edge {
            Ok(vec![DecisionPayload::PlaceOrder {
                client_order_id,
                instrument_id: instrument,
                outcome_side: OutcomeSide::Yes,
                side: Side::Buy,
                price: yes_ask.format(),
                quantity: self.size.clone(),
                fair_price: Some(probability.format()),
                min_executable_edge_ticks: Some(self.min_edge_ticks / 100),
                fee_rate_bps: None,
                time_in_force: TimeInForce::Ioc,
            }])
        } else {
            Ok(vec![DecisionPayload::PlaceOrder {
                client_order_id,
                instrument_id: instrument,
                outcome_side: OutcomeSide::No,
                side: Side::Buy,
                price: format_decimal_ticks(no_ask),
                quantity: self.size.clone(),
                fair_price: Some(format_decimal_ticks(PRICE_SCALE - probability.ticks())),
                min_executable_edge_ticks: Some(self.min_edge_ticks / 100),
                fee_rate_bps: None,
                time_in_force: TimeInForce::Ioc,
            }])
        }
    }

    /// Trailing-stop liquidation of an open position. Tracks the peak of the HELD
    /// side's best bid and sells (taker, at the bid) once it falls
    /// `stop_loss_ticks` below that peak. Otherwise the position holds (no
    /// take-profit). Mirrors the Python `_exit_decision` for parity.
    fn maybe_exit(&mut self, market_id: &str) -> Result<Vec<DecisionPayload>, RunnerError> {
        if self.stop_loss_ticks <= 0 {
            return Ok(vec![]);
        }
        let Some(state) = self.markets.get_mut(market_id) else {
            return Ok(vec![]);
        };
        if state.closed || !state.holding || state.exit_client_order_id.is_some() {
            return Ok(vec![]);
        }
        let (Some(yes_bid), Some(yes_ask), Some(held_side)) =
            (state.yes_bid, state.yes_ask, state.held_side)
        else {
            return Ok(vec![]);
        };
        let liquidation_ticks = match held_side {
            OutcomeSide::Yes => yes_bid.ticks(),
            OutcomeSide::No => PRICE_SCALE - yes_ask.ticks(),
        };
        let peak = state
            .peak_ticks
            .map_or(liquidation_ticks, |p| p.max(liquidation_ticks));
        state.peak_ticks = Some(peak);
        if peak - liquidation_ticks < self.stop_loss_ticks {
            return Ok(vec![]);
        }
        let quantity = state.held_qty.clone().unwrap_or_else(|| self.size.clone());
        let instrument = state
            .instrument
            .clone()
            .unwrap_or_else(|| format!("kalshi:{market_id}"));
        self.next_client_order += 1;
        let client_order_id = format!("c-tennis-sell-{:08}", self.next_client_order);
        state.exit_client_order_id = Some(client_order_id.clone());
        let price = format_decimal_ticks(liquidation_ticks);
        Ok(vec![DecisionPayload::PlaceOrder {
            client_order_id,
            instrument_id: instrument,
            outcome_side: held_side,
            side: Side::Sell,
            price,
            quantity,
            fair_price: None,
            min_executable_edge_ticks: None,
            fee_rate_bps: None,
            time_in_force: TimeInForce::Ioc,
        }])
    }

    /// A fill matching the recorded entry order establishes the held position;
    /// one matching the in-flight exit order closes it for good.
    fn apply_own_fill(&mut self, client_order_id: &str) {
        for market in self.markets.values_mut() {
            if market.exit_client_order_id.as_deref() == Some(client_order_id) {
                market.holding = false;
                market.held_qty = None;
                market.exit_client_order_id = None;
                market.closed = true;
                return;
            }
            if market.entry_client_order_id.as_deref() == Some(client_order_id) && !market.holding {
                market.holding = true;
                market.filled = true;
                market.held_side = market.pending_side;
                market.held_qty = market.pending_qty.clone();
                market.peak_ticks = None;
                market.pending_client_order_id = None;
                return;
            }
        }
    }

    fn apply_own_order_update(&mut self, client_order_id: &str, state: &str) {
        let terminal_retryable = matches!(
            state.trim().to_ascii_lowercase().as_str(),
            "rejected" | "canceled" | "cancelled" | "expired"
        );
        let filled = state.eq_ignore_ascii_case("filled");
        for market in self.markets.values_mut() {
            if market.pending_client_order_id.as_deref() == Some(client_order_id) {
                if filled {
                    market.filled = true;
                    market.pending_client_order_id = None;
                } else if terminal_retryable {
                    market.pending_client_order_id = None;
                }
            }
            // A failed liquidation must be retryable on the next adverse quote.
            if market.exit_client_order_id.as_deref() == Some(client_order_id) && terminal_retryable
            {
                market.exit_client_order_id = None;
            }
        }
    }

    fn clear_pending_order(&mut self, client_order_id: &str) {
        for market in self.markets.values_mut() {
            if market.pending_client_order_id.as_deref() == Some(client_order_id) {
                market.pending_client_order_id = None;
            }
        }
    }
}

impl FromSpec for TennisXgboostStrategy {
    fn from_spec(spec: &StrategySpecArtifact) -> Result<Self, SpecError> {
        let prediction_source = spec.param_str_or("prediction_source", "tennis_xgboost_onnx");
        let min_edge_bps = spec.param_f64_or("min_edge_bps", 150.0)?;
        let min_model_confidence = spec.param_f64_or("min_model_confidence", 0.0)?;
        let require_odds_present = spec.param_bool_or("require_odds_present", false)?;
        let size = spec.param_str_or("size", "5");
        let trailing_stop_loss = spec.param_f64_or("trailing_stop_loss", 0.12)?;
        let sleeve_id = format!("{}-sleeve", spec.strategy_id);
        let (tier, expires_after_ms) = priority_from_spec(spec);
        Ok(Self::new(
            spec.strategy_id.clone(),
            sleeve_id,
            prediction_source,
            min_edge_bps,
            size,
        )
        .with_signal_gates(min_model_confidence, require_odds_present)
        .with_trailing_stop(trailing_stop_loss)
        .with_priority(tier, expires_after_ms))
    }
}

impl StrategyRuntime for TennisXgboostStrategy {
    fn strategy_id(&self) -> &str {
        &self.strategy_id
    }

    fn sleeve_id(&self) -> &str {
        &self.sleeve_id
    }

    fn priority_tier(&self) -> &str {
        &self.priority_tier
    }

    fn expires_after_ms(&self) -> Option<u64> {
        self.expires_after_ms
    }

    fn on_intent_rejected(&mut self, client_order_id: &str, _reason: &str) {
        self.clear_pending_order(client_order_id);
    }

    fn on_event(
        &mut self,
        event: &StrategyEvent,
        _ctx: &StrategyContext,
    ) -> Result<Vec<DecisionPayload>, RunnerError> {
        if let StrategyEvent::Quote {
            instrument,
            market_id,
            bid,
            ask,
        } = event
        {
            let state = self.markets.entry(market_id.clone()).or_default();
            state.yes_bid = Some(*bid);
            state.yes_ask = Some(*ask);
            state.instrument = Some(instrument.clone());
            let manage_position = state.holding && !state.closed;
            if manage_position {
                return self.maybe_exit(market_id);
            }
            return self.maybe_decide(market_id);
        }
        if let StrategyEvent::TennisPrediction {
            source,
            market_id,
            probability,
            confidence,
            odds_present,
        } = event
        {
            if source != &self.prediction_source {
                return Ok(vec![]);
            }
            let state = self.markets.entry(market_id.to_string()).or_default();
            state.probability = Some(*probability);
            state.confidence = *confidence;
            state.odds_present = *odds_present;
            return self.maybe_decide(market_id);
        }
        if let StrategyEvent::OwnFill {
            client_order_id, ..
        } = event
        {
            self.apply_own_fill(client_order_id);
            return Ok(vec![]);
        }
        if let StrategyEvent::OwnOrderUpdate {
            client_order_id,
            state,
            ..
        } = event
        {
            self.apply_own_order_update(client_order_id, state);
        }
        Ok(vec![])
    }
}

// ---------- shared intent envelope builder ----------

/// Build a signed `IntentEnvelopeRecord` from a decision. Single source of
/// truth for the envelope shape — formerly duplicated in both `live-runner`
/// and `SleeveRunner::wrap_envelope` (drift risk).
///
/// `priority_tier` must be one of `"fast" | "standard" | "relaxed"`; the
/// envelope's `Contract::validate` checks this. `expires_after_ms`, if set,
/// is stamped into `audit.metadata["expires_after_ms"]` so the gateway can
/// enforce an explicit TTL — otherwise the gateway falls back to its
/// per-tier default.
#[allow(clippy::too_many_arguments)]
pub fn build_intent_envelope(
    strategy_id: &str,
    sleeve_id: &str,
    n: u64,
    decision: &DecisionPayload,
    now_rfc3339: &str,
    producer: &str,
    priority_tier: &str,
    expires_after_ms: Option<u64>,
) -> Result<IntentEnvelopeRecord, RunnerError> {
    let decision_json =
        serde_json::to_string(decision).map_err(|e| RunnerError::Decision(e.to_string()))?;
    let decision_kind = match decision {
        DecisionPayload::PlaceOrder { .. } => "place_order",
        DecisionPayload::CancelOrder { .. } => "cancel_order",
    }
    .to_string();
    let correlation_id = format!("{sleeve_id}-{n:08}");
    let object_id = format!("intent-{n:08}");
    let digest = canonical_sha256(&serde_json::json!({
        "strategy_id": strategy_id,
        "sleeve_id": sleeve_id,
        "correlation_id": correlation_id.clone(),
        "emitted_at": now_rfc3339,
        "decision_kind": decision_kind.clone(),
        "decision_json": decision_json.clone(),
        "priority_tier": priority_tier,
        "expires_after_ms": expires_after_ms,
    }))
    .map_err(|e| RunnerError::Decision(e.to_string()))?;
    let mut metadata = Metadata::new();
    if let Some(ttl) = expires_after_ms {
        metadata.insert("expires_after_ms".to_string(), ttl.to_string());
    }
    let envelope = IntentEnvelopeRecord {
        strategy_id: strategy_id.to_string(),
        sleeve_id: sleeve_id.to_string(),
        correlation_id,
        emitted_at: now_rfc3339.to_string(),
        decision_kind,
        decision_json,
        priority_tier: priority_tier.to_string(),
        audit: AuditStamp {
            object_id,
            object_kind: "intent_envelope".into(),
            schema_version: "intent-envelope-v1".into(),
            produced_at: now_rfc3339.to_string(),
            producer: producer.to_string(),
            canonical_sha256: digest,
            parent_ids: vec![],
            trace_id: None,
            metadata,
        },
    };
    envelope
        .validate()
        .map_err(|e| RunnerError::Decision(e.to_string()))?;
    Ok(envelope)
}

/// Lifecycle tracker shared between strategies that emit single-order-per-market
/// flows (TennisXgboost, OnnxQuote). Reset on terminal-non-Filled (rejected,
/// canceled, expired); sticky on Filled.
#[derive(Debug, Default)]
pub struct OrderTracker {
    /// market_id → pending client_order_id (Some if an order is outstanding).
    pub pending_by_market: HashMap<String, String>,
    /// client_order_id → market_id (reverse lookup for OwnOrderUpdate routing).
    pub client_to_market: HashMap<String, String>,
    /// market_id → true if an order on that market has filled in this process.
    pub filled_markets: HashMap<String, bool>,
}

impl OrderTracker {
    pub fn is_market_locked(&self, market_id: &str) -> bool {
        *self.filled_markets.get(market_id).unwrap_or(&false)
            || self.pending_by_market.contains_key(market_id)
    }

    pub fn record_pending(&mut self, market_id: &str, client_order_id: &str) {
        self.pending_by_market
            .insert(market_id.to_string(), client_order_id.to_string());
        self.client_to_market
            .insert(client_order_id.to_string(), market_id.to_string());
    }

    /// Apply a venue state report. Returns `true` if the tracker observed
    /// a terminal transition for a known order.
    pub fn apply_state(&mut self, client_order_id: &str, state: &str) -> bool {
        let Some(market_id) = self.client_to_market.get(client_order_id).cloned() else {
            return false;
        };
        match state.trim().to_ascii_lowercase().as_str() {
            "filled" => {
                self.pending_by_market.remove(&market_id);
                self.filled_markets.insert(market_id, true);
                true
            }
            "rejected" | "canceled" | "cancelled" | "expired" => {
                self.pending_by_market.remove(&market_id);
                self.client_to_market.remove(client_order_id);
                true
            }
            _ => false,
        }
    }

    pub fn clear_pending(&mut self, client_order_id: &str) -> bool {
        let Some(market_id) = self.client_to_market.remove(client_order_id) else {
            return false;
        };
        self.pending_by_market.remove(&market_id);
        true
    }
}

// ---------- ONNX quote-feature strategy ----------

/// Strategy that runs a [`Scorer`] on every `quote` event and emits a
/// `PlaceOrder` when the model's first output crosses a configured
/// threshold.
///
/// - `buy_yes_above`: model output ≥ this → Buy YES at ask.
/// - `buy_no_below`: model output ≤ this → Buy NO at (1 − bid).
/// - One order per instrument per process (matches `TennisXgboostStrategy`).
///
/// Generic over `S: Scorer` so production wires a real `OnnxScorer` and
/// tests pass an inline mock without depending on `ort`.
/// Max retained recent predictions in memory. Older predictions are evicted
/// FIFO. For full audit lineage you'd sink them to disk before eviction —
/// out of scope for this in-process MVP.
pub const PREDICTION_AUDIT_CAP: usize = 4096;

pub struct OnnxQuoteStrategy<S: Scorer> {
    pub strategy_id: String,
    pub sleeve_id: String,
    scorer: S,
    buy_yes_above: f32,
    buy_no_below: f32,
    size: String,
    next_client_order: u64,
    tracker: OrderTracker,
    rolling_quotes: HashMap<String, RollingQuoteState>,
    /// Recent predictions, capped at `PREDICTION_AUDIT_CAP`. Use a deque so
    /// eviction is O(1) rather than the previous unbounded `Vec`.
    pub prediction_audit: VecDeque<PredictionRecord>,
    priority_tier: String,
    expires_after_ms: Option<u64>,
}

impl<S: Scorer> OnnxQuoteStrategy<S> {
    pub fn new(
        strategy_id: impl Into<String>,
        sleeve_id: impl Into<String>,
        scorer: S,
        buy_yes_above: f32,
        buy_no_below: f32,
        size: impl Into<String>,
    ) -> Self {
        Self {
            strategy_id: strategy_id.into(),
            sleeve_id: sleeve_id.into(),
            scorer,
            buy_yes_above,
            buy_no_below,
            size: size.into(),
            next_client_order: 0,
            tracker: OrderTracker::default(),
            rolling_quotes: HashMap::new(),
            prediction_audit: VecDeque::with_capacity(PREDICTION_AUDIT_CAP),
            priority_tier: "standard".to_string(),
            expires_after_ms: None,
        }
    }

    pub fn with_priority(mut self, tier: impl Into<String>, expires_after_ms: Option<u64>) -> Self {
        self.priority_tier = tier.into();
        self.expires_after_ms = expires_after_ms;
        self
    }

    fn record_prediction(
        &mut self,
        instrument: &str,
        score: f32,
        timestamp: &str,
        source_event_id: &str,
        feature_hash: &str,
    ) -> Result<(), RunnerError> {
        let object_id = format!(
            "prediction:{}:{:08}",
            self.strategy_id,
            self.prediction_audit.len() + 1
        );
        let value = format!("{score:.8}");
        let digest = canonical_sha256(&serde_json::json!({
            "model_name": "onnx_quote",
            "model_version": self.strategy_id.clone(),
            "instrument_id": instrument,
            "timestamp": timestamp,
            "horizon_seconds": 0_u64,
            "value": value.clone(),
            "source_event_id": source_event_id,
        }))
        .map_err(|e| RunnerError::Strategy(e.to_string()))?;
        let mut metadata = Metadata::new();
        if !feature_hash.is_empty() {
            metadata.insert("feature_hash".to_string(), feature_hash.to_string());
        }
        // N12: prediction audit links to the originating normalized event
        // via parent_ids, closing the lineage chain quote → prediction →
        // intent.
        let parent_ids = if source_event_id.is_empty() {
            vec![]
        } else {
            vec![source_event_id.to_string()]
        };
        let record = PredictionRecord {
            model_name: "onnx_quote".into(),
            model_version: self.strategy_id.clone(),
            instrument_id: Some(instrument.into()),
            timestamp: timestamp.into(),
            horizon_seconds: 0,
            value,
            confidence: None,
            audit: AuditStamp {
                object_id,
                object_kind: "prediction".into(),
                schema_version: "prediction-v1".into(),
                produced_at: timestamp.into(),
                producer: self.strategy_id.clone(),
                canonical_sha256: digest,
                parent_ids,
                trace_id: None,
                metadata,
            },
        };
        record
            .validate()
            .map_err(|e| RunnerError::Strategy(e.to_string()))?;
        // Bounded ring: drop oldest when full so memory stays O(1) over the
        // life of a long-running process (fix for F10 — unbounded growth).
        if self.prediction_audit.len() >= PREDICTION_AUDIT_CAP {
            self.prediction_audit.pop_front();
        }
        self.prediction_audit.push_back(record);
        Ok(())
    }
}

impl<S: Scorer> StrategyRuntime for OnnxQuoteStrategy<S> {
    fn strategy_id(&self) -> &str {
        &self.strategy_id
    }
    fn sleeve_id(&self) -> &str {
        &self.sleeve_id
    }
    fn priority_tier(&self) -> &str {
        &self.priority_tier
    }
    fn expires_after_ms(&self) -> Option<u64> {
        self.expires_after_ms
    }
    fn on_intent_rejected(&mut self, client_order_id: &str, _reason: &str) {
        self.tracker.clear_pending(client_order_id);
    }
    fn on_event(
        &mut self,
        event: &StrategyEvent,
        ctx: &StrategyContext,
    ) -> Result<Vec<DecisionPayload>, RunnerError> {
        if let StrategyEvent::OwnOrderUpdate {
            client_order_id,
            state,
            ..
        } = event
        {
            self.tracker.apply_state(client_order_id, state);
            return Ok(vec![]);
        }

        let StrategyEvent::Quote {
            instrument,
            market_id,
            bid,
            ask,
        } = event
        else {
            return Ok(vec![]);
        };
        if !(bid.ticks() > 0 && ask.ticks() > 0) {
            return Ok(vec![]);
        }
        if self.tracker.is_market_locked(market_id) {
            return Ok(vec![]);
        }

        // Runner FixedPrice scale is 1e6; convert once to dollar f32 for the
        // model. Feature builder owns the canonical layout.
        let bid_dollars = bid.ticks() as f32 / PRICE_SCALE as f32;
        let ask_dollars = ask.ticks() as f32 / PRICE_SCALE as f32;
        let input_width = self.scorer.input_width();
        if input_width != QUOTE_FEATURE_WIDTH && input_width != ROLLING_QUOTE_FEATURE_WIDTH {
            return Err(RunnerError::Strategy(format!(
                "scorer input width {} must be {} (stateless quote) or {} (rolling quote)",
                input_width, QUOTE_FEATURE_WIDTH, ROLLING_QUOTE_FEATURE_WIDTH
            )));
        }
        let rolling_features;
        let stateless_features;
        let features: &[f32] = if input_width == ROLLING_QUOTE_FEATURE_WIDTH {
            let state = self.rolling_quotes.entry(instrument.clone()).or_default();
            rolling_features = quote_features_rolling_raw(state, bid_dollars, ask_dollars);
            &rolling_features
        } else {
            stateless_features = quote_features_raw(bid_dollars, ask_dollars);
            &stateless_features
        };
        let out = self
            .scorer
            .predict(features)
            .map_err(|e| RunnerError::Strategy(format!("scorer error: {e}")))?;
        let score = out
            .first()
            .copied()
            .ok_or_else(|| RunnerError::Strategy("scorer returned empty output".into()))?;
        // Hash the feature vector so the prediction record links to the
        // exact numeric input that produced the score (N12 audit chain).
        // Wrap the slice so it serializes as a JSON array (slice itself
        // is unsized for the generic bound).
        let feature_hash = canonical_sha256(&features.to_vec())
            .map_err(|e| RunnerError::Strategy(e.to_string()))?;
        self.record_prediction(
            instrument,
            score,
            &ctx.now,
            &ctx.source_event_id,
            &feature_hash,
        )?;

        if score >= self.buy_yes_above {
            self.next_client_order += 1;
            let client_order_id = format!("c-onnx-yes-{:08}", self.next_client_order);
            self.tracker.record_pending(market_id, &client_order_id);
            return Ok(vec![DecisionPayload::PlaceOrder {
                client_order_id,
                instrument_id: instrument.clone(),
                outcome_side: OutcomeSide::Yes,
                side: Side::Buy,
                price: ask.format(),
                quantity: self.size.clone(),
                fair_price: Some(format!("{score:.4}")),
                min_executable_edge_ticks: Some(0),
                fee_rate_bps: None,
                time_in_force: TimeInForce::Ioc,
            }]);
        }
        if score <= self.buy_no_below {
            self.next_client_order += 1;
            let client_order_id = format!("c-onnx-no-{:08}", self.next_client_order);
            self.tracker.record_pending(market_id, &client_order_id);
            // Buying NO at the complement price (1 − bid) is the standard
            // way to express a Kalshi NO buy when only YES quotes are
            // streamed; the venue adapter routes price into no_price.
            let no_price_ticks = PRICE_SCALE - bid.ticks();
            return Ok(vec![DecisionPayload::PlaceOrder {
                client_order_id,
                instrument_id: instrument.clone(),
                outcome_side: OutcomeSide::No,
                side: Side::Buy,
                price: format_decimal_ticks(no_price_ticks),
                quantity: self.size.clone(),
                fair_price: Some(format!("{:.4}", 1.0_f32 - score)),
                min_executable_edge_ticks: Some(0),
                fee_rate_bps: None,
                time_in_force: TimeInForce::Ioc,
            }]);
        }
        Ok(vec![])
    }
}

fn market_id_from_instrument(instrument: &str) -> String {
    instrument
        .rsplit_once(':')
        .map(|(_venue, market)| market.to_string())
        .unwrap_or_else(|| instrument.to_string())
}

fn parse_decimal_ticks(raw: &str) -> Result<i64, RunnerError> {
    if raw.is_empty() {
        return Err(RunnerError::Strategy("empty decimal".into()));
    }
    let negative = raw.starts_with('-');
    let body = if negative { &raw[1..] } else { raw };
    let mut parts = body.split('.');
    let whole = parts
        .next()
        .ok_or_else(|| RunnerError::Strategy(format!("invalid decimal `{raw}`")))?;
    let frac = parts.next().unwrap_or("");
    if parts.next().is_some()
        || whole.is_empty()
        || !whole.bytes().all(|b| b.is_ascii_digit())
        || frac.len() > 6
        || !frac.bytes().all(|b| b.is_ascii_digit())
    {
        return Err(RunnerError::Strategy(format!("invalid decimal `{raw}`")));
    }
    let whole_ticks = whole
        .parse::<i64>()
        .map_err(|e| RunnerError::Strategy(format!("invalid decimal `{raw}`: {e}")))?
        .checked_mul(PRICE_SCALE)
        .ok_or_else(|| RunnerError::Strategy(format!("decimal overflow `{raw}`")))?;
    let mut frac_padded = frac.to_string();
    while frac_padded.len() < 6 {
        frac_padded.push('0');
    }
    let frac_ticks = if frac_padded.is_empty() {
        0
    } else {
        frac_padded
            .parse::<i64>()
            .map_err(|e| RunnerError::Strategy(format!("invalid decimal `{raw}`: {e}")))?
    };
    let ticks = whole_ticks
        .checked_add(frac_ticks)
        .ok_or_else(|| RunnerError::Strategy(format!("decimal overflow `{raw}`")))?;
    Ok(if negative { -ticks } else { ticks })
}

/// Format an internal 1e6-scale tick value as a decimal string with at most 4
/// fractional digits.
///
/// The runner carries prices at 1e6 internally, but EVERY downstream consumer —
/// the risk gate (`parse_fixed`), the gateway (`parse_fixed_4`), and the Kalshi
/// venue itself (whole-cent grid) — works at 1e4 and REJECTS any string with
/// more than 4 fractional digits. A model probability like 0.967731 would
/// otherwise emit "0.967731" (6 dp) and be rejected as `InvalidNumeric`, which
/// is exactly what blocked every live tennis intent. Rounding to the 1e4 grid is
/// lossless with respect to anything that actually trades. Round-half-up on the
/// magnitude keeps it symmetric for the NO side (`PRICE_SCALE - ticks`).
fn format_decimal_ticks(ticks: i64) -> String {
    let sign = if ticks < 0 { "-" } else { "" };
    let abs = ticks.unsigned_abs();
    // 1e6 -> 1e4 ticks, rounding half up (1e6 / 1e4 = 100).
    let abs_1e4 = (abs + 50) / 100;
    let whole = abs_1e4 / 10_000;
    let frac = abs_1e4 % 10_000;
    if frac == 0 {
        return format!("{sign}{whole}");
    }
    let mut frac_text = format!("{frac:04}");
    while frac_text.ends_with('0') {
        frac_text.pop();
    }
    format!("{sign}{whole}.{frac_text}")
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
            let strategy_event = StrategyEvent::from_record(&event)?;
            let now_str = (self.now_provider)();
            let now_epoch = epoch_seconds_from_rfc3339(&now_str);
            if let StrategyEvent::Quote {
                instrument,
                bid,
                ask,
                ..
            } = &strategy_event
            {
                // 4-decimal mark for risk + gateway last-look. Runner's
                // FixedPrice is 10^6 scale → divide by 100 to land in 10^4.
                let bid_ticks_4dp = bid.ticks() / 100;
                let ask_ticks_4dp = ask.ticks() / 100;
                record_quote_bbo(
                    self.sleeve_state,
                    instrument,
                    bid_ticks_4dp,
                    ask_ticks_4dp,
                    now_epoch,
                );
                if let Some(gw) = self.gateway.as_deref_mut() {
                    record_quote_bbo(
                        &mut gw.sleeve_state,
                        instrument,
                        bid_ticks_4dp,
                        ask_ticks_4dp,
                        now_epoch,
                    );
                }
            }
            let ctx = StrategyContext::from_sleeve_state(now_str, self.sleeve_state)
                .with_source_event(event.event_id.clone());
            let decisions = self.strategy.on_event(&strategy_event, &ctx)?;
            summary.decisions_emitted += decisions.len() as u64;

            for decision in decisions {
                let envelope = self.wrap_envelope(&decision)?;
                let approved = match &decision {
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
                        ..
                    } => {
                        let snap = IntentSnapshot {
                            client_order_id: client_order_id.clone(),
                            instrument_id: instrument_id.clone(),
                            outcome_side: *outcome_side,
                            side: *side,
                            price: price.clone(),
                            quantity: quantity.clone(),
                            fair_price: fair_price.clone(),
                            min_executable_edge_ticks: *min_executable_edge_ticks,
                            fee_rate_bps: *fee_rate_bps,
                        };
                        match self.risk.evaluate(self.sleeve_state, &snap, now_epoch) {
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
        build_intent_envelope(
            self.strategy.strategy_id(),
            self.strategy.sleeve_id(),
            self.next_corr,
            decision,
            &now,
            "runner",
            self.strategy.priority_tier(),
            self.strategy.expires_after_ms(),
        )
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

    #[test]
    fn box_office_gross_to_prob_clamps_and_interpolates() {
        // ratio = extrapolated / target_gross, mapped to [0.05, 0.95]:
        // <=0.5 -> 0.05 ; >=1.5 -> 0.95 ; else 0.05 + (ratio-0.5)*0.9.
        let target = 100_000_000;
        assert_eq!(gross_to_prob_ticks(40_000_000, target), 50_000); // ratio 0.4 -> clamp low
        assert_eq!(gross_to_prob_ticks(50_000_000, target), 50_000); // ratio 0.5 boundary
        assert_eq!(gross_to_prob_ticks(100_000_000, target), 500_000); // ratio 1.0 -> 0.50
        assert_eq!(gross_to_prob_ticks(120_000_000, target), 680_000); // ratio 1.2 -> 0.68 (fixture)
        assert_eq!(gross_to_prob_ticks(150_000_000, target), 950_000); // ratio 1.5 boundary
        assert_eq!(gross_to_prob_ticks(200_000_000, target), 950_000); // ratio 2.0 -> clamp high
        assert_eq!(gross_to_prob_ticks(100_000_000, 0), 50_000); // degenerate target -> floor
    }

    fn audit() -> AuditStamp {
        AuditStamp {
            object_id: "event-1".into(),
            object_kind: "normalized_event".into(),
            schema_version: "normalized-event-v1".into(),
            produced_at: "2026-05-26T12:00:00Z".into(),
            producer: "fixture".into(),
            canonical_sha256: "a".repeat(64),
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

    fn entry_fill() -> StrategyEvent {
        // The deterministic id the Rust strategy assigns to its first entry.
        StrategyEvent::OwnFill {
            client_order_id: "c-tennis-buy-00000001".into(),
            instrument: "kalshi:M-1".into(),
            price: FixedPrice::from_f64(0.57),
            quantity: 5,
            remaining_quantity: 0,
        }
    }

    #[test]
    fn tennis_trailing_stop_liquidates_when_bid_drops_from_peak() {
        let mut strat = TennisXgboostStrategy::new(
            "sports-tennis-xgboost-v1",
            "sports-tennis-paper",
            "tennis_xgboost_onnx",
            150.0,
            "5",
        ); // default trailing stop = 0.12
        strat
            .on_event(&strategy_event(&tennis_signal("M-1", "0.70")), &ctx())
            .unwrap();
        let entry = strat
            .on_event(&strategy_event(&quote("0.55", "0.57")), &ctx())
            .unwrap();
        assert!(matches!(
            &entry[0],
            DecisionPayload::PlaceOrder {
                side: Side::Buy,
                ..
            }
        ));
        strat.on_event(&entry_fill(), &ctx()).unwrap();

        // Bid climbs to a 0.66 peak — held to completion, no take-profit.
        assert!(strat
            .on_event(&strategy_event(&quote("0.60", "0.62")), &ctx())
            .unwrap()
            .is_empty());
        assert!(strat
            .on_event(&strategy_event(&quote("0.66", "0.68")), &ctx())
            .unwrap()
            .is_empty());
        // Bid falls 0.13 from the peak (>= 0.12) -> liquidate at the bid.
        let exit = strat
            .on_event(&strategy_event(&quote("0.53", "0.55")), &ctx())
            .unwrap();
        assert_eq!(exit.len(), 1);
        assert!(matches!(
            &exit[0],
            DecisionPayload::PlaceOrder {
                side: Side::Sell,
                outcome_side: OutcomeSide::Yes,
                price,
                quantity,
                time_in_force: TimeInForce::Ioc,
                ..
            } if price == "0.53" && quantity == "5"
        ));
    }

    #[test]
    fn tennis_trailing_stop_holds_within_threshold() {
        let mut strat = TennisXgboostStrategy::new(
            "sports-tennis-xgboost-v1",
            "sports-tennis-paper",
            "tennis_xgboost_onnx",
            150.0,
            "5",
        );
        strat
            .on_event(&strategy_event(&tennis_signal("M-1", "0.70")), &ctx())
            .unwrap();
        strat
            .on_event(&strategy_event(&quote("0.55", "0.57")), &ctx())
            .unwrap();
        strat.on_event(&entry_fill(), &ctx()).unwrap();
        strat
            .on_event(&strategy_event(&quote("0.66", "0.68")), &ctx())
            .unwrap(); // peak 0.66
                       // 0.07 drop < 0.12 stop -> still holding.
        assert!(strat
            .on_event(&strategy_event(&quote("0.59", "0.61")), &ctx())
            .unwrap()
            .is_empty());
    }

    #[test]
    fn tennis_disabled_trailing_stop_holds_to_completion() {
        let mut strat = TennisXgboostStrategy::new(
            "sports-tennis-xgboost-v1",
            "sports-tennis-paper",
            "tennis_xgboost_onnx",
            150.0,
            "5",
        )
        .with_trailing_stop(0.0); // disabled
        strat
            .on_event(&strategy_event(&tennis_signal("M-1", "0.70")), &ctx())
            .unwrap();
        strat
            .on_event(&strategy_event(&quote("0.55", "0.57")), &ctx())
            .unwrap();
        strat.on_event(&entry_fill(), &ctx()).unwrap();
        strat
            .on_event(&strategy_event(&quote("0.66", "0.68")), &ctx())
            .unwrap();
        // Huge drop, but the stop is disabled -> position still held.
        assert!(strat
            .on_event(&strategy_event(&quote("0.20", "0.22")), &ctx())
            .unwrap()
            .is_empty());
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

    fn tennis_signal(market_id: &str, probability: &str) -> NormalizedEventRecord {
        let payload = serde_json::json!({
            "source": "tennis_xgboost_onnx",
            "market_id": market_id,
            "player_1_win_probability": probability,
        });
        NormalizedEventRecord {
            event_id: format!("tennis-{market_id}-{probability}"),
            event_kind: "external".into(),
            payload_json: payload.to_string(),
            provenance: EventProvenance {
                source: "tennis_xgboost_onnx".into(),
                channel: "external".into(),
                schema_version: "normalized-event-v1".into(),
                venue: Some("kalshi".into()),
                source_sequence: None,
                normalization_version: "1".into(),
                metadata: Metadata::new(),
            },
            audit: audit(),
        }
    }

    fn tennis_signal_with_context(
        market_id: &str,
        probability: &str,
        confidence: &str,
        odds_present: bool,
    ) -> NormalizedEventRecord {
        let payload = serde_json::json!({
            "source": "tennis_xgboost_onnx",
            "market_id": market_id,
            "player_1_win_probability": probability,
            "model_confidence": confidence,
            "odds_present": odds_present,
        });
        NormalizedEventRecord {
            event_id: format!("tennis-{market_id}-{probability}"),
            event_kind: "external".into(),
            payload_json: payload.to_string(),
            provenance: EventProvenance {
                source: "tennis_xgboost_onnx".into(),
                channel: "external".into(),
                schema_version: "normalized-event-v1".into(),
                venue: Some("kalshi".into()),
                source_sequence: None,
                normalization_version: "1".into(),
                metadata: Metadata::new(),
            },
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

    fn ctx() -> StrategyContext {
        StrategyContext::from_sleeve_state("2026-05-26T12:00:00Z", &SleeveState::default())
    }

    fn strategy_event(record: &NormalizedEventRecord) -> StrategyEvent {
        StrategyEvent::from_record(record).unwrap()
    }

    #[test]
    fn strategy_context_exposes_sleeve_state() {
        let mut state = SleeveState {
            open_orders: 2,
            ..Default::default()
        };
        state.positions.insert(
            "kalshi:M-1".into(),
            eventcontracts_risk::Position {
                quantity: 7,
                avg_price_ticks: 4200,
            },
        );

        let ctx = StrategyContext::from_sleeve_state("2026-05-26T12:00:00Z", &state);

        assert_eq!(ctx.open_orders(), 2);
        assert_eq!(ctx.position_quantity("kalshi:M-1"), Some(7.0));
    }

    #[test]
    fn threshold_strategy_emits_intent_when_mid_below_threshold() {
        let mut strat =
            ThresholdStrategy::new("weather-v1", "weather-kalshi-paper-a", 0.5, 1.0, "10");
        let decisions = strat
            .on_event(&strategy_event(&quote("0.30", "0.32")), &ctx())
            .unwrap();
        assert_eq!(decisions.len(), 1);
        assert!(matches!(decisions[0], DecisionPayload::PlaceOrder { .. }));
    }

    #[test]
    fn threshold_strategy_emits_nothing_when_mid_above_threshold() {
        let mut strat =
            ThresholdStrategy::new("weather-v1", "weather-kalshi-paper-a", 0.5, 1.0, "10");
        let decisions = strat
            .on_event(&strategy_event(&quote("0.55", "0.57")), &ctx())
            .unwrap();
        assert_eq!(decisions.len(), 0);
    }

    #[test]
    fn tennis_strategy_buys_yes_when_probability_beats_ask() {
        let mut strat = TennisXgboostStrategy::new(
            "sports-tennis-xgboost-v1",
            "sports-tennis-paper",
            "tennis_xgboost_onnx",
            150.0,
            "5",
        );

        assert!(strat
            .on_event(&strategy_event(&tennis_signal("M-1", "0.70")), &ctx())
            .unwrap()
            .is_empty());
        let decisions = strat
            .on_event(&strategy_event(&quote("0.55", "0.57")), &ctx())
            .unwrap();

        assert_eq!(decisions.len(), 1);
        assert!(matches!(
            &decisions[0],
            DecisionPayload::PlaceOrder {
                side: Side::Buy,
                price,
                quantity,
                ..
            } if price == "0.57" && quantity == "5"
        ));
        assert!(strat
            .on_event(&strategy_event(&quote("0.54", "0.56")), &ctx())
            .unwrap()
            .is_empty());
    }

    #[test]
    fn tennis_strategy_buys_no_when_probability_is_below_bid() {
        let mut strat = TennisXgboostStrategy::new(
            "sports-tennis-xgboost-v1",
            "sports-tennis-paper",
            "tennis_xgboost_onnx",
            150.0,
            "5",
        );

        assert!(strat
            .on_event(&strategy_event(&quote("0.60", "0.62")), &ctx())
            .unwrap()
            .is_empty());
        let decisions = strat
            .on_event(&strategy_event(&tennis_signal("M-1", "0.38")), &ctx())
            .unwrap();

        assert_eq!(decisions.len(), 1);
        assert!(matches!(
            &decisions[0],
            DecisionPayload::PlaceOrder {
                outcome_side: OutcomeSide::No,
                side: Side::Buy,
                price,
                ..
            } if price == "0.4"
        ));
    }

    #[test]
    fn format_decimal_ticks_caps_at_four_dp_and_rounds() {
        // Internal scale is 1e6; downstream risk/gateway/Kalshi are 1e4 and
        // reject >4 dp. Whole + <=4dp values must be exact; finer ticks round
        // half-up onto the 1e4 grid. Regression for the live tennis
        // InvalidNumeric{fair_price} reject (a 6-dp probability string).
        assert_eq!(format_decimal_ticks(700_000), "0.7");
        assert_eq!(format_decimal_ticks(570_000), "0.57");
        assert_eq!(format_decimal_ticks(1_000_000), "1");
        assert_eq!(format_decimal_ticks(0), "0");
        // 0.967731 (a real model probability) -> 4 dp, rounded half-up.
        assert_eq!(format_decimal_ticks(967_731), "0.9677");
        assert_eq!(format_decimal_ticks(967_750), "0.9678");
        // NO-side complement PRICE_SCALE - 967_731 = 32_269 -> "0.0323".
        assert_eq!(format_decimal_ticks(PRICE_SCALE - 967_731), "0.0323");
        // Every output has at most 4 fractional digits.
        for ticks in [1_i64, 49, 50, 51, 123_456, 999_999, 967_731] {
            let s = format_decimal_ticks(ticks);
            if let Some((_, frac)) = s.split_once('.') {
                assert!(frac.len() <= 4, "{s} has >4 dp");
            }
        }
    }

    /// Regression: a realistic 6-dp model probability must yield a `fair_price`
    /// string the risk gate accepts (<=4 dp), on BOTH the YES and NO branch.
    /// Before the fix this emitted "0.967731"/"0.032269" and every live tennis
    /// intent was rejected InvalidNumeric{fair_price}. Mirrors risk/gateway's
    /// `frac.len() > 4` rule.
    #[test]
    fn tennis_fair_price_is_risk_parseable_for_realistic_probability() {
        fn max_4dp(s: &str) -> bool {
            s.split_once('.').map(|(_, f)| f.len() <= 4).unwrap_or(true)
        }
        let new_strat = || {
            TennisXgboostStrategy::new(
                "sports-tennis-xgboost-v1",
                "sports-tennis-paper",
                "tennis_xgboost_onnx",
                150.0,
                "5",
            )
        };

        // YES branch: prob 0.9677 (a real 4-dp model output that exposed the
        // >4-dp bug live) well above the ask -> BUY YES.
        let mut strat = new_strat();
        let _ = strat
            .on_event(&strategy_event(&tennis_signal("M-1", "0.9677")), &ctx())
            .unwrap();
        let decisions = strat
            .on_event(&strategy_event(&quote("0.55", "0.57")), &ctx())
            .unwrap();
        assert_eq!(decisions.len(), 1);
        let DecisionPayload::PlaceOrder {
            outcome_side,
            price,
            fair_price,
            ..
        } = &decisions[0]
        else {
            panic!("expected place order");
        };
        assert_eq!(*outcome_side, OutcomeSide::Yes);
        let fp = fair_price.as_deref().expect("fair_price");
        assert!(max_4dp(fp), "YES fair_price {fp} exceeds 4 dp");
        assert!(max_4dp(price), "YES price {price} exceeds 4 dp");

        // NO branch: prob 0.0323 below the bid -> BUY NO; the NO fair_price is
        // the 1e6 complement, which is exactly where the 6-dp string came from.
        let mut strat = new_strat();
        let _ = strat
            .on_event(&strategy_event(&tennis_signal("M-1", "0.0323")), &ctx())
            .unwrap();
        let decisions = strat
            .on_event(&strategy_event(&quote("0.60", "0.62")), &ctx())
            .unwrap();
        assert_eq!(decisions.len(), 1);
        let DecisionPayload::PlaceOrder {
            outcome_side,
            price,
            fair_price,
            ..
        } = &decisions[0]
        else {
            panic!("expected place order");
        };
        assert_eq!(*outcome_side, OutcomeSide::No);
        let fp = fair_price.as_deref().expect("fair_price");
        assert!(max_4dp(fp), "NO fair_price {fp} exceeds 4 dp");
        assert!(max_4dp(price), "NO price {price} exceeds 4 dp");
    }

    /// F5 invariant: the live tennis taker only ever emits IOC `PlaceOrder`s,
    /// across the model/quote input range and on BOTH the YES- and NO-favoured
    /// branches. This is what makes the absence of a `ReplaceOrder` variant
    /// safe: an IOC order cannot rest, so a partial-fill tail is canceled, never
    /// repriced. A future maker/quoting sleeve that introduces resting orders
    /// must add its own replace-path coverage; this guards the taker against
    /// silently growing a non-IOC code path.
    #[test]
    fn tennis_taker_emits_ioc_orders_only() {
        // (probability, yes_bid, yes_ask): strong-YES, strong-NO, and tighter
        // books that still clear the edge gate on exactly one side.
        let cases = [
            ("0.90", "0.55", "0.57"),
            ("0.80", "0.55", "0.57"),
            ("0.10", "0.60", "0.62"),
            ("0.20", "0.60", "0.62"),
            ("0.70", "0.55", "0.57"),
            ("0.30", "0.60", "0.62"),
        ];
        for (prob, bid, ask) in cases {
            let mut strat = TennisXgboostStrategy::new(
                "sports-tennis-xgboost-v1",
                "sports-tennis-paper",
                "tennis_xgboost_onnx",
                150.0,
                "5",
            );
            let _ = strat
                .on_event(&strategy_event(&tennis_signal("M-1", prob)), &ctx())
                .unwrap();
            let decisions = strat
                .on_event(&strategy_event(&quote(bid, ask)), &ctx())
                .unwrap();
            for decision in &decisions {
                match decision {
                    DecisionPayload::PlaceOrder {
                        time_in_force,
                        side,
                        ..
                    } => {
                        assert_eq!(
                            *time_in_force,
                            TimeInForce::Ioc,
                            "taker order for prob={prob} bid={bid} ask={ask} must be IOC"
                        );
                        // The taker only ever buys into the opposite touch.
                        assert_eq!(*side, Side::Buy);
                    }
                    DecisionPayload::CancelOrder { .. } => {
                        panic!("a fresh taker signal must never emit a cancel")
                    }
                }
            }
        }
    }

    #[test]
    fn tennis_strategy_clears_pending_after_gateway_rejection() {
        let mut strat = TennisXgboostStrategy::new(
            "sports-tennis-xgboost-v1",
            "sports-tennis-paper",
            "tennis_xgboost_onnx",
            150.0,
            "5",
        );
        strat
            .on_event(&strategy_event(&tennis_signal("M-1", "0.70")), &ctx())
            .unwrap();
        let first = strat
            .on_event(&strategy_event(&quote("0.55", "0.57")), &ctx())
            .unwrap();
        let DecisionPayload::PlaceOrder {
            client_order_id, ..
        } = &first[0]
        else {
            panic!("expected PlaceOrder");
        };
        let rejected_id = client_order_id.clone();

        strat.on_intent_rejected(&rejected_id, "last_look");
        let second = strat
            .on_event(&strategy_event(&quote("0.54", "0.56")), &ctx())
            .unwrap();

        assert_eq!(second.len(), 1);
    }

    #[test]
    fn tennis_strategy_honors_confidence_and_odds_gates() {
        let mut strat = TennisXgboostStrategy::new(
            "sports-tennis-xgboost-v1",
            "sports-tennis-paper",
            "tennis_xgboost_onnx",
            150.0,
            "5",
        )
        .with_signal_gates(0.62, true);
        let ctx = ctx();
        assert!(strat
            .on_event(&strategy_event(&quote("0.55", "0.57")), &ctx)
            .unwrap()
            .is_empty());

        let signal = |confidence: i64, odds_present: bool| StrategyEvent::TennisPrediction {
            source: "tennis_xgboost_onnx".into(),
            market_id: "M-1".into(),
            probability: FixedPrice(700_000),
            confidence: Some(FixedPrice(confidence)),
            odds_present: Some(odds_present),
        };

        assert!(strat
            .on_event(&signal(610_000, true), &ctx)
            .unwrap()
            .is_empty());
        assert!(strat
            .on_event(&signal(700_000, false), &ctx)
            .unwrap()
            .is_empty());
        assert_eq!(
            strat.on_event(&signal(700_000, true), &ctx).unwrap().len(),
            1
        );
    }

    #[test]
    fn tennis_external_signal_parses_confidence_and_odds_context() {
        let event = strategy_event(&tennis_signal_with_context("M-1", "0.70", "0.70", true));

        assert!(matches!(
            event,
            StrategyEvent::TennisPrediction {
                probability,
                confidence: Some(confidence),
                odds_present: Some(true),
                ..
            } if probability.ticks() == 700_000 && confidence.ticks() == 700_000
        ));
    }

    /// Closure-backed `Scorer` used in tests so `OnnxQuoteStrategy` can be
    /// exercised without pulling in `ort`.
    struct MockScorer {
        width: usize,
        out: Vec<f32>,
    }
    impl eventcontracts_feature_builder::Scorer for MockScorer {
        fn input_width(&self) -> usize {
            self.width
        }
        fn predict(
            &self,
            features: &[f32],
        ) -> Result<Vec<f32>, eventcontracts_feature_builder::ScorerError> {
            assert_eq!(features.len(), self.width, "feature width invariant");
            Ok(self.out.clone())
        }
    }

    #[test]
    fn onnx_quote_strategy_buys_yes_when_score_above_threshold() {
        let scorer = MockScorer {
            width: QUOTE_FEATURE_WIDTH,
            out: vec![0.80],
        };
        let mut strat = OnnxQuoteStrategy::new(
            "onnx-quote-v1",
            "onnx-quote-paper",
            scorer,
            /* buy_yes_above = */ 0.65,
            /* buy_no_below = */ 0.35,
            "5",
        );
        let decisions = strat
            .on_event(&strategy_event(&quote("0.55", "0.57")), &ctx())
            .unwrap();
        assert_eq!(decisions.len(), 1);
        assert!(matches!(
            &decisions[0],
            DecisionPayload::PlaceOrder {
                outcome_side: OutcomeSide::Yes,
                side: Side::Buy,
                price,
                quantity,
                ..
            } if price == "0.57" && quantity == "5"
        ));
    }

    #[test]
    fn onnx_quote_strategy_buys_no_when_score_below_threshold() {
        let scorer = MockScorer {
            width: QUOTE_FEATURE_WIDTH,
            out: vec![0.20],
        };
        let mut strat =
            OnnxQuoteStrategy::new("onnx-quote-v1", "onnx-quote-paper", scorer, 0.65, 0.35, "5");
        let decisions = strat
            .on_event(&strategy_event(&quote("0.40", "0.42")), &ctx())
            .unwrap();
        assert_eq!(decisions.len(), 1);
        let DecisionPayload::PlaceOrder {
            outcome_side,
            price,
            ..
        } = &decisions[0]
        else {
            panic!("expected PlaceOrder");
        };
        assert_eq!(*outcome_side, OutcomeSide::No);
        // bid was $0.40 → no_price = 1.0 − 0.40 = $0.60
        assert_eq!(price, "0.6");
    }

    #[test]
    fn onnx_quote_strategy_holds_when_score_inside_band() {
        let scorer = MockScorer {
            width: QUOTE_FEATURE_WIDTH,
            out: vec![0.50],
        };
        let mut strat =
            OnnxQuoteStrategy::new("onnx-quote-v1", "onnx-quote-paper", scorer, 0.65, 0.35, "5");
        assert!(strat
            .on_event(&strategy_event(&quote("0.45", "0.50")), &ctx())
            .unwrap()
            .is_empty());
    }

    #[test]
    fn onnx_quote_strategy_only_orders_once_per_instrument() {
        let scorer = MockScorer {
            width: QUOTE_FEATURE_WIDTH,
            out: vec![0.99],
        };
        let mut strat =
            OnnxQuoteStrategy::new("onnx-quote-v1", "onnx-quote-paper", scorer, 0.65, 0.35, "5");
        let first = strat
            .on_event(&strategy_event(&quote("0.40", "0.42")), &ctx())
            .unwrap();
        let second = strat
            .on_event(&strategy_event(&quote("0.41", "0.43")), &ctx())
            .unwrap();
        assert_eq!(first.len(), 1);
        assert_eq!(second.len(), 0);
    }

    #[test]
    fn onnx_quote_strategy_clears_pending_after_gateway_rejection() {
        let scorer = MockScorer {
            width: QUOTE_FEATURE_WIDTH,
            out: vec![0.99],
        };
        let mut strat =
            OnnxQuoteStrategy::new("onnx-quote-v1", "onnx-quote-paper", scorer, 0.65, 0.35, "5");
        let first = strat
            .on_event(&strategy_event(&quote("0.40", "0.42")), &ctx())
            .unwrap();
        let DecisionPayload::PlaceOrder {
            client_order_id, ..
        } = &first[0]
        else {
            panic!("expected PlaceOrder");
        };
        let rejected_id = client_order_id.clone();

        strat.on_intent_rejected(&rejected_id, "risk_rejected");
        let second = strat
            .on_event(&strategy_event(&quote("0.41", "0.43")), &ctx())
            .unwrap();

        assert_eq!(second.len(), 1);
    }

    #[test]
    fn onnx_quote_strategy_rejects_scorer_with_wrong_input_width() {
        let scorer = MockScorer {
            width: 99,
            out: vec![0.99],
        };
        let mut strat =
            OnnxQuoteStrategy::new("onnx-quote-v1", "onnx-quote-paper", scorer, 0.65, 0.35, "5");
        let err = strat
            .on_event(&strategy_event(&quote("0.40", "0.42")), &ctx())
            .unwrap_err();
        assert!(matches!(err, RunnerError::Strategy(msg) if msg.contains("input width")));
    }

    #[test]
    fn end_to_end_run_emits_intent_routes_to_gateway_and_records_summary() {
        let mut bus = InMemoryBus::new();
        bus.publish(quote("0.30", "0.32"));
        bus.publish(quote("0.55", "0.57"));
        bus.publish(quote("0.20", "0.22"));

        let mut strat =
            ThresholdStrategy::new("weather-v1", "weather-kalshi-paper-a", 0.5, 1.0, "10");
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
        let mut strat =
            ThresholdStrategy::new("weather-v1", "weather-kalshi-paper-a", 0.5, 1.0, "10");
        let risk = RiskGate::new(limits());
        let mut state = SleeveState::default();
        let mut sink = InMemoryBus::new();
        let mut t = 0i64;
        let now = Box::new(move || {
            t += 1;
            format!("2026-05-26T12:00:{:02}Z", t)
        });
        let summary: RunSummary = SleeveRunner::<_, _, RecordingVenueClient>::new(
            &mut strat, &mut bus, &risk, &mut state, &mut sink, None, now,
        )
        .run()
        .unwrap();
        assert_eq!(summary.intents_approved, 1);
        assert_eq!(summary.gateway_acks, 0);
        assert_eq!(sink.emitted.len(), 1);
    }

    fn weather_strategy() -> WeatherTemperatureArbitrageStrategy {
        let spec = StrategySpecArtifact::from_toml_str(
            r#"
strategy_id = "weather-temperature-arbitrage-live-v1"
name = "weather_temperature_arbitrage"
version = "1.0.0"
[parameters]
signal_source = "open-meteo"
min_edge_bps = "150"
size = "1"
max_spread = "0.80"
spread_edge_multiplier = "0.10"
near_binary_price = "0.95"
near_binary_min_edge_bps = "600"
max_signal_age_seconds = 180
min_seconds_to_close = "300"
min_retrade_price_delta = "0.03"
min_retrade_probability_delta = "0.04"
[tags]
sleeve_id = "weather-kalshi-live-a"
"#,
        )
        .unwrap();
        WeatherTemperatureArbitrageStrategy::from_spec(&spec).unwrap()
    }

    fn weather_signal(market_id: &str, probability_ticks: i64) -> StrategyEvent {
        weather_signal_with_context(market_id, probability_ticks, None, None)
    }

    fn weather_signal_with_context(
        market_id: &str,
        probability_ticks: i64,
        lead_days: Option<i64>,
        close_time: Option<&str>,
    ) -> StrategyEvent {
        StrategyEvent::ExternalProbability {
            source: "open-meteo".into(),
            market_id: market_id.into(),
            probability: FixedPrice(probability_ticks),
            confidence: None,
            lead_days,
            close_time: close_time.map(str::to_string),
        }
    }

    fn weather_external_record(
        market_id: &str,
        probability: &str,
        lead_days: i64,
        close_time: &str,
    ) -> NormalizedEventRecord {
        let payload = serde_json::json!({
            "source": "open-meteo",
            "market_id": market_id,
            "implied_prob": probability,
            "lead_days": lead_days,
            "close_time": close_time,
            "instrument_id": {
                "venue": "kalshi",
                "market_id": market_id,
            },
        });
        NormalizedEventRecord {
            event_id: format!("weather-{market_id}-{probability}"),
            event_kind: "external".into(),
            payload_json: payload.to_string(),
            provenance: EventProvenance {
                source: "open-meteo".into(),
                channel: "external".into(),
                schema_version: "weather-temperature-probability-v1".into(),
                venue: Some("kalshi".into()),
                source_sequence: None,
                normalization_version: "normalizer-v1".into(),
                metadata: Metadata::new(),
            },
            audit: audit(),
        }
    }

    #[test]
    fn weather_strategy_buys_yes_when_signal_beats_live_ask() {
        let mut strat = weather_strategy();
        let ctx = ctx();
        assert!(strat
            .on_event(&weather_signal("M-1", 700_000), &ctx)
            .unwrap()
            .is_empty());
        let decisions = strat
            .on_event(&strategy_event(&quote("0.55", "0.57")), &ctx)
            .unwrap();

        assert_eq!(decisions.len(), 1);
        assert!(matches!(
            &decisions[0],
            DecisionPayload::PlaceOrder {
                outcome_side: OutcomeSide::Yes,
                side: Side::Buy,
                price,
                quantity,
                fair_price: Some(fair_price),
                time_in_force: TimeInForce::Ioc,
                fee_rate_bps: Some(700),
                ..
            } if price == "0.57" && quantity == "1" && fair_price == "0.7"
        ));
    }

    #[test]
    fn weather_strategy_buys_no_at_complement_of_yes_bid() {
        let mut strat = weather_strategy();
        let ctx = ctx();
        let _ = strat
            .on_event(
                &StrategyEvent::Quote {
                    market_id: "M-1".into(),
                    instrument: "kalshi:M-1".into(),
                    bid: FixedPrice(600_000),
                    ask: FixedPrice(620_000),
                },
                &ctx,
            )
            .unwrap();
        let decisions = strat
            .on_event(&weather_signal("M-1", 300_000), &ctx)
            .unwrap();

        assert_eq!(decisions.len(), 1);
        assert!(matches!(
            &decisions[0],
            DecisionPayload::PlaceOrder {
                outcome_side: OutcomeSide::No,
                side: Side::Buy,
                price,
                fair_price: Some(fair_price),
                ..
            } if price == "0.4" && fair_price == "0.7"
        ));
    }

    #[test]
    fn weather_strategy_book_tick_inverts_kalshi_no_bid_to_yes_ask() {
        let mut strat = weather_strategy();
        let ctx = ctx();
        let _ = strat
            .on_event(&weather_signal("M-1", 700_000), &ctx)
            .unwrap();
        let decisions = strat
            .on_event(
                &StrategyEvent::Book {
                    market_id: "M-1".into(),
                    instrument: "kalshi:M-1".into(),
                    bids: vec![(FixedPrice(400_000), 20)],
                    // Kalshi book "no" levels are no bids. The weather taker
                    // must invert 0.58 to a YES ask at 0.42 before trading.
                    asks: vec![(FixedPrice(580_000), 10)],
                    is_snapshot: true,
                },
                &ctx,
            )
            .unwrap();

        assert_eq!(decisions.len(), 1);
        assert!(matches!(
            &decisions[0],
            DecisionPayload::PlaceOrder {
                outcome_side: OutcomeSide::Yes,
                price,
                ..
            } if price == "0.42"
        ));
    }

    #[test]
    fn weather_strategy_retrade_gate_suppresses_duplicate_tick() {
        let mut strat = weather_strategy();
        let ctx = ctx();
        let _ = strat
            .on_event(&weather_signal("M-1", 700_000), &ctx)
            .unwrap();
        let first = strat
            .on_event(&strategy_event(&quote("0.55", "0.57")), &ctx)
            .unwrap();
        let second = strat
            .on_event(&strategy_event(&quote("0.55", "0.57")), &ctx)
            .unwrap();

        assert_eq!(first.len(), 1);
        assert!(second.is_empty());
    }

    #[test]
    fn weather_strategy_suppresses_non_zero_lead_without_caching() {
        let mut strat = weather_strategy();
        let ctx = ctx();
        strat
            .on_event(
                &weather_signal_with_context("M-1", 700_000, Some(1), None),
                &ctx,
            )
            .unwrap();

        assert!(strat
            .on_event(&strategy_event(&quote("0.55", "0.57")), &ctx)
            .unwrap()
            .is_empty());

        // Older producers without a lead tag are still accepted once a quote is
        // cached; this proves the lead=1 signal was refused rather than cached.
        assert_eq!(
            strat
                .on_event(&weather_signal("M-1", 700_000), &ctx)
                .unwrap()
                .len(),
            1
        );
    }

    #[test]
    fn weather_strategy_suppresses_signal_within_close_buffer() {
        let mut strat = weather_strategy();
        let ctx = ctx();
        strat
            .on_event(&strategy_event(&quote("0.55", "0.57")), &ctx)
            .unwrap();

        let near = strat
            .on_event(
                &weather_signal_with_context("M-1", 700_000, Some(0), Some("2026-05-26T12:01:00Z")),
                &ctx,
            )
            .unwrap();
        assert!(near.is_empty());
        assert!(strat
            .on_event(&strategy_event(&quote("0.55", "0.57")), &ctx)
            .unwrap()
            .is_empty());

        let far = strat
            .on_event(
                &weather_signal_with_context("M-1", 700_000, Some(0), Some("2026-05-26T13:00:00Z")),
                &ctx,
            )
            .unwrap();
        assert_eq!(far.len(), 1);
    }

    #[test]
    fn weather_external_signal_parses_lead_and_close_context() {
        let event = strategy_event(&weather_external_record(
            "M-1",
            "0.70",
            0,
            "2026-05-26T13:00:00Z",
        ));

        assert!(matches!(
            event,
            StrategyEvent::ExternalProbability {
                source,
                market_id,
                probability,
                lead_days: Some(0),
                close_time: Some(close_time),
                ..
            } if source == "open-meteo"
                && market_id == "M-1"
                && probability.ticks() == 700_000
                && close_time == "2026-05-26T13:00:00Z"
        ));
    }

    #[test]
    fn external_edge_confidence_gate_suppresses_low_confidence() {
        // Closes the Python↔Rust crop divergence: the generic external_edge
        // runtime must honour `min_confidence` exactly like the Python strategy,
        // so a low- or no-confidence signal cannot trade live.
        let spec = StrategySpecArtifact::from_toml_str(
            r#"
strategy_id = "crop-conf-v1"
name = "crop_conf"
version = "1.0.0"
[parameters]
signal_source = "src"
min_edge_bps = "100"
min_confidence = "0.55"
size = "3"
[tags]
archetype = "external_edge"
"#,
        )
        .unwrap();
        let mut strat = ExternalEdgeStrategy::from_spec(&spec).unwrap();
        let ctx = ctx();
        // A quote sets the per-market mid (0.41) so a signal would otherwise fire.
        strat
            .on_event(
                &StrategyEvent::Quote {
                    market_id: "KXM".into(),
                    instrument: "kalshi:KXM".into(),
                    bid: FixedPrice(400_000),
                    ask: FixedPrice(420_000),
                },
                &ctx,
            )
            .unwrap();
        let signal = |conf: Option<i64>| StrategyEvent::ExternalProbability {
            source: "src".into(),
            market_id: "KXM".into(),
            probability: FixedPrice(700_000),
            confidence: conf.map(FixedPrice),
            lead_days: None,
            close_time: None,
        };
        // Below the 0.55 gate -> suppressed.
        assert!(strat
            .on_event(&signal(Some(400_000)), &ctx)
            .unwrap()
            .is_empty());
        // Missing confidence with a gate configured -> suppressed (treated as 0).
        assert!(strat.on_event(&signal(None), &ctx).unwrap().is_empty());
        // At/above the gate -> emits exactly one order.
        assert_eq!(
            strat.on_event(&signal(Some(900_000)), &ctx).unwrap().len(),
            1
        );
    }
}
