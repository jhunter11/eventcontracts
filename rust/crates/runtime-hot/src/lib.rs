//! Hot-path runtime projections of contract records.
//!
//! `contracts` is the cross-language wire layer: decimals are `String`,
//! timestamps are RFC3339 strings, and the canonical-JSON / sha256 chain is
//! preserved exactly. Strategy and execution code paid that price once at the
//! bus boundary by parsing every tick.
//!
//! This crate is the in-process representation downstream of that boundary:
//!
//! - prices are `FixedPrice(i64)` in 1/10000 of a USD (so 1 Kalshi cent = 100,
//!   one Polymarket sub-cent fits losslessly, range ±$922T),
//! - quantities are `Qty(u32)`,
//! - instrument / correlation / order ids are `SmolStr` so the common case
//!   (≤23 bytes) avoids heap allocation entirely,
//! - book levels live in `ArrayVec<Level, MAX_BOOK_LEVELS>` so depth carries
//!   on the stack.
//!
//! The projection runs once per envelope at the bus subscriber (see
//! [`project::project_event`]). Strategy workers, the CEG, and arbitrage
//! engines consume `HotEvent` values from then on and never touch the JSON
//! payload again. Audit-bearing writes always go back through `contracts`.

pub mod event;
pub mod project;
pub mod types;

pub use event::{
    HotBook, HotEvent, HotEventKind, HotMarketState, HotOwnFill, HotOwnOrderUpdate, HotQuote,
    HotTrade, Level, MarketState, MAX_BOOK_LEVELS,
};
pub use project::{project_event, ProjectError};
pub use types::{parse_fixed_price, parse_qty, CorrelationId, FixedPrice, InstrumentId, Qty};
