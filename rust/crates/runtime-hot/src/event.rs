//! Hot-path event types — what strategy workers and the CEG consume after
//! the bus boundary.
//!
//! All variants are owned (`SmolStr` for IDs, fixed-point integers for
//! numerics). No `String` allocations, no `f64`, no per-tick parsing once
//! the projection has run.

use arrayvec::ArrayVec;
use serde::{Deserialize, Serialize};

use crate::types::{FixedPrice, InstrumentId, Qty};

/// Max book depth carried on the stack. Kalshi's top-of-book is the only
/// thing most strategies need; arb engines reading deeper levels can
/// reconfigure this constant or fall back to parsing the raw payload.
pub const MAX_BOOK_LEVELS: usize = 32;

/// A single price level — `(price, aggregated_size)`. 16 bytes.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq, Serialize, Deserialize)]
pub struct Level {
    pub price: FixedPrice,
    pub size: Qty,
}

/// Top-of-book quote (best bid / best ask).
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct HotQuote {
    pub instrument: InstrumentId,
    pub bid: FixedPrice,
    pub ask: FixedPrice,
}

impl HotQuote {
    #[inline]
    pub fn spread(&self) -> FixedPrice {
        self.ask.saturating_sub(self.bid)
    }

    #[inline]
    pub fn mid(&self) -> FixedPrice {
        // Integer mid; bias is at most 1 unit. Strategies that need exact
        // half-tick semantics should consume bid/ask directly.
        FixedPrice((self.bid.raw() + self.ask.raw()) / 2)
    }
}

/// Last-trade tick.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct HotTrade {
    pub instrument: InstrumentId,
    pub price: FixedPrice,
    pub size: Qty,
}

/// Full or partial book update. `is_snapshot` distinguishes the initial
/// state-load from incremental deltas — strategies that maintain a local
/// book must reset on snapshots and merge on deltas.
///
/// Levels are stack-allocated (`ArrayVec`). Books deeper than
/// `MAX_BOOK_LEVELS` are truncated at projection time — see [`HotBook::truncated`].
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct HotBook {
    pub instrument: InstrumentId,
    pub is_snapshot: bool,
    pub bids: ArrayVec<Level, MAX_BOOK_LEVELS>,
    pub asks: ArrayVec<Level, MAX_BOOK_LEVELS>,
    /// True if the venue sent more levels than `MAX_BOOK_LEVELS`; the tail
    /// has been dropped. Strategies that require full depth should monitor
    /// this and either widen the bound or fall back to a slow path.
    pub truncated: bool,
}

impl HotBook {
    #[inline]
    pub fn best_bid(&self) -> Option<Level> {
        self.bids.first().copied()
    }

    #[inline]
    pub fn best_ask(&self) -> Option<Level> {
        self.asks.first().copied()
    }
}

/// Own-fill notification from the venue feedback loop. Mirrors the shape
/// the OMS consumes (`eventcontracts_oms::Fill`) but with hot-path types.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct HotOwnFill {
    pub fill_id: InstrumentId,
    pub client_order_id: InstrumentId,
    pub instrument: InstrumentId,
    pub price: FixedPrice,
    pub quantity: Qty,
    pub remaining_quantity: Qty,
    pub fee: FixedPrice,
}

/// Own-order state update from the venue (ack, partial, cancel, reject).
/// The state string is preserved verbatim so this crate stays decoupled
/// from the OMS state machine — downstream code maps it.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct HotOwnOrderUpdate {
    pub client_order_id: InstrumentId,
    pub instrument: InstrumentId,
    pub state: smol_str::SmolStr,
    pub reason: Option<smol_str::SmolStr>,
}

/// Venue-side market lifecycle state. Mirrors the Python
/// `MarketLifecycleKind` so a single typed value crosses the boundary.
/// Only `Opened` and `Resumed` are tradable; everything else is an
/// administrative pause/halt/end-of-life.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum MarketState {
    Listed,
    Opened,
    Paused,
    Resumed,
    MetadataUpdated,
    Closed,
    Determined,
    Disputed,
    Finalized,
}

impl MarketState {
    /// True only for states in which the venue is accepting orders. All
    /// other states must reject new placements and trigger cancels for
    /// existing resting orders on that instrument.
    #[inline]
    pub fn is_tradable(self) -> bool {
        matches!(self, MarketState::Opened | MarketState::Resumed)
    }

    /// Map a Kalshi `event_type` string to the canonical `MarketState`.
    /// Mirrors the Python `_lifecycle_kind` map in normalization/kalshi.py
    /// so single-source-of-truth lives in the contract.
    pub fn from_kalshi_event_type(event_type: &str) -> Self {
        match event_type {
            "created" => MarketState::Listed,
            "activated" => MarketState::Opened,
            "deactivated" => MarketState::Paused,
            "resumed" => MarketState::Resumed,
            "close_date_updated" | "metadata_updated" => MarketState::MetadataUpdated,
            "closed" => MarketState::Closed,
            "determined" => MarketState::Determined,
            "disputed" => MarketState::Disputed,
            "settled" | "finalized" => MarketState::Finalized,
            // Unknown lifecycle labels are informational rather than a
            // reason to halt the market.
            _ => MarketState::Listed,
        }
    }
}

/// Typed market-state event. Carries the instrument the venue moved and
/// the new state. `reason` preserves the raw venue label so downstream
/// audits can correlate with the source feed.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct HotMarketState {
    pub instrument: InstrumentId,
    pub state: MarketState,
    pub reason: Option<smol_str::SmolStr>,
}

/// The discriminant kind. Useful for routing tables / metrics without
/// destructuring the full `HotEvent`.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq, Serialize, Deserialize)]
pub enum HotEventKind {
    Quote,
    Trade,
    Book,
    OwnFill,
    OwnOrderUpdate,
    OwnOrderReject,
    MarketState,
    Settlement,
    External,
    Timer,
}

/// The unified hot-path event. Strategy workers match on this directly;
/// no downstream component should ever need to re-parse the original
/// `NormalizedEventRecord.payload_json`.
#[allow(clippy::large_enum_variant)]
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub enum HotEvent {
    Quote(HotQuote),
    Trade(HotTrade),
    Book(HotBook),
    OwnFill(HotOwnFill),
    OwnOrderUpdate(HotOwnOrderUpdate),
    OwnOrderReject(HotOwnOrderUpdate),
    /// Typed market lifecycle transition. The gateway uses this to gate
    /// placements and to cancel resting orders on a non-tradable state.
    MarketState(HotMarketState),
    /// Events whose payloads carry no fields the hot path acts on
    /// (settlement, external, timer). The instrument id, if the venue
    /// supplied one, is preserved for routing.
    Passthrough {
        kind: HotEventKind,
        instrument: Option<InstrumentId>,
    },
}

impl HotEvent {
    pub fn kind(&self) -> HotEventKind {
        match self {
            HotEvent::Quote(_) => HotEventKind::Quote,
            HotEvent::Trade(_) => HotEventKind::Trade,
            HotEvent::Book(_) => HotEventKind::Book,
            HotEvent::OwnFill(_) => HotEventKind::OwnFill,
            HotEvent::OwnOrderUpdate(_) => HotEventKind::OwnOrderUpdate,
            HotEvent::OwnOrderReject(_) => HotEventKind::OwnOrderReject,
            HotEvent::MarketState(_) => HotEventKind::MarketState,
            HotEvent::Passthrough { kind, .. } => *kind,
        }
    }

    pub fn instrument(&self) -> Option<&InstrumentId> {
        match self {
            HotEvent::Quote(q) => Some(&q.instrument),
            HotEvent::Trade(t) => Some(&t.instrument),
            HotEvent::Book(b) => Some(&b.instrument),
            HotEvent::OwnFill(f) => Some(&f.instrument),
            HotEvent::OwnOrderUpdate(u) | HotEvent::OwnOrderReject(u) => Some(&u.instrument),
            HotEvent::MarketState(m) => Some(&m.instrument),
            HotEvent::Passthrough { instrument, .. } => instrument.as_ref(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_opened_and_resumed_are_tradable() {
        assert!(MarketState::Opened.is_tradable());
        assert!(MarketState::Resumed.is_tradable());
        for state in [
            MarketState::Listed,
            MarketState::Paused,
            MarketState::MetadataUpdated,
            MarketState::Closed,
            MarketState::Determined,
            MarketState::Disputed,
            MarketState::Finalized,
        ] {
            assert!(!state.is_tradable(), "{state:?} must not be tradable");
        }
    }

    #[test]
    fn kalshi_event_type_mapping_matches_python_normalizer() {
        // Mirror of `_lifecycle_kind` in python/.../normalization/kalshi.py
        // — any divergence is a cross-language contract bug.
        assert_eq!(
            MarketState::from_kalshi_event_type("created"),
            MarketState::Listed
        );
        assert_eq!(
            MarketState::from_kalshi_event_type("activated"),
            MarketState::Opened
        );
        assert_eq!(
            MarketState::from_kalshi_event_type("deactivated"),
            MarketState::Paused
        );
        assert_eq!(
            MarketState::from_kalshi_event_type("close_date_updated"),
            MarketState::MetadataUpdated
        );
        assert_eq!(
            MarketState::from_kalshi_event_type("determined"),
            MarketState::Determined
        );
        assert_eq!(
            MarketState::from_kalshi_event_type("settled"),
            MarketState::Finalized
        );
        assert_eq!(
            MarketState::from_kalshi_event_type("metadata_updated"),
            MarketState::MetadataUpdated
        );
        assert_eq!(
            MarketState::from_kalshi_event_type("anything_unknown"),
            MarketState::Listed
        );
    }
}
