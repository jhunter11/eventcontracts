//! Cross-language contract primitives.
//!
//! At this layer Rust keeps externally visible decimals, timestamps, IDs, and
//! checksums as `String` so there is no silent rounding or timezone drift
//! between Python and Rust. Parsing into numeric / time types belongs in a
//! validated adapter (see e.g. the OMS, risk, and gateway crates).
//!
//! The public surface is:
//! - record types with `serde` derives that round-trip canonical JSON,
//! - a `validate()` method on every record that enforces the same invariants
//!   as the corresponding JSON Schema in `contracts/schemas/`,
//! - a `ContractError` enum that pinpoints the first failing field.

use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use thiserror::Error;

pub type Metadata = BTreeMap<String, String>;

// ---------- error ----------

#[derive(Debug, Error, PartialEq, Eq, Clone)]
pub enum ContractError {
    #[error("field `{0}` must be non-empty")]
    EmptyField(&'static str),
    #[error("field `{0}` must be a 64-character lowercase hex sha256")]
    InvalidSha256(&'static str),
    #[error("unsupported schema_version `{0}`")]
    UnsupportedSchema(String),
    #[error("invalid enum value `{value}` for field `{field}`")]
    InvalidEnum {
        field: &'static str,
        value: String,
    },
    #[error("field `{0}` must be a non-negative decimal string")]
    InvalidDecimal(&'static str),
    #[error("field `{0}` must be an RFC3339 UTC timestamp")]
    InvalidTimestamp(&'static str),
    #[error("json decode failed: {0}")]
    Json(String),
}

impl From<serde_json::Error> for ContractError {
    fn from(value: serde_json::Error) -> Self {
        ContractError::Json(value.to_string())
    }
}

// ---------- helpers ----------

pub fn require_non_empty(field: &'static str, value: &str) -> Result<(), ContractError> {
    if value.is_empty() {
        Err(ContractError::EmptyField(field))
    } else {
        Ok(())
    }
}

pub fn require_sha256(field: &'static str, value: &str) -> Result<(), ContractError> {
    if value.len() != 64
        || !value
            .chars()
            .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())
    {
        return Err(ContractError::InvalidSha256(field));
    }
    Ok(())
}

pub fn require_decimal_string(field: &'static str, value: &str) -> Result<(), ContractError> {
    if value.is_empty() {
        return Err(ContractError::EmptyField(field));
    }
    let bytes = value.as_bytes();
    let mut i = 0;
    if bytes[0] == b'-' {
        i = 1;
    }
    let mut saw_digit = false;
    let mut saw_dot = false;
    while i < bytes.len() {
        match bytes[i] {
            b'0'..=b'9' => saw_digit = true,
            b'.' if !saw_dot => saw_dot = true,
            _ => return Err(ContractError::InvalidDecimal(field)),
        }
        i += 1;
    }
    if !saw_digit {
        return Err(ContractError::InvalidDecimal(field));
    }
    Ok(())
}

/// Lightweight RFC3339 sanity check: requires `YYYY-MM-DDTHH:MM:SS` prefix and
/// a `Z`, `.fracZ`, or `±HH:MM` suffix. Full parsing happens in adapter crates.
pub fn require_rfc3339(field: &'static str, value: &str) -> Result<(), ContractError> {
    if value.len() < 20 {
        return Err(ContractError::InvalidTimestamp(field));
    }
    let bytes = value.as_bytes();
    let digits_ok = |range: std::ops::Range<usize>| {
        let mut r = range;
        r.all(|i| bytes[i].is_ascii_digit())
    };
    if !(digits_ok(0..4)
        && bytes[4] == b'-'
        && digits_ok(5..7)
        && bytes[7] == b'-'
        && digits_ok(8..10)
        && (bytes[10] == b'T' || bytes[10] == b' ')
        && digits_ok(11..13)
        && bytes[13] == b':'
        && digits_ok(14..16)
        && bytes[16] == b':'
        && digits_ok(17..19))
    {
        return Err(ContractError::InvalidTimestamp(field));
    }
    let suffix = &value[19..];
    let zulu = suffix == "Z" || (suffix.starts_with('.') && suffix.ends_with('Z'));
    let offset = suffix.len() >= 6
        && (suffix.starts_with('+') || suffix.starts_with('-'))
        && suffix.as_bytes()[3] == b':';
    if !(zulu || offset) {
        return Err(ContractError::InvalidTimestamp(field));
    }
    Ok(())
}

pub trait Contract {
    fn validate(&self) -> Result<(), ContractError>;
}

// ---------- audit ----------

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct AuditStamp {
    pub object_id: String,
    pub object_kind: String,
    pub schema_version: String,
    pub produced_at: String,
    pub producer: String,
    pub canonical_sha256: String,
    #[serde(default)]
    pub parent_ids: Vec<String>,
    #[serde(default)]
    pub trace_id: Option<String>,
    #[serde(default)]
    pub metadata: Metadata,
}

impl Contract for AuditStamp {
    fn validate(&self) -> Result<(), ContractError> {
        require_non_empty("object_id", &self.object_id)?;
        require_non_empty("object_kind", &self.object_kind)?;
        require_non_empty("schema_version", &self.schema_version)?;
        require_rfc3339("produced_at", &self.produced_at)?;
        require_non_empty("producer", &self.producer)?;
        require_sha256("canonical_sha256", &self.canonical_sha256)?;
        Ok(())
    }
}

// ---------- raw envelope ----------

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct RawEnvelope {
    #[serde(default)]
    pub venue: Option<String>,
    pub source: String,
    pub channel: String,
    pub received_at: String,
    #[serde(default)]
    pub exchange_ts: Option<String>,
    pub payload_json: String,
    pub schema_version: String,
    #[serde(default)]
    pub metadata: Metadata,
    pub audit: AuditStamp,
}

impl Contract for RawEnvelope {
    fn validate(&self) -> Result<(), ContractError> {
        require_non_empty("source", &self.source)?;
        require_non_empty("channel", &self.channel)?;
        require_rfc3339("received_at", &self.received_at)?;
        if let Some(ts) = &self.exchange_ts {
            require_rfc3339("exchange_ts", ts)?;
        }
        require_non_empty("payload_json", &self.payload_json)?;
        require_non_empty("schema_version", &self.schema_version)?;
        self.audit.validate()?;
        Ok(())
    }
}

// ---------- normalized event ----------

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct EventProvenance {
    pub source: String,
    pub channel: String,
    pub schema_version: String,
    #[serde(default)]
    pub venue: Option<String>,
    #[serde(default)]
    pub source_sequence: Option<String>,
    pub normalization_version: String,
    #[serde(default)]
    pub metadata: Metadata,
}

impl Contract for EventProvenance {
    fn validate(&self) -> Result<(), ContractError> {
        require_non_empty("source", &self.source)?;
        require_non_empty("channel", &self.channel)?;
        require_non_empty("schema_version", &self.schema_version)?;
        require_non_empty("normalization_version", &self.normalization_version)?;
        Ok(())
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct NormalizedEventRecord {
    pub event_id: String,
    pub event_kind: String,
    pub payload_json: String,
    pub provenance: EventProvenance,
    pub audit: AuditStamp,
}

impl Contract for NormalizedEventRecord {
    fn validate(&self) -> Result<(), ContractError> {
        require_non_empty("event_id", &self.event_id)?;
        require_non_empty("event_kind", &self.event_kind)?;
        require_non_empty("payload_json", &self.payload_json)?;
        match self.event_kind.as_str() {
            "quote" | "trade" | "book" | "lifecycle" | "settlement" | "external" | "timer"
            | "own_fill" | "own_order_update" | "own_order_reject" => {}
            other => {
                return Err(ContractError::InvalidEnum {
                    field: "event_kind",
                    value: other.to_string(),
                })
            }
        }
        self.provenance.validate()?;
        self.audit.validate()?;
        Ok(())
    }
}

// ---------- feature vector / prediction ----------

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct FeatureVectorRecord {
    pub schema_id: String,
    pub schema_version: String,
    #[serde(default)]
    pub instrument_id: Option<String>,
    pub timestamp: String,
    pub values: Vec<(String, String)>,
    pub audit: AuditStamp,
}

impl Contract for FeatureVectorRecord {
    fn validate(&self) -> Result<(), ContractError> {
        require_non_empty("schema_id", &self.schema_id)?;
        require_non_empty("schema_version", &self.schema_version)?;
        require_rfc3339("timestamp", &self.timestamp)?;
        for (name, value) in &self.values {
            require_non_empty("values.name", name)?;
            require_non_empty("values.value", value)?;
        }
        self.audit.validate()?;
        Ok(())
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct PredictionRecord {
    pub model_name: String,
    pub model_version: String,
    #[serde(default)]
    pub instrument_id: Option<String>,
    pub timestamp: String,
    pub horizon_seconds: u64,
    pub value: String,
    #[serde(default)]
    pub confidence: Option<String>,
    pub audit: AuditStamp,
}

impl Contract for PredictionRecord {
    fn validate(&self) -> Result<(), ContractError> {
        require_non_empty("model_name", &self.model_name)?;
        require_non_empty("model_version", &self.model_version)?;
        require_rfc3339("timestamp", &self.timestamp)?;
        require_decimal_string("value", &self.value)?;
        if let Some(c) = &self.confidence {
            require_decimal_string("confidence", c)?;
        }
        self.audit.validate()?;
        Ok(())
    }
}

// ---------- intent envelope ----------

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
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

impl Contract for IntentEnvelopeRecord {
    fn validate(&self) -> Result<(), ContractError> {
        require_non_empty("strategy_id", &self.strategy_id)?;
        require_non_empty("sleeve_id", &self.sleeve_id)?;
        require_non_empty("correlation_id", &self.correlation_id)?;
        require_rfc3339("emitted_at", &self.emitted_at)?;
        require_non_empty("decision_kind", &self.decision_kind)?;
        require_non_empty("decision_json", &self.decision_json)?;
        match self.priority_tier.as_str() {
            "fast" | "standard" | "relaxed" => {}
            other => {
                return Err(ContractError::InvalidEnum {
                    field: "priority_tier",
                    value: other.to_string(),
                })
            }
        }
        self.audit.validate()?;
        Ok(())
    }
}

// ---------- JSON parsing helpers ----------

pub fn from_json_str<T: for<'de> Deserialize<'de> + Contract>(s: &str) -> Result<T, ContractError> {
    let value: T = serde_json::from_str(s)?;
    value.validate()?;
    Ok(value)
}

pub fn to_json_string<T: Serialize>(value: &T) -> Result<String, ContractError> {
    Ok(serde_json::to_string(value)?)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn good_audit() -> AuditStamp {
        AuditStamp {
            object_id: "obj-1".into(),
            object_kind: "intent".into(),
            schema_version: "intent-envelope-v1".into(),
            produced_at: "2026-05-26T12:00:00Z".into(),
            producer: "runner".into(),
            canonical_sha256:
                "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef".into(),
            parent_ids: vec![],
            trace_id: None,
            metadata: Metadata::new(),
        }
    }

    #[test]
    fn require_sha256_accepts_lowercase_64_hex() {
        assert!(require_sha256("h", &"a".repeat(64)).is_ok());
    }

    #[test]
    fn require_sha256_rejects_uppercase_short_and_non_hex() {
        assert!(require_sha256("h", &"A".repeat(64)).is_err());
        assert!(require_sha256("h", "abc").is_err());
        assert!(require_sha256("h", &"g".repeat(64)).is_err());
    }

    #[test]
    fn require_decimal_string_accepts_valid_forms() {
        for v in ["0", "1", "100", "0.5", "-1.25", "12345.6789"] {
            require_decimal_string("v", v).unwrap_or_else(|e| panic!("{v} failed: {e}"));
        }
    }

    #[test]
    fn require_decimal_string_rejects_invalid_forms() {
        for v in ["", "abc", "1.2.3", "--1", ".", "1e5"] {
            assert!(require_decimal_string("v", v).is_err(), "{v} should fail");
        }
    }

    #[test]
    fn require_rfc3339_accepts_valid() {
        for v in [
            "2026-05-26T12:00:00Z",
            "2026-05-26T12:00:00.123Z",
            "2026-05-26T12:00:00+00:00",
            "2026-05-26T12:00:00-05:00",
        ] {
            require_rfc3339("t", v).unwrap_or_else(|e| panic!("{v} failed: {e}"));
        }
    }

    #[test]
    fn require_rfc3339_rejects_invalid() {
        for v in ["", "2026-05-26", "2026/05/26T12:00:00Z", "abc"] {
            assert!(require_rfc3339("t", v).is_err(), "{v} should fail");
        }
    }

    #[test]
    fn audit_validates_known_good() {
        good_audit().validate().unwrap();
    }

    #[test]
    fn audit_rejects_bad_sha256() {
        let mut a = good_audit();
        a.canonical_sha256 = "tooshort".into();
        assert_eq!(
            a.validate().unwrap_err(),
            ContractError::InvalidSha256("canonical_sha256")
        );
    }

    #[test]
    fn intent_envelope_json_roundtrip() {
        let intent = IntentEnvelopeRecord {
            strategy_id: "weather-temperature_arbitrage-v1".into(),
            sleeve_id: "weather-kalshi-paper-a".into(),
            correlation_id: "intent-001".into(),
            emitted_at: "2026-05-26T12:00:00Z".into(),
            decision_kind: "place_order".into(),
            decision_json: "{}".into(),
            priority_tier: "standard".into(),
            audit: good_audit(),
        };
        intent.validate().unwrap();
        let s = to_json_string(&intent).unwrap();
        let back: IntentEnvelopeRecord = from_json_str(&s).unwrap();
        assert_eq!(back, intent);
    }

    #[test]
    fn intent_envelope_rejects_unknown_priority_tier() {
        let mut intent = IntentEnvelopeRecord {
            strategy_id: "s".into(),
            sleeve_id: "sl".into(),
            correlation_id: "c".into(),
            emitted_at: "2026-05-26T12:00:00Z".into(),
            decision_kind: "place_order".into(),
            decision_json: "{}".into(),
            priority_tier: "ultra".into(),
            audit: good_audit(),
        };
        let err = intent.validate().unwrap_err();
        assert!(matches!(
            err,
            ContractError::InvalidEnum {
                field: "priority_tier",
                ..
            }
        ));
        intent.priority_tier = "fast".into();
        intent.validate().unwrap();
    }

    #[test]
    fn normalized_event_rejects_unknown_kind() {
        let ev = NormalizedEventRecord {
            event_id: "e".into(),
            event_kind: "halt".into(),
            payload_json: "{}".into(),
            provenance: EventProvenance {
                source: "kalshi-ws".into(),
                channel: "market_data".into(),
                schema_version: "normalized-event-v1".into(),
                venue: Some("kalshi".into()),
                source_sequence: Some("42".into()),
                normalization_version: "1".into(),
                metadata: Metadata::new(),
            },
            audit: good_audit(),
        };
        assert!(matches!(
            ev.validate().unwrap_err(),
            ContractError::InvalidEnum {
                field: "event_kind",
                ..
            }
        ));
    }

    #[test]
    fn raw_envelope_validates_and_roundtrips() {
        let raw = RawEnvelope {
            venue: Some("kalshi".into()),
            source: "kalshi-ws".into(),
            channel: "market_data".into(),
            received_at: "2026-05-26T12:00:00Z".into(),
            exchange_ts: Some("2026-05-26T11:59:59.500Z".into()),
            payload_json: "{}".into(),
            schema_version: "raw-event-v1".into(),
            metadata: Metadata::new(),
            audit: good_audit(),
        };
        raw.validate().unwrap();
        let s = to_json_string(&raw).unwrap();
        let back: RawEnvelope = from_json_str(&s).unwrap();
        assert_eq!(back, raw);
    }

    #[test]
    fn from_json_str_surfaces_missing_field() {
        let s = "{}";
        let err = from_json_str::<IntentEnvelopeRecord>(s).unwrap_err();
        assert!(matches!(err, ContractError::Json(_)));
    }
}
