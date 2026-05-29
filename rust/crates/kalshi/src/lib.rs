//! Kalshi adapter — REST + WS market data, plus the live `VenueClient`.
//!
//! Live-trading invariants:
//! - Order placement and cancellation go through [`KalshiVenueClient`], which
//!   implements `eventcontracts_gateway::VenueClient`. Callers must opt in
//!   explicitly — the read path (`KalshiRest::list_open_markets`, the WS
//!   subscriber) doesn't touch the order endpoints.
//! - All authenticated requests sign with RSA-PSS-SHA256, matching the
//!   Python adapter byte-for-byte: `f"{ts_ms}{METHOD}{path}"`.
//! - Public surface returns `NormalizedEventRecord` so callers stay on the
//!   cross-language contract.

pub mod auth;
pub mod normalize;
pub mod rest;
pub mod venue_client;
pub mod ws;

pub use auth::{KalshiAuth, KalshiEnv, KalshiEnvironment, SignError};
pub use normalize::{normalize_ws_payload, reset_sequence_tracking, NormalizeError};
pub use rest::{
    KalshiBalance, KalshiFill, KalshiOrder, KalshiPosition, KalshiRest, MarketRow,
    PlaceOrderRequest, PlaceOrderResponse, PositionsResponse, RestError,
};
pub use venue_client::KalshiVenueClient;
pub use ws::{KalshiWsClient, KalshiWsEnvelope, WsError};
