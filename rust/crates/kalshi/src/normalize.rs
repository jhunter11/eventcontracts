//! Raw Kalshi WS message → `NormalizedEventRecord`.
//!
//! The mapping mirrors the Python normalizer: `ticker` → `quote`,
//! `trade` → `trade`, `orderbook_snapshot` / `orderbook_delta` → `book`,
//! `lifecycle` → `lifecycle`. Anything we don't model is returned as a
//! `NormalizeError::Ignored` so the runner can count it without failing.

use crate::ws::KalshiWsEnvelope;
use eventcontracts_contracts::{
    canonical_sha256, AuditStamp, ContractError, EventProvenance, Metadata, NormalizedEventRecord,
};
use parking_lot::Mutex;
use serde::{Deserialize, Deserializer, Serialize};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::fmt;
use std::sync::OnceLock;
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
    #[error("json error: {0}")]
    Json(#[from] serde_json::Error),
    #[error("contract error: {0}")]
    Contract(#[from] ContractError),
    #[error("sequence gap on channel `{channel}` sid {sid}: expected {expected}, got {got}")]
    SequenceGap {
        channel: String,
        sid: i64,
        expected: i64,
        got: i64,
    },
    #[error("ignored: {0}")]
    Ignored(&'static str),
}

pub fn normalize_ws_payload(
    env: &KalshiWsEnvelope,
    received_at: OffsetDateTime,
) -> Result<NormalizedEventRecord, NormalizeError> {
    check_sequence(env)?;
    let kind = match env.msg_type.as_str() {
        "ticker" => "quote",
        "trade" => "trade",
        "orderbook_snapshot" | "orderbook_delta" => "book",
        // Kalshi's lifecycle channel is `market_lifecycle_v2`; legacy
        // `lifecycle` is preserved for fixture-style payloads.
        "market_lifecycle_v2" | "lifecycle" => "lifecycle",
        "fill" => "own_fill",
        "order" | "order_update" => "own_order_update",
        "subscribed" | "ok" | "error" => return Err(NormalizeError::Ignored("control msg")),
        other => return Err(NormalizeError::UnsupportedChannel(other.to_string())),
    };

    let received_iso = received_at
        .format(&Rfc3339)
        .unwrap_or_else(|_| "1970-01-01T00:00:00Z".into());

    let mut payload_json = build_payload_json(env, kind)?;
    let mut event_kind = kind.to_string();
    if kind == "own_order_update" {
        let mut payload: Value = serde_json::from_str(&payload_json)?;
        if let Some(override_kind) = payload
            .get("event_kind_override")
            .and_then(Value::as_str)
            .map(str::to_string)
        {
            event_kind = override_kind;
        }
        if let Some(obj) = payload.as_object_mut() {
            obj.remove("event_kind_override");
        }
        payload_json = payload.to_string();
    }

    let provenance = EventProvenance {
        source: "kalshi-ws".into(),
        channel: env.msg_type.clone(),
        schema_version: NORMALIZATION_VERSION.into(),
        venue: Some("kalshi".into()),
        source_sequence: env.seq.map(|s| s.to_string()),
        normalization_version: NORMALIZATION_VERSION.into(),
        metadata: Metadata::new(),
    };

    let event_id = synth_event_id(env, &event_kind);
    let digest = canonical_sha256(&json!({
        "event_id": event_id,
        "event_kind": event_kind,
        "payload_json": payload_json,
        "provenance": provenance,
    }))?;

    let audit = AuditStamp {
        object_id: event_id.clone(),
        object_kind: "normalized_event".into(),
        schema_version: "normalized-event-v1".into(),
        produced_at: received_iso,
        producer: "kalshi-ws-normalizer".into(),
        canonical_sha256: digest,
        parent_ids: vec![],
        trace_id: None,
        metadata: Metadata::new(),
    };

    Ok(NormalizedEventRecord {
        event_id,
        event_kind,
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

fn check_sequence(env: &KalshiWsEnvelope) -> Result<(), NormalizeError> {
    let (Some(sid), Some(seq)) = (env.sid, env.seq) else {
        return Ok(());
    };
    let key = (sid, env.msg_type.clone());
    // parking_lot::Mutex — faster than std and never poisons.
    let mut seen = sequence_state().lock();
    if let Some(last) = seen.insert(key, seq) {
        let expected = last + 1;
        if seq != expected {
            return Err(NormalizeError::SequenceGap {
                channel: env.msg_type.clone(),
                sid,
                expected,
                got: seq,
            });
        }
    }
    Ok(())
}

fn sequence_state() -> &'static Mutex<HashMap<(i64, String), i64>> {
    static STATE: OnceLock<Mutex<HashMap<(i64, String), i64>>> = OnceLock::new();
    STATE.get_or_init(|| Mutex::new(HashMap::new()))
}

pub fn reset_sequence_tracking() {
    sequence_state().lock().clear();
}

fn build_payload_json(env: &KalshiWsEnvelope, kind: &str) -> Result<String, NormalizeError> {
    let msg = env
        .msg
        .as_deref()
        .ok_or(NormalizeError::MissingField("msg", "envelope"))?
        .get();
    match kind {
        "quote" => {
            // Kalshi ticker msg has yes_bid/yes_ask/no_bid/no_ask in cents.
            let parsed: TickerMsg = serde_json::from_str(msg)?;
            let yes_bid = first_decimal(&[parsed.yes_bid_dollars, parsed.yes_bid]);
            let yes_ask = first_decimal(&[parsed.yes_ask_dollars, parsed.yes_ask]);
            let instrument = parsed
                .market_ticker
                .ok_or(NormalizeError::MissingField("ticker", "quote"))?;
            Ok(serde_json::to_string(&QuotePayload {
                instrument: format!("kalshi:{instrument}"),
                bid: yes_bid,
                ask: yes_ask,
            })?)
        }
        "trade" => {
            let parsed: TradeMsg = serde_json::from_str(msg)?;
            let instrument = parsed
                .market_ticker
                .ok_or(NormalizeError::MissingField("ticker", "trade"))?;
            let yes_price = first_decimal(&[parsed.yes_price_dollars, parsed.yes_price]);
            let count = first_decimal(&[parsed.count_fp, parsed.count, parsed.quantity]);
            Ok(serde_json::to_string(&TradePayload {
                instrument: format!("kalshi:{instrument}"),
                price: yes_price,
                size: count,
            })?)
        }
        "book" => {
            let parsed: TickerKeyMsg = serde_json::from_str(msg)?;
            let instrument = parsed
                .market_ticker
                .ok_or(NormalizeError::MissingField("ticker", "book"))?;
            let raw: Value = serde_json::from_str(msg)?;
            Ok(json!({
                "instrument": format!("kalshi:{instrument}"),
                "raw": raw,
            })
            .to_string())
        }
        "lifecycle" => {
            let raw: Value = serde_json::from_str(msg)?;
            // Hoist the instrument and the canonical Kalshi `event_type`
            // out of the raw envelope so the hot-path projection can produce
            // a typed `HotEvent::MarketState` without re-parsing the raw
            // blob. The raw block stays for downstream auditors.
            let instrument = instrument_from_value(&raw, "lifecycle")
                .map_err(|_| NormalizeError::MissingField("ticker", "lifecycle"))?;
            let event_type = value_text_any(&raw, &["event_type", "status"]);
            let reason = value_text_any(&raw, &["reason"]);
            Ok(json!({
                "instrument": instrument,
                "kalshi_event_type": event_type,
                "reason": reason,
                "raw": raw,
            })
            .to_string())
        }
        "own_fill" => {
            let raw: Value = serde_json::from_str(msg)?;
            let instrument = instrument_from_value(&raw, "own_fill")?;
            let fill_id = value_text_any(&raw, &["fill_id", "trade_id", "id"])
                .ok_or(NormalizeError::MissingField("fill_id", "own_fill"))?;
            let client_order_id =
                value_text_any(&raw, &["client_order_id", "client_id", "order_client_id"])
                    .or_else(|| nested_value_text_any(&raw, "order", &["client_order_id"]))
                    .ok_or(NormalizeError::MissingField("client_order_id", "own_fill"))?;
            let price = value_text_any(
                &raw,
                &[
                    "yes_price_dollars",
                    "price_dollars",
                    "fill_price_dollars",
                    "price",
                    "yes_price",
                ],
            )
            .unwrap_or_default();
            let quantity = value_text_any(&raw, &["quantity", "count", "count_fp", "size"])
                .unwrap_or_default();
            let fee = value_text_any(&raw, &["fee_dollars", "fee"]).unwrap_or_else(|| "0".into());
            Ok(json!({
                "fill_id": fill_id,
                "client_order_id": client_order_id,
                "instrument": instrument,
                "price": price,
                "quantity": quantity,
                "fee": fee,
            })
            .to_string())
        }
        "own_order_update" => {
            let raw: Value = serde_json::from_str(msg)?;
            let instrument = instrument_from_value(&raw, "own_order_update").unwrap_or_default();
            let client_order_id =
                value_text_any(&raw, &["client_order_id", "client_id", "order_client_id"])
                    .or_else(|| nested_value_text_any(&raw, "order", &["client_order_id"]))
                    .ok_or(NormalizeError::MissingField(
                        "client_order_id",
                        "own_order_update",
                    ))?;
            let state =
                value_text_any(&raw, &["state", "status", "order_status"]).unwrap_or_default();
            let reason = value_text_any(&raw, &["reason", "reject_reason", "error"]);
            let event_kind = if state.eq_ignore_ascii_case("rejected") {
                "own_order_reject"
            } else {
                kind
            };
            Ok(json!({
                "event_kind_override": event_kind,
                "client_order_id": client_order_id,
                "instrument": instrument,
                "state": state,
                "reason": reason,
            })
            .to_string())
        }
        _ => Ok("{}".to_string()),
    }
}

#[derive(Debug, Clone)]
struct DecimalText(String);

impl<'de> Deserialize<'de> for DecimalText {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        struct Visitor;

        impl<'de> serde::de::Visitor<'de> for Visitor {
            type Value = DecimalText;

            fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
                formatter.write_str("a decimal string or JSON number")
            }

            fn visit_str<E>(self, value: &str) -> Result<Self::Value, E>
            where
                E: serde::de::Error,
            {
                Ok(DecimalText(value.to_string()))
            }

            fn visit_string<E>(self, value: String) -> Result<Self::Value, E>
            where
                E: serde::de::Error,
            {
                Ok(DecimalText(value))
            }

            fn visit_i64<E>(self, value: i64) -> Result<Self::Value, E>
            where
                E: serde::de::Error,
            {
                Ok(DecimalText(value.to_string()))
            }

            fn visit_u64<E>(self, value: u64) -> Result<Self::Value, E>
            where
                E: serde::de::Error,
            {
                Ok(DecimalText(value.to_string()))
            }

            fn visit_f64<E>(self, value: f64) -> Result<Self::Value, E>
            where
                E: serde::de::Error,
            {
                Ok(DecimalText(value.to_string()))
            }
        }

        deserializer.deserialize_any(Visitor)
    }
}

#[derive(Debug, Deserialize)]
struct TickerKeyMsg {
    #[serde(default, alias = "ticker")]
    market_ticker: Option<String>,
}

#[derive(Debug, Deserialize)]
struct TickerMsg {
    #[serde(default, alias = "ticker")]
    market_ticker: Option<String>,
    #[serde(default)]
    yes_bid_dollars: Option<DecimalText>,
    #[serde(default)]
    yes_bid: Option<DecimalText>,
    #[serde(default)]
    yes_ask_dollars: Option<DecimalText>,
    #[serde(default)]
    yes_ask: Option<DecimalText>,
}

#[derive(Debug, Deserialize)]
struct TradeMsg {
    #[serde(default, alias = "ticker")]
    market_ticker: Option<String>,
    #[serde(default)]
    yes_price_dollars: Option<DecimalText>,
    #[serde(default)]
    yes_price: Option<DecimalText>,
    #[serde(default)]
    count_fp: Option<DecimalText>,
    #[serde(default)]
    count: Option<DecimalText>,
    #[serde(default)]
    quantity: Option<DecimalText>,
}

#[derive(Debug, Serialize)]
struct QuotePayload {
    instrument: String,
    bid: String,
    ask: String,
}

#[derive(Debug, Serialize)]
struct TradePayload {
    instrument: String,
    price: String,
    size: String,
}

fn first_decimal(values: &[Option<DecimalText>]) -> String {
    values
        .iter()
        .find_map(|value| value.as_ref().map(|text| text.0.clone()))
        .unwrap_or_default()
}

fn instrument_from_value(raw: &Value, context: &'static str) -> Result<String, NormalizeError> {
    let raw_ticker = value_text_any(
        raw,
        &["market_ticker", "ticker", "instrument", "instrument_id"],
    )
    .or_else(|| nested_value_text_any(raw, "order", &["market_ticker", "ticker"]));
    let ticker = raw_ticker.ok_or(NormalizeError::MissingField("ticker", context))?;
    if ticker.starts_with("kalshi:") {
        Ok(ticker)
    } else {
        Ok(format!("kalshi:{ticker}"))
    }
}

fn nested_value_text_any(raw: &Value, object_key: &str, keys: &[&str]) -> Option<String> {
    raw.get(object_key)
        .and_then(|value| value_text_any(value, keys))
}

fn value_text_any(raw: &Value, keys: &[&str]) -> Option<String> {
    for key in keys {
        if let Some(value) = raw.get(*key).and_then(value_to_text) {
            return Some(value);
        }
    }
    None
}

fn value_to_text(value: &Value) -> Option<String> {
    match value {
        Value::Null => None,
        Value::String(s) if !s.is_empty() => Some(s.clone()),
        Value::Number(n) => Some(n.to_string()),
        Value::Bool(b) => Some(b.to_string()),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use eventcontracts_contracts::Contract;
    use serde_json::value::RawValue;

    fn ts() -> OffsetDateTime {
        OffsetDateTime::from_unix_timestamp(1_700_000_000).unwrap()
    }

    fn raw_msg(value: Value) -> Option<Box<RawValue>> {
        Some(RawValue::from_string(value.to_string()).unwrap())
    }

    #[test]
    fn ticker_msg_becomes_quote_event() {
        let env = KalshiWsEnvelope {
            msg_type: "ticker".into(),
            sid: Some(1),
            seq: Some(42),
            msg: raw_msg(json!({
                "market_ticker": "KXHIGHNY-26MAY27-T68",
                "yes_bid_dollars": "0.42",
                "yes_ask_dollars": "0.44",
            })),
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
            msg: raw_msg(json!({
                "market_ticker": "KXHIGHNY-26MAY27-T68",
                "yes_price_dollars": "0.43",
                "count": 5,
                "taker_side": "yes",
            })),
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
                msg: raw_msg(json!({})),
                id: Some(1),
            };
            let err = normalize_ws_payload(&env, ts()).unwrap_err();
            matches!(err, NormalizeError::Ignored(_));
        }
    }

    #[test]
    fn market_lifecycle_v2_msg_becomes_lifecycle_event() {
        let env = KalshiWsEnvelope {
            msg_type: "market_lifecycle_v2".into(),
            sid: Some(13),
            seq: Some(1),
            msg: raw_msg(json!({
                "market_ticker": "KXHIGHNY-26MAY24-B75",
                "event_type": "deactivated",
                "open_ts": 1_779_620_000_i64,
                "close_ts": 1_779_667_140_i64,
            })),
            id: None,
        };
        let ev = normalize_ws_payload(&env, ts()).unwrap();
        assert_eq!(ev.event_kind, "lifecycle");
        let payload: Value = serde_json::from_str(&ev.payload_json).unwrap();
        assert_eq!(payload["instrument"], "kalshi:KXHIGHNY-26MAY24-B75");
        assert_eq!(payload["kalshi_event_type"], "deactivated");
        assert!(payload.get("raw").is_some());
        ev.validate().unwrap();
    }

    #[test]
    fn unknown_channels_produce_unsupported_error() {
        let env = KalshiWsEnvelope {
            msg_type: "halt".into(),
            sid: None,
            seq: None,
            msg: raw_msg(json!({})),
            id: None,
        };
        let err = normalize_ws_payload(&env, ts()).unwrap_err();
        assert!(matches!(err, NormalizeError::UnsupportedChannel(_)));
    }
}
