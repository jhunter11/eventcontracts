//! Strongly-typed crypto-strategy domain shared by every research-grade
//! Rust crate.
//!
//! Mirrors the surface of [`python/src/eventcontracts/crypto/`] plus a
//! subset of [`python/src/eventcontracts/domain/`] — the union of types
//! the crypto signal ensemble, historical loader, and backtester need.
//!
//! The package is intentionally small. Anything that touches storage,
//! networking, or pricing math lives in a higher crate; this one is
//! pure data plus tiny validators.

use chrono::{DateTime, Utc};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};
use thiserror::Error;

// ----------------------------- IDs -----------------------------

macro_rules! string_id {
    ($name:ident, $doc:expr) => {
        #[doc = $doc]
        #[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
        #[serde(transparent)]
        pub struct $name(pub String);

        impl $name {
            pub fn new(value: impl Into<String>) -> Self {
                Self(value.into())
            }
            pub fn as_str(&self) -> &str {
                &self.0
            }
        }

        impl std::fmt::Display for $name {
            fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                write!(f, "{}", self.0)
            }
        }
    };
}

string_id!(EventId, "Identifier for one normalized event.");
string_id!(StrategyId, "Identifier for a strategy.");
string_id!(SleeveId, "Identifier for a sleeve binding.");
string_id!(CorrelationId, "Trace identifier carried across async boundaries.");
string_id!(ClientOrderId, "Client-assigned order identifier.");

// ----------------------------- Enums -----------------------------

/// Trading venue. Matches the Python `eventcontracts.domain.models.Venue`
/// string values exactly.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Venue {
    Kalshi,
    PolymarketGlobal,
    PolymarketUs,
}

impl Venue {
    pub fn as_str(&self) -> &'static str {
        match self {
            Venue::Kalshi => "kalshi",
            Venue::PolymarketGlobal => "polymarket_global",
            Venue::PolymarketUs => "polymarket_us",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum OutcomeSide {
    Yes,
    No,
}

impl OutcomeSide {
    pub fn as_str(&self) -> &'static str {
        match self {
            OutcomeSide::Yes => "yes",
            OutcomeSide::No => "no",
        }
    }
    /// Sign used in net-edge aggregation: YES = +1, NO = -1.
    pub fn sign(&self) -> Decimal {
        match self {
            OutcomeSide::Yes => Decimal::ONE,
            OutcomeSide::No => Decimal::NEGATIVE_ONE,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum OrderSide {
    Buy,
    Sell,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum OrderType {
    Limit,
    Market,
    PostOnly,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum TimeInForce {
    Gtc,
    Ioc,
    Fok,
    Gtd,
}

// ----------------------------- Composite types -----------------------------

#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct InstrumentId {
    pub venue: Venue,
    pub market_id: String,
    #[serde(default)]
    pub outcome_id: Option<String>,
}

impl InstrumentId {
    pub fn new(venue: Venue, market_id: impl Into<String>) -> Self {
        Self {
            venue,
            market_id: market_id.into(),
            outcome_id: None,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OrderBookLevel {
    pub price: Decimal,
    pub quantity: Decimal,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Quote {
    pub instrument_id: InstrumentId,
    pub side: OutcomeSide,
    pub bid: Option<OrderBookLevel>,
    pub ask: Option<OrderBookLevel>,
    pub exchange_ts: Option<DateTime<Utc>>,
    pub received_at: DateTime<Utc>,
}

impl Quote {
    pub fn mid(&self) -> Option<Decimal> {
        match (&self.bid, &self.ask) {
            (Some(b), Some(a)) => Some((b.price + a.price) / Decimal::TWO),
            _ => None,
        }
    }

    pub fn spread_bps(&self) -> Option<Decimal> {
        let mid = self.mid()?;
        if mid <= Decimal::ZERO {
            return None;
        }
        let (b, a) = (self.bid.as_ref()?, self.ask.as_ref()?);
        Some((a.price - b.price) / mid * Decimal::ONE_HUNDRED * Decimal::ONE_HUNDRED)
    }
}

// ----------------------------- Signal types -----------------------------

/// Per-instrument directional view at a point in time.
///
/// `side` is the outcome bracket the signal recommends *buying*; `edge_bps`
/// is always non-negative, with direction carried by `side`. `confidence`
/// is the source's self-rated reliability in `[0, 1]`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Signal {
    pub instrument_id: InstrumentId,
    pub side: OutcomeSide,
    pub edge_bps: Decimal,
    pub confidence: Decimal,
    pub horizon_seconds: i64,
    pub source: String,
    pub reason: String,
}

/// Aggregate verdict for one instrument after `combine_signals`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct EnsembleVerdict {
    pub instrument_id: InstrumentId,
    /// `None` when the verdict is HOLD (net edge below threshold).
    pub side: Option<OutcomeSide>,
    pub net_edge_bps: Decimal,
    pub contributing_sources: Vec<String>,
    pub per_source_edge_bps: Vec<(String, Decimal)>,
}

// ----------------------------- Strategy decisions -----------------------------

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PlaceOrder {
    pub client_order_id: ClientOrderId,
    pub instrument_id: InstrumentId,
    pub outcome_side: OutcomeSide,
    pub order_side: OrderSide,
    pub order_type: OrderType,
    pub time_in_force: TimeInForce,
    pub quantity: Decimal,
    pub price: Option<Decimal>,
    #[serde(default)]
    pub reason: String,
    pub expected_edge_bps: Option<Decimal>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum AlertSeverity {
    Info,
    Warn,
    Error,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Alert {
    pub severity: AlertSeverity,
    pub message: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct NoAction {
    #[serde(default)]
    pub reason: String,
}

/// Closed sum type of every decision a strategy can emit.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum StrategyDecision {
    PlaceOrder(PlaceOrder),
    Alert(Alert),
    NoAction(NoAction),
}

// ----------------------------- Fill / settlement -----------------------------

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Liquidity {
    Maker,
    Taker,
    Unknown,
}

impl Liquidity {
    pub fn as_str(&self) -> &'static str {
        match self {
            Liquidity::Maker => "maker",
            Liquidity::Taker => "taker",
            Liquidity::Unknown => "unknown",
        }
    }
}

/// Realized fill after a backtester applies a quote and a fee.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct FilledTrade {
    pub market_id: String,
    pub outcome_side: OutcomeSide,
    pub fill_price: Decimal,
    pub quantity: Decimal,
    pub fee_amount: Decimal,
    pub fee_currency: String,
    pub payout_per_contract: Decimal,
    pub pnl_per_contract: Decimal,
    pub pnl_total: Decimal,
    pub sources: Vec<String>,
    pub decision_at: DateTime<Utc>,
    pub settled: bool,
}

// ----------------------------- Validation -----------------------------

#[derive(Debug, Clone, Error, PartialEq, Eq)]
pub enum ValidationError {
    #[error("{field} must be non-empty")]
    Empty { field: &'static str },
    #[error("{field} must be a probability in [0, 1]: {value}")]
    NotProbability { field: &'static str, value: String },
    #[error("{field} must be positive: {value}")]
    NotPositive { field: &'static str, value: String },
}

pub fn require_probability(value: Decimal, field: &'static str) -> Result<(), ValidationError> {
    if value < Decimal::ZERO || value > Decimal::ONE {
        return Err(ValidationError::NotProbability {
            field,
            value: value.to_string(),
        });
    }
    Ok(())
}

pub fn require_positive(value: Decimal, field: &'static str) -> Result<(), ValidationError> {
    if value <= Decimal::ZERO {
        return Err(ValidationError::NotPositive {
            field,
            value: value.to_string(),
        });
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::str::FromStr;

    #[test]
    fn outcome_side_sign_matches_python_convention() {
        assert_eq!(OutcomeSide::Yes.sign(), Decimal::ONE);
        assert_eq!(OutcomeSide::No.sign(), Decimal::NEGATIVE_ONE);
    }

    #[test]
    fn quote_mid_is_average_of_bid_and_ask() {
        let q = Quote {
            instrument_id: InstrumentId::new(Venue::Kalshi, "M-1"),
            side: OutcomeSide::Yes,
            bid: Some(OrderBookLevel {
                price: Decimal::from_str("0.40").unwrap(),
                quantity: Decimal::from(100),
            }),
            ask: Some(OrderBookLevel {
                price: Decimal::from_str("0.42").unwrap(),
                quantity: Decimal::from(100),
            }),
            exchange_ts: None,
            received_at: Utc::now(),
        };
        assert_eq!(q.mid(), Some(Decimal::from_str("0.41").unwrap()));
    }

    #[test]
    fn venue_round_trips_through_json() {
        let v = Venue::Kalshi;
        let json = serde_json::to_string(&v).unwrap();
        assert_eq!(json, "\"kalshi\"");
        assert_eq!(serde_json::from_str::<Venue>(&json).unwrap(), v);
    }
}
