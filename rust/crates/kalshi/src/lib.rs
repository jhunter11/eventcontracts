//! Kalshi adapter — REST + WS read path, raw → NormalizedEventRecord mapping.
//!
//! Live-trading invariants this crate honors:
//! - No submit/cancel/replace endpoints. Live order placement lives in a
//!   future `VenueClient` impl that explicitly takes credentials and is
//!   exposed behind a feature flag.
//! - All authenticated requests sign with RSA-PSS-SHA256, matching the
//!   Python adapter byte-for-byte: `f"{ts_ms}{METHOD}{path}"`.
//! - Public surface returns `NormalizedEventRecord` so callers stay on the
//!   cross-language contract.

pub mod auth;
pub mod normalize;
pub mod rest;
pub mod ws;

pub use auth::{KalshiAuth, KalshiEnv, KalshiEnvironment, SignError};
pub use normalize::{normalize_ws_payload, NormalizeError};
pub use rest::{KalshiRest, MarketRow, RestError};
pub use ws::{KalshiWsClient, KalshiWsEnvelope, WsError};
