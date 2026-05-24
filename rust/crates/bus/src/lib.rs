//! IPC topic and message boundary.

use eventcontracts_contracts::{AuditStamp, IntentEnvelopeRecord, NormalizedEventRecord};
use std::collections::BTreeMap;

#[derive(Clone, Debug)]
pub struct TopicSpec {
    pub name: String,
    pub schema_version: String,
    pub description: String,
}

#[derive(Clone, Debug)]
pub struct BusMessage {
    pub topic: String,
    pub key: String,
    pub payload: Vec<u8>,
    pub schema_version: String,
    pub published_at: String,
    pub audit: AuditStamp,
    pub headers: BTreeMap<String, String>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum BusError {
    Encode(String),
    Decode(String),
    Publish(String),
    Subscribe(String),
}

pub trait MessageCodec {
    fn encode_event(&self, event: &NormalizedEventRecord, topic: &TopicSpec)
        -> Result<BusMessage, BusError>;
    fn encode_intent(
        &self,
        envelope: &IntentEnvelopeRecord,
        topic: &TopicSpec,
    ) -> Result<BusMessage, BusError>;
}

pub trait BusPublisher {
    fn publish(&self, message: BusMessage) -> Result<(), BusError>;
}

pub trait BusSubscriber {
    fn next(&mut self) -> Result<Option<BusMessage>, BusError>;
    fn ack(&self, message: &BusMessage) -> Result<(), BusError>;
    fn nack(&self, message: &BusMessage, reason: &str) -> Result<(), BusError>;
}
