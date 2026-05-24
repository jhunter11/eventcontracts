//! Venue-facing gateway boundary.

use eventcontracts_contracts::{AuditStamp, IntentEnvelopeRecord};

#[derive(Clone, Debug)]
pub struct GatewayCommand {
    pub kind: String,
    pub venue: String,
    pub correlation_id: String,
    pub intent: IntentEnvelopeRecord,
    pub audit: AuditStamp,
}

#[derive(Clone, Debug)]
pub struct GatewayAck {
    pub command_id: String,
    pub accepted: bool,
    pub acknowledged_at: String,
    pub venue_order_id: Option<String>,
    pub reasons: Vec<String>,
    pub audit: AuditStamp,
}

#[derive(Clone, Debug)]
pub struct RateLimitBudget {
    pub venue: String,
    pub priority_tier: String,
    pub requests_remaining: u64,
    pub reset_at: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum GatewayError {
    Idempotency(String),
    RateLimited(String),
    VenueReject(String),
    Transport(String),
}

pub trait PriorityScheduler {
    fn enqueue(&mut self, envelope: IntentEnvelopeRecord) -> Result<(), GatewayError>;
    fn next_batch(
        &mut self,
        now: &str,
        limit: usize,
    ) -> Result<Vec<IntentEnvelopeRecord>, GatewayError>;
    fn drop_stale(&mut self, now: &str) -> Result<Vec<IntentEnvelopeRecord>, GatewayError>;
}

pub trait IdempotencyStore {
    fn reserve(&mut self, key: &str, correlation_id: &str) -> Result<bool, GatewayError>;
    fn mark_complete(&mut self, key: &str, ack: GatewayAck) -> Result<(), GatewayError>;
}

pub trait VenueGateway {
    fn submit(&mut self, command: GatewayCommand) -> Result<GatewayAck, GatewayError>;
    fn cancel(&mut self, command: GatewayCommand) -> Result<GatewayAck, GatewayError>;
    fn replace(&mut self, command: GatewayCommand) -> Result<GatewayAck, GatewayError>;
}
