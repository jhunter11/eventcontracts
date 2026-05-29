//! Project a `NormalizedEventRecord` into a `HotEvent`.
//!
//! Runs exactly once per envelope, at the bus subscriber. After this point
//! the strategy / CEG / arb engine never touches JSON again.

use serde::Deserialize;
use serde_json::Value;
use smol_str::SmolStr;
use thiserror::Error;

use eventcontracts_contracts::NormalizedEventRecord;

use crate::event::{
    HotBook, HotEvent, HotEventKind, HotMarketState, HotOwnFill, HotOwnOrderUpdate, HotQuote,
    HotTrade, Level, MarketState, MAX_BOOK_LEVELS,
};
use crate::types::{parse_fixed_price, parse_qty, FixedPrice, ParseError, Qty};

#[derive(Debug, Error)]
pub enum ProjectError {
    #[error("unknown event_kind `{0}`")]
    UnknownKind(String),
    #[error("payload_json decode failed: {0}")]
    Json(#[from] serde_json::Error),
    #[error("missing field `{0}`")]
    MissingField(&'static str),
    #[error("numeric parse error in field `{field}`: {source}")]
    Parse {
        field: &'static str,
        #[source]
        source: ParseError,
    },
}

fn parse_price_field(field: &'static str, raw: &str) -> Result<FixedPrice, ProjectError> {
    parse_fixed_price(raw).map_err(|source| ProjectError::Parse { field, source })
}

fn parse_qty_field(field: &'static str, raw: &str) -> Result<Qty, ProjectError> {
    parse_qty(raw).map_err(|source| ProjectError::Parse { field, source })
}

#[derive(Debug, Deserialize)]
struct QuotePayload {
    instrument: String,
    #[serde(default)]
    bid: String,
    #[serde(default)]
    ask: String,
}

#[derive(Debug, Deserialize)]
struct TradePayload {
    instrument: String,
    #[serde(default)]
    price: String,
    #[serde(default)]
    size: String,
}

#[derive(Debug, Deserialize)]
struct BookPayload {
    instrument: String,
    /// The verbatim Kalshi snapshot/delta. Shape varies — we parse it
    /// best-effort below.
    #[serde(default)]
    raw: Value,
}

#[derive(Debug, Deserialize)]
struct OwnFillPayload {
    #[serde(default)]
    fill_id: String,
    #[serde(default, alias = "instrument_id")]
    instrument: String,
    #[serde(default)]
    client_order_id: String,
    #[serde(default)]
    price: String,
    #[serde(default, alias = "size")]
    quantity: String,
    #[serde(default, alias = "remaining")]
    remaining_quantity: String,
    #[serde(default)]
    fee: String,
}

#[derive(Debug, Deserialize)]
struct OwnOrderUpdatePayload {
    #[serde(default)]
    client_order_id: String,
    #[serde(default, alias = "instrument_id")]
    instrument: String,
    #[serde(default)]
    state: String,
    #[serde(default)]
    reason: Option<String>,
}

#[derive(Debug, Deserialize)]
struct LifecyclePayload {
    /// Normalizers may put the canonical instrument id at the top level
    /// (`instrument`/`instrument_id`) or carry the raw Kalshi envelope under
    /// `raw`. We accept both shapes so the projection survives normalizer
    /// changes.
    #[serde(default, alias = "instrument_id")]
    instrument: Option<String>,
    #[serde(default)]
    kalshi_event_type: Option<String>,
    #[serde(default)]
    state: Option<String>,
    #[serde(default)]
    reason: Option<String>,
    #[serde(default)]
    raw: Option<serde_json::Value>,
}

/// Project a normalized event into the hot-path representation.
pub fn project_event(record: &NormalizedEventRecord) -> Result<HotEvent, ProjectError> {
    match record.event_kind.as_str() {
        "quote" => project_quote(&record.payload_json),
        "trade" => project_trade(&record.payload_json),
        "book" => project_book(&record.payload_json),
        "own_fill" => project_own_fill(&record.payload_json),
        "own_order_update" => {
            project_own_order_update(&record.payload_json, /* reject = */ false)
        }
        "own_order_reject" => {
            project_own_order_update(&record.payload_json, /* reject = */ true)
        }
        "lifecycle" => project_lifecycle(&record.payload_json),
        "settlement" => Ok(passthrough(HotEventKind::Settlement, &record.payload_json)),
        "external" => Ok(passthrough(HotEventKind::External, &record.payload_json)),
        "timer" => Ok(passthrough(HotEventKind::Timer, &record.payload_json)),
        other => Err(ProjectError::UnknownKind(other.to_string())),
    }
}

fn project_quote(payload: &str) -> Result<HotEvent, ProjectError> {
    let p: QuotePayload = serde_json::from_str(payload)?;
    if p.instrument.is_empty() {
        return Err(ProjectError::MissingField("instrument"));
    }
    Ok(HotEvent::Quote(HotQuote {
        instrument: SmolStr::new(&p.instrument),
        bid: parse_price_field("bid", &p.bid)?,
        ask: parse_price_field("ask", &p.ask)?,
    }))
}

fn project_trade(payload: &str) -> Result<HotEvent, ProjectError> {
    let p: TradePayload = serde_json::from_str(payload)?;
    if p.instrument.is_empty() {
        return Err(ProjectError::MissingField("instrument"));
    }
    Ok(HotEvent::Trade(HotTrade {
        instrument: SmolStr::new(&p.instrument),
        price: parse_price_field("price", &p.price)?,
        size: parse_qty_field("size", &p.size)?,
    }))
}

fn project_book(payload: &str) -> Result<HotEvent, ProjectError> {
    let p: BookPayload = serde_json::from_str(payload)?;
    if p.instrument.is_empty() {
        return Err(ProjectError::MissingField("instrument"));
    }
    let (is_snapshot, bids, asks, truncated) = parse_kalshi_book(&p.raw)?;
    Ok(HotEvent::Book(HotBook {
        instrument: SmolStr::new(&p.instrument),
        is_snapshot,
        bids,
        asks,
        truncated,
    }))
}

fn project_own_fill(payload: &str) -> Result<HotEvent, ProjectError> {
    let p: OwnFillPayload = serde_json::from_str(payload)?;
    if p.instrument.is_empty() {
        return Err(ProjectError::MissingField("instrument"));
    }
    if p.client_order_id.is_empty() {
        return Err(ProjectError::MissingField("client_order_id"));
    }
    if p.fill_id.is_empty() {
        return Err(ProjectError::MissingField("fill_id"));
    }
    Ok(HotEvent::OwnFill(HotOwnFill {
        fill_id: SmolStr::new(&p.fill_id),
        client_order_id: SmolStr::new(&p.client_order_id),
        instrument: SmolStr::new(&p.instrument),
        price: parse_price_field("price", &p.price)?,
        quantity: parse_qty_field("quantity", &p.quantity)?,
        remaining_quantity: parse_qty_field("remaining_quantity", &p.remaining_quantity)?,
        fee: parse_price_field("fee", &p.fee)?,
    }))
}

fn project_own_order_update(payload: &str, reject: bool) -> Result<HotEvent, ProjectError> {
    let p: OwnOrderUpdatePayload = serde_json::from_str(payload)?;
    if p.client_order_id.is_empty() {
        return Err(ProjectError::MissingField("client_order_id"));
    }
    let update = HotOwnOrderUpdate {
        client_order_id: SmolStr::new(&p.client_order_id),
        instrument: SmolStr::new(&p.instrument),
        state: SmolStr::new(&p.state),
        reason: p.reason.map(|r| SmolStr::new(&r)),
    };
    Ok(if reject {
        HotEvent::OwnOrderReject(update)
    } else {
        HotEvent::OwnOrderUpdate(update)
    })
}

fn project_lifecycle(payload: &str) -> Result<HotEvent, ProjectError> {
    let p: LifecyclePayload = serde_json::from_str(payload)?;
    let raw = p.raw.as_ref();

    // Instrument may live at the top level (post-normalizer) or under `raw`
    // (legacy passthrough shape). We accept both.
    let instrument = p
        .instrument
        .or_else(|| {
            raw.and_then(|v| {
                v.get("instrument")
                    .or_else(|| v.get("instrument_id"))
                    .and_then(Value::as_str)
                    .map(str::to_string)
            })
        })
        .or_else(|| {
            raw.and_then(|v| {
                v.get("market_ticker")
                    .or_else(|| v.get("ticker"))
                    .and_then(Value::as_str)
                    .map(|s| {
                        if s.starts_with("kalshi:") {
                            s.to_string()
                        } else {
                            format!("kalshi:{s}")
                        }
                    })
            })
        })
        .ok_or(ProjectError::MissingField("instrument"))?;

    // Resolve the canonical `MarketState`. Preference order:
    //   1. explicit `state` (already canonical),
    //   2. `kalshi_event_type` mapped via `MarketState::from_kalshi_event_type`,
    //   3. raw `event_type` field if the normalizer hasn't tagged it.
    let state = if let Some(state_str) = p.state.as_deref() {
        parse_market_state(state_str)?
    } else if let Some(event_type) = p.kalshi_event_type.as_deref() {
        MarketState::from_kalshi_event_type(event_type)
    } else if let Some(event_type) = raw
        .and_then(|v| v.get("event_type"))
        .and_then(Value::as_str)
    {
        MarketState::from_kalshi_event_type(event_type)
    } else {
        return Err(ProjectError::MissingField("state"));
    };

    let reason = p
        .reason
        .or_else(|| p.kalshi_event_type.clone())
        .or_else(|| {
            raw.and_then(|v| {
                v.get("event_type")
                    .and_then(Value::as_str)
                    .map(str::to_string)
            })
        });

    Ok(HotEvent::MarketState(HotMarketState {
        instrument: SmolStr::new(&instrument),
        state,
        reason: reason.map(|r| SmolStr::new(&r)),
    }))
}

fn parse_market_state(value: &str) -> Result<MarketState, ProjectError> {
    match value.to_ascii_lowercase().as_str() {
        "listed" => Ok(MarketState::Listed),
        "opened" | "open" | "active" => Ok(MarketState::Opened),
        "paused" | "halted" | "deactivated" => Ok(MarketState::Paused),
        "resumed" => Ok(MarketState::Resumed),
        "metadata_updated" | "close_date_updated" => Ok(MarketState::MetadataUpdated),
        "closed" => Ok(MarketState::Closed),
        "determined" => Ok(MarketState::Determined),
        "disputed" => Ok(MarketState::Disputed),
        "finalized" | "settled" => Ok(MarketState::Finalized),
        _ => Err(ProjectError::MissingField("state")),
    }
}

fn passthrough(kind: HotEventKind, payload: &str) -> HotEvent {
    let instrument = serde_json::from_str::<Value>(payload).ok().and_then(|v| {
        v.get("instrument")
            .and_then(Value::as_str)
            .map(SmolStr::new)
    });
    HotEvent::Passthrough { kind, instrument }
}

/// Best-effort Kalshi snapshot/delta parser. Recognises:
///
/// - `{"snapshot": {"yes": [[price, size], ...], "no": [[price, size], ...]}}`
/// - `{"delta": {"side": "yes"|"no", "price": ..., "delta": ...}}`
/// - flat `{"yes": [...], "no": [...]}` (snapshot fallback)
///
/// Prices in Kalshi book payloads are cents (integers 1..99); we accept both
/// integer and decimal forms.
fn parse_kalshi_book(
    raw: &Value,
) -> Result<
    (
        bool,
        arrayvec::ArrayVec<Level, MAX_BOOK_LEVELS>,
        arrayvec::ArrayVec<Level, MAX_BOOK_LEVELS>,
        bool,
    ),
    ProjectError,
> {
    let mut bids = arrayvec::ArrayVec::<Level, MAX_BOOK_LEVELS>::new();
    let mut asks = arrayvec::ArrayVec::<Level, MAX_BOOK_LEVELS>::new();
    let mut truncated = false;

    let (snap_obj, is_snapshot) = if let Some(s) = raw.get("snapshot") {
        (s, true)
    } else if raw.get("delta").is_some() {
        let d = raw.get("delta").unwrap();
        let side = d.get("side").and_then(Value::as_str).unwrap_or("yes");
        let price = book_price_to_fixed(d.get("price"))?;
        let size = book_size_to_qty(d.get("delta").or_else(|| d.get("size")))?;
        let level = Level { price, size };
        if side.eq_ignore_ascii_case("yes") {
            let _ = bids.try_push(level);
        } else {
            let _ = asks.try_push(level);
        }
        return Ok((false, bids, asks, false));
    } else if raw.get("yes").is_some() || raw.get("no").is_some() {
        (raw, true)
    } else {
        return Ok((true, bids, asks, false));
    };

    if let Some(arr) = snap_obj.get("yes").and_then(Value::as_array) {
        for entry in arr {
            if let Some(level) = level_from_pair(entry)? {
                if bids.try_push(level).is_err() {
                    truncated = true;
                    break;
                }
            }
        }
    }
    if let Some(arr) = snap_obj.get("no").and_then(Value::as_array) {
        for entry in arr {
            if let Some(level) = level_from_pair(entry)? {
                if asks.try_push(level).is_err() {
                    truncated = true;
                    break;
                }
            }
        }
    }
    Ok((is_snapshot, bids, asks, truncated))
}

fn level_from_pair(v: &Value) -> Result<Option<Level>, ProjectError> {
    let arr = match v.as_array() {
        Some(a) if a.len() >= 2 => a,
        _ => return Ok(None),
    };
    let price = book_price_to_fixed(Some(&arr[0]))?;
    let size = book_size_to_qty(Some(&arr[1]))?;
    Ok(Some(Level { price, size }))
}

fn book_price_to_fixed(v: Option<&Value>) -> Result<FixedPrice, ProjectError> {
    match v {
        None | Some(Value::Null) => Ok(FixedPrice::ZERO),
        Some(Value::Number(n)) => {
            // Kalshi book prices are integer cents. Reject f64 explicitly —
            // would re-introduce float drift.
            if let Some(i) = n.as_i64() {
                Ok(FixedPrice::from_cents(i))
            } else if let Some(u) = n.as_u64() {
                Ok(FixedPrice::from_cents(u.min(i64::MAX as u64) as i64))
            } else {
                Err(ProjectError::Parse {
                    field: "price",
                    source: ParseError::InvalidChar('f', 0),
                })
            }
        }
        Some(Value::String(s)) => parse_price_field("price", s),
        Some(_) => Err(ProjectError::MissingField("price")),
    }
}

fn book_size_to_qty(v: Option<&Value>) -> Result<Qty, ProjectError> {
    match v {
        None | Some(Value::Null) => Ok(Qty::ZERO),
        Some(Value::Number(n)) => {
            if let Some(i) = n.as_i64() {
                Ok(Qty(i.max(0) as u32))
            } else if let Some(u) = n.as_u64() {
                Ok(Qty(u.min(u32::MAX as u64) as u32))
            } else {
                Err(ProjectError::Parse {
                    field: "size",
                    source: ParseError::InvalidQty,
                })
            }
        }
        Some(Value::String(s)) => parse_qty_field("size", s),
        Some(_) => Err(ProjectError::MissingField("size")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use eventcontracts_contracts::{AuditStamp, EventProvenance, Metadata};
    use serde_json::json;

    fn audit() -> AuditStamp {
        AuditStamp {
            object_id: "ev-1".into(),
            object_kind: "normalized_event".into(),
            schema_version: "normalized-event-v1".into(),
            produced_at: "2026-05-27T12:00:00Z".into(),
            producer: "kalshi-ws-normalizer".into(),
            canonical_sha256: "a".repeat(64),
            parent_ids: vec![],
            trace_id: None,
            metadata: Metadata::new(),
        }
    }

    fn provenance() -> EventProvenance {
        EventProvenance {
            source: "kalshi-ws".into(),
            channel: "ticker".into(),
            schema_version: "kalshi-ws-v1".into(),
            venue: Some("kalshi".into()),
            source_sequence: Some("1".into()),
            normalization_version: "kalshi-ws-v1".into(),
            metadata: Metadata::new(),
        }
    }

    fn make(kind: &str, payload: serde_json::Value) -> NormalizedEventRecord {
        NormalizedEventRecord {
            event_id: "ev-1".into(),
            event_kind: kind.into(),
            payload_json: payload.to_string(),
            provenance: provenance(),
            audit: audit(),
        }
    }

    #[test]
    fn project_quote_from_kalshi_normalizer_shape() {
        let rec = make(
            "quote",
            json!({"instrument": "kalshi:KXHIGHNY-26MAY27-T68", "bid": "0.42", "ask": "0.44"}),
        );
        let HotEvent::Quote(q) = project_event(&rec).unwrap() else {
            panic!("expected quote");
        };
        assert_eq!(q.instrument.as_str(), "kalshi:KXHIGHNY-26MAY27-T68");
        assert_eq!(q.bid, FixedPrice(4200));
        assert_eq!(q.ask, FixedPrice(4400));
        assert_eq!(q.spread(), FixedPrice(200));
        assert_eq!(q.mid(), FixedPrice(4300));
    }

    #[test]
    fn project_quote_tolerates_empty_decimals() {
        // The Kalshi normalizer's `first_decimal` returns "" when the
        // ticker message has neither *_dollars nor cent fields. The hot
        // path must not crash; it sees a zero price.
        let rec = make(
            "quote",
            json!({"instrument": "kalshi:X", "bid": "", "ask": ""}),
        );
        let HotEvent::Quote(q) = project_event(&rec).unwrap() else {
            panic!("expected quote");
        };
        assert_eq!(q.bid, FixedPrice::ZERO);
        assert_eq!(q.ask, FixedPrice::ZERO);
    }

    #[test]
    fn project_trade_from_kalshi_shape() {
        let rec = make(
            "trade",
            json!({"instrument": "kalshi:X", "price": "0.43", "size": "5"}),
        );
        let HotEvent::Trade(t) = project_event(&rec).unwrap() else {
            panic!("expected trade");
        };
        assert_eq!(t.price, FixedPrice(4300));
        assert_eq!(t.size, Qty(5));
    }

    #[test]
    fn project_book_snapshot_yes_no_arrays() {
        let rec = make(
            "book",
            json!({
                "instrument": "kalshi:X",
                "raw": {
                    "snapshot": {
                        "yes": [[42, 100], [41, 250]],
                        "no":  [[58, 75]],
                    }
                }
            }),
        );
        let HotEvent::Book(b) = project_event(&rec).unwrap() else {
            panic!("expected book");
        };
        assert!(b.is_snapshot);
        assert!(!b.truncated);
        assert_eq!(b.bids.len(), 2);
        assert_eq!(b.bids[0].price, FixedPrice::from_cents(42));
        assert_eq!(b.bids[0].size, Qty(100));
        assert_eq!(b.asks.len(), 1);
        assert_eq!(b.asks[0].price, FixedPrice::from_cents(58));
        assert_eq!(b.best_bid().unwrap().size, Qty(100));
    }

    #[test]
    fn project_book_delta_yes_side() {
        let rec = make(
            "book",
            json!({
                "instrument": "kalshi:X",
                "raw": {
                    "delta": {"side": "yes", "price": 42, "delta": 50}
                }
            }),
        );
        let HotEvent::Book(b) = project_event(&rec).unwrap() else {
            panic!("expected book");
        };
        assert!(!b.is_snapshot);
        assert_eq!(b.bids.len(), 1);
        assert_eq!(b.bids[0].price, FixedPrice::from_cents(42));
        assert_eq!(b.bids[0].size, Qty(50));
        assert!(b.asks.is_empty());
    }

    #[test]
    fn project_book_truncates_past_max_levels() {
        let levels: Vec<_> = (0..(MAX_BOOK_LEVELS + 5))
            .map(|i| json!([99 - i as i64, 10]))
            .collect();
        let rec = make(
            "book",
            json!({
                "instrument": "kalshi:X",
                "raw": {"snapshot": {"yes": levels, "no": []}}
            }),
        );
        let HotEvent::Book(b) = project_event(&rec).unwrap() else {
            panic!("expected book");
        };
        assert!(b.truncated);
        assert_eq!(b.bids.len(), MAX_BOOK_LEVELS);
    }

    #[test]
    fn project_own_fill() {
        let rec = make(
            "own_fill",
            json!({
                "fill_id": "f-1",
                "client_order_id": "c-1",
                "instrument": "kalshi:X",
                "price": "0.42",
                "quantity": "10",
                "remaining_quantity": "90",
                "fee": "0",
            }),
        );
        let HotEvent::OwnFill(f) = project_event(&rec).unwrap() else {
            panic!("expected own_fill");
        };
        assert_eq!(f.fill_id.as_str(), "f-1");
        assert_eq!(f.client_order_id.as_str(), "c-1");
        assert_eq!(f.price, FixedPrice(4200));
        assert_eq!(f.quantity, Qty(10));
        assert_eq!(f.remaining_quantity, Qty(90));
    }

    #[test]
    fn project_own_order_reject_routes_to_reject_variant() {
        let rec = make(
            "own_order_reject",
            json!({
                "client_order_id": "c-1",
                "instrument": "kalshi:X",
                "state": "rejected",
                "reason": "price_band",
            }),
        );
        match project_event(&rec).unwrap() {
            HotEvent::OwnOrderReject(u) => {
                assert_eq!(u.state.as_str(), "rejected");
                assert_eq!(u.reason.as_deref(), Some("price_band"));
            }
            other => panic!("expected reject, got {other:?}"),
        }
    }

    #[test]
    fn lifecycle_projects_typed_market_state_from_kalshi_payload() {
        // What the Kalshi normalizer now emits: top-level instrument plus
        // the canonical `kalshi_event_type`.
        let rec = make(
            "lifecycle",
            json!({
                "instrument": "kalshi:KXHIGHNY-26MAY27-T68",
                "kalshi_event_type": "deactivated",
            }),
        );
        let HotEvent::MarketState(state) = project_event(&rec).unwrap() else {
            panic!("expected MarketState");
        };
        assert_eq!(state.instrument.as_str(), "kalshi:KXHIGHNY-26MAY27-T68");
        assert_eq!(state.state, MarketState::Paused);
        assert!(!state.state.is_tradable());
        assert_eq!(state.reason.as_deref(), Some("deactivated"));
    }

    #[test]
    fn lifecycle_projects_typed_state_from_explicit_state_field() {
        let rec = make(
            "lifecycle",
            json!({
                "instrument": "kalshi:KXHIGHNY-26MAY27-T68",
                "state": "resumed",
            }),
        );
        let HotEvent::MarketState(state) = project_event(&rec).unwrap() else {
            panic!("expected MarketState");
        };
        assert_eq!(state.state, MarketState::Resumed);
        assert!(state.state.is_tradable());
    }

    #[test]
    fn lifecycle_projects_from_raw_envelope_passthrough_shape() {
        // Legacy normalizer shape — `raw` block with the Kalshi envelope.
        let rec = make(
            "lifecycle",
            json!({
                "raw": {
                    "market_ticker": "KXHIGHNY-26MAY27-T68",
                    "event_type": "activated",
                }
            }),
        );
        let HotEvent::MarketState(state) = project_event(&rec).unwrap() else {
            panic!("expected MarketState");
        };
        assert_eq!(state.instrument.as_str(), "kalshi:KXHIGHNY-26MAY27-T68");
        assert_eq!(state.state, MarketState::Opened);
    }

    #[test]
    fn lifecycle_without_instrument_or_state_is_a_projection_error() {
        let rec = make("lifecycle", json!({"raw": {}}));
        assert!(matches!(
            project_event(&rec),
            Err(ProjectError::MissingField(_))
        ));
    }

    #[test]
    fn unknown_kind_errors() {
        // event_kind is validated by contracts::Contract::validate before
        // it reaches us, but defense-in-depth: handle the unknown case.
        let rec = NormalizedEventRecord {
            event_id: "e".into(),
            event_kind: "halt".into(),
            payload_json: "{}".into(),
            provenance: provenance(),
            audit: audit(),
        };
        assert!(matches!(
            project_event(&rec),
            Err(ProjectError::UnknownKind(_))
        ));
    }

    #[test]
    fn projection_is_zero_alloc_for_short_instrument_ids() {
        // SmolStr inlines up to 23 bytes. "kalshi:X" (8) is well within;
        // confirm we don't accidentally box the SmolStr via `From<String>`
        // — the projection uses `SmolStr::new(&str)` for that reason.
        let rec = make(
            "quote",
            json!({"instrument": "kalshi:X", "bid": "0.5", "ask": "0.6"}),
        );
        let HotEvent::Quote(q) = project_event(&rec).unwrap() else {
            panic!();
        };
        assert!(!q.instrument.is_heap_allocated());
    }
}
