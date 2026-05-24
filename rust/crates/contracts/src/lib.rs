//! Cross-language contract primitives.
//!
//! Rust keeps externally visible decimals, timestamps, IDs, and checksums as
//! strings at this layer. Parsing into numeric/time types belongs in a later
//! validated adapter so there is no silent rounding or timezone drift.

use std::collections::BTreeMap;

pub type Metadata = BTreeMap<String, String>;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct AuditStamp {
    pub object_id: String,
    pub object_kind: String,
    pub schema_version: String,
    pub produced_at: String,
    pub producer: String,
    pub canonical_sha256: String,
    pub parent_ids: Vec<String>,
    pub trace_id: Option<String>,
    pub metadata: Metadata,
}

impl AuditStamp {
    pub fn validate(&self) -> Result<(), ContractError> {
        require_non_empty("object_id", &self.object_id)?;
        require_non_empty("object_kind", &self.object_kind)?;
        require_non_empty("schema_version", &self.schema_version)?;
        require_non_empty("produced_at", &self.produced_at)?;
        require_non_empty("producer", &self.producer)?;
        if self.canonical_sha256.len() != 64 {
            return Err(ContractError::InvalidSha256);
        }
        Ok(())
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RawEnvelope {
    pub venue: Option<String>,
    pub source: String,
    pub channel: String,
    pub received_at: String,
    pub exchange_ts: Option<String>,
    pub payload_json: String,
    pub schema_version: String,
    pub metadata: Metadata,
    pub audit: AuditStamp,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct EventProvenance {
    pub source: String,
    pub channel: String,
    pub schema_version: String,
    pub venue: Option<String>,
    pub source_sequence: Option<String>,
    pub normalization_version: String,
    pub metadata: Metadata,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct NormalizedEventRecord {
    pub event_id: String,
    pub event_kind: String,
    pub payload_json: String,
    pub provenance: EventProvenance,
    pub audit: AuditStamp,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct FeatureVectorRecord {
    pub schema_id: String,
    pub schema_version: String,
    pub instrument_id: Option<String>,
    pub timestamp: String,
    pub values: Vec<(String, String)>,
    pub audit: AuditStamp,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PredictionRecord {
    pub model_name: String,
    pub model_version: String,
    pub instrument_id: Option<String>,
    pub timestamp: String,
    pub horizon_seconds: u64,
    pub value: String,
    pub confidence: Option<String>,
    pub audit: AuditStamp,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct IntentEnvelopeRecord {
    pub strategy_id: String,
    pub sleeve_id: String,
    pub correlation_id: String,
    pub emitted_at: String,
    pub decision_kind: String,
    pub decision_json: String,
    pub priority_tier: String,
    pub audit: AuditStamp,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ContractError {
    EmptyField(&'static str),
    InvalidSha256,
    UnsupportedSchema(String),
    ParseDeferred(String),
}

pub fn require_non_empty(field: &'static str, value: &str) -> Result<(), ContractError> {
    if value.is_empty() {
        return Err(ContractError::EmptyField(field));
    }
    Ok(())
}

pub trait ContractValidator<T> {
    fn validate(&self, value: &T) -> Result<(), ContractError>;
}
