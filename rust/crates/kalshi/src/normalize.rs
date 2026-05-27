//! Raw Kalshi WS message → `NormalizedEventRecord`.
//!
//! The mapping mirrors the Python normalizer: `ticker` → `quote`,
//! `trade` → `trade`, `orderbook_snapshot` / `orderbook_delta` → `book`,
//! `lifecycle` → `lifecycle`. Anything we don't model is returned as a
//! `NormalizeError::Ignored` so the runner can count it without failing.

use crate::ws::KalshiWsEnvelope;
use eventcontracts_contracts::{AuditStamp, EventProvenance, Metadata, NormalizedEventRecord};
use serde_json::{json, Value};
use thiserror::Error;
use time::format_description::well_known::Rfc3339;
use time::OffsetDateTime;

const NORMALIZATION_VERSION: &str = "kalshi-ws-v1";

#[derive(Debug, Error)]
pub enum NormalizeError {
    #[error("unsupported channel `{0}`")]
    UnsupportedChannel(String),
    #[error("missing field `{0}` in {1}")]
    MissingField(&'static str, &'static str),
    #[error("ignored: {0}")]
    Ignored(&'static str),
}

pub fn normalize_ws_payload(
    env: &KalshiWsEnvelope,
    received_at: OffsetDateTime,
) -> Result<NormalizedEventRecord, NormalizeError> {
    let kind = match env.msg_type.as_str() {
        "ticker" => "quote",
        "trade" => "trade",
        "orderbook_snapshot" | "orderbook_delta" => "book",
        "lifecycle" => "lifecycle",
        "subscribed" | "ok" | "error" => return Err(NormalizeError::Ignored("control msg")),
        other => return Err(NormalizeError::UnsupportedChannel(other.to_string())),
    };

    let received_iso = received_at
        .format(&Rfc3339)
        .unwrap_or_else(|_| "1970-01-01T00:00:00Z".into());

    let payload = build_payload(env, kind)?;
    let payload_json = serde_json::to_string(&payload)
        .unwrap_or_else(|_| "{}".to_string());

    let provenance = EventProvenance {
        source: "kalshi-ws".into(),
        channel: env.msg_type.clone(),
        schema_version: NORMALIZATION_VERSION.into(),
        venue: Some("kalshi".into()),
        source_sequence: env.seq.map(|s| s.to_string()),
        normalization_version: NORMALIZATION_VERSION.into(),
        metadata: Metadata::new(),
    };

    let audit = AuditStamp {
        object_id: synth_event_id(env, kind),
        object_kind: "normalized_event".into(),
        schema_version: "normalized-event-v1".into(),
        produced_at: received_iso,
        producer: "kalshi-ws-normalizer".into(),
        canonical_sha256: "0".repeat(64),
        parent_ids: vec![],
        trace_id: None,
        metadata: Metadata::new(),
    };

    let event_id = audit.object_id.clone();
    Ok(NormalizedEventRecord {
        event_id,
        event_kind: kind.to_string(),
        payload_json,
        provenance,
        audit,
    })
}

fn synth_event_id(env: &KalshiWsEnvelope, kind: &str) -> String {
    match (env.sid, env.seq) {
        (Some(sid), Some(seq)) => format!("kalshi:{kind}:{sid}:{seq}"),
        (None, Some(seq)) => format!("kalshi:{kind}::{seq}"),
        _ => format!("kalshi:{kind}:anon:{}", anon_counter()),
    }
}

fn anon_counter() -> u64 {
    static COUNTER: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
    COUNTER.fetch_add(1, std::sync::atomic::Ordering::Relaxed)
}

fn build_payload(env: &KalshiWsEnvelope, kind: &str) -> Result<Value, NormalizeError> {
    let msg = &env.msg;
    match kind {
        "quote" => {
            // Kalshi ticker msg has yes_bid/yes_ask/no_bid/no_ask in cents.
            let yes_bid = decimal_field(msg, &["yes_bid_dollars", "yes_bid"]).unwrap_or_default();
            let yes_ask = decimal_field(msg, &["yes_ask_dollars", "yes_ask"]).unwrap_or_default();
            let instrument = string_field(msg, &["market_ticker", "ticker"])
                .ok_or(NormalizeError::MissingField("ticker", "quote"))?;
            Ok(json!({
                "instrument": format!("kalshi:{instrument}"),
                "bid": yes_bid,
                "ask": yes_ask,
            }))
        }
        "trade" => {
            let instrument = string_field(msg, &["market_ticker", "ticker"])
                .ok_or(NormalizeError::MissingField("ticker", "trade"))?;
            let yes_price = decimal_field(msg, &["yes_price_dollars", "yes_price"]).unwrap_or_default();
            let count = decimal_field(msg, &["count_fp", "count", "quantity"]).unwrap_or_default();
            Ok(json!({
                "instrument": format!("kalshi:{instrument}"),
                "price": yes_price,
                "size": count,
            }))
        }
        "book" => {
            let instrument = string_field(msg, &["market_ticker", "ticker"])
                .ok_or(NormalizeError::MissingField("ticker", "book"))?;
            Ok(json!({
                "instrument": format!("kalshi:{instrument}"),
                "raw": msg.clone(),
            }))
        }
        "lifecycle" => Ok(json!({"raw": msg.clone()})),
        _ => Ok(json!({})),
    }
}

fn string_field(value: &Value, keys: &[&str]) -> Option<String> {
    for k in keys {
        if let Some(v) = value.get(*k).and_then(|x| x.as_str()) {
            return Some(v.to_string());
        }
    }
    None
}

fn decimal_field(value: &Value, keys: &[&str]) -> Option<String> {
    for k in keys {
        if let Some(v) = value.get(*k) {
            if let Some(s) = v.as_str() {
                return Some(s.to_string());
            }
            if let Some(n) = v.as_f64() {
                return Some(format!("{n}"));
            }
            if let Some(n) = v.as_i64() {
                return Some(format!("{n}"));
            }
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;
    use eventcontracts_contracts::Contract;

    fn ts() -> OffsetDateTime {
        OffsetDateTime::from_unix_timestamp(1_700_000_000).unwrap()
    }

    #[test]
    fn ticker_msg_becomes_quote_event() {
        let env = KalshiWsEnvelope {
            msg_type: "ticker".into(),
            sid: Some(1),
            seq: Some(42),
            msg: json!({
                "market_ticker": "KXHIGHNY-26MAY27-T68",
                "yes_bid_dollars": "0.42",
                "yes_ask_dollars": "0.44",
            }),
            id: None,
        };
        let ev = normalize_ws_payload(&env, ts()).unwrap();
        assert_eq!(ev.event_kind, "quote");
        assert_eq!(ev.provenance.source_sequence.as_deref(), Some("42"));
        let payload: Value = serde_json::from_str(&ev.payload_json).unwrap();
        assert_eq!(payload["bid"], "0.42");
        assert_eq!(payload["ask"], "0.44");
        ev.validate().unwrap();
    }

    #[test]
    fn trade_msg_becomes_trade_event() {
        let env = KalshiWsEnvelope {
            msg_type: "trade".into(),
            sid: Some(1),
            seq: Some(43),
            msg: json!({
                "market_ticker": "KXHIGHNY-26MAY27-T68",
                "yes_price_dollars": "0.43",
                "count": 5,
                "taker_side": "yes",
            }),
            id: None,
        };
        let ev = normalize_ws_payload(&env, ts()).unwrap();
        assert_eq!(ev.event_kind, "trade");
        ev.validate().unwrap();
    }

    #[test]
    fn control_messages_are_explicitly_ignored() {
        for t in ["subscribed", "ok", "error"] {
            let env = KalshiWsEnvelope {
                msg_type: t.into(),
                sid: None,
                seq: None,
                msg: json!({}),
                id: Some(1),
            };
            let err = normalize_ws_payload(&env, ts()).unwrap_err();
            matches!(err, NormalizeError::Ignored(_));
        }
    }

    #[test]
    fn unknown_channels_produce_unsupported_error() {
        let env = KalshiWsEnvelope {
            msg_type: "halt".into(),
            sid: None,
            seq: None,
            msg: json!({}),
            id: None,
        };
        let err = normalize_ws_payload(&env, ts()).unwrap_err();
        assert!(matches!(err, NormalizeError::UnsupportedChannel(_)));
    }
}
