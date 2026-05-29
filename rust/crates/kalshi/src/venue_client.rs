//! Live venue adapter for Kalshi.
//!
//! Production order entry uses [`AsyncVenueClient`]. The synchronous
//! [`VenueClient`] methods are deliberately disabled for this adapter so a
//! future caller cannot reintroduce blocking REST work on a tokio worker.

use std::collections::HashMap;
use std::sync::Arc;

use eventcontracts_contracts::IntentEnvelopeRecord;
use eventcontracts_gateway::{
    AsyncVenueClient, DecisionPayload, GatewayAck, GatewayError, OutcomeSide, VenueClient,
};
use eventcontracts_oms::{Side, TimeInForce};
use tokio::runtime::Handle;

use crate::rest::{KalshiRest, PlaceOrderRequest, RestError};

pub struct KalshiVenueClient {
    rest: Arc<KalshiRest>,
    _handle: Handle,
    /// `client_order_id` -> venue `order_id`, populated on successful async
    /// submit so a later cancel can address the order by venue id.
    venue_ids: HashMap<String, String>,
}

impl KalshiVenueClient {
    pub fn new(rest: KalshiRest, handle: Handle) -> Self {
        Self {
            rest: Arc::new(rest),
            _handle: handle,
            venue_ids: HashMap::new(),
        }
    }

    pub fn venue_id_for(&self, client_order_id: &str) -> Option<&str> {
        self.venue_ids.get(client_order_id).map(String::as_str)
    }
}

impl VenueClient for KalshiVenueClient {
    fn submit(
        &mut self,
        envelope: &IntentEnvelopeRecord,
        payload: &DecisionPayload,
    ) -> Result<GatewayAck, GatewayError> {
        let _ = (envelope, payload);
        Err(GatewayError::Transport(
            "KalshiVenueClient sync submit disabled; use submit_async".into(),
        ))
    }

    fn cancel(
        &mut self,
        envelope: &IntentEnvelopeRecord,
        client_order_id: &str,
    ) -> Result<GatewayAck, GatewayError> {
        let _ = (envelope, client_order_id);
        Err(GatewayError::Transport(
            "KalshiVenueClient sync cancel disabled; use cancel_async".into(),
        ))
    }

    fn cancel_all(&mut self, now: &str) -> Result<Vec<GatewayAck>, GatewayError> {
        let _ = now;
        Err(GatewayError::Transport(
            "KalshiVenueClient sync cancel_all disabled; use cancel_all_async".into(),
        ))
    }
}

#[async_trait::async_trait]
impl AsyncVenueClient for KalshiVenueClient {
    async fn submit_async(
        &mut self,
        envelope: &IntentEnvelopeRecord,
        payload: &DecisionPayload,
    ) -> Result<GatewayAck, GatewayError> {
        let req = build_place_order_request(payload)?;
        let response = self
            .rest
            .submit_order(&req)
            .await
            .map_err(rest_err_to_gateway)?;
        let order = response.order;
        let venue_order_id = order.order_id.clone();
        if let DecisionPayload::PlaceOrder {
            client_order_id, ..
        } = payload
        {
            self.venue_ids
                .insert(client_order_id.clone(), venue_order_id.clone());
        }
        Ok(GatewayAck {
            correlation_id: envelope.correlation_id.clone(),
            accepted: true,
            acknowledged_at: order
                .created_time
                .unwrap_or_else(|| envelope.emitted_at.clone()),
            venue_order_id: Some(venue_order_id),
            reasons: vec![],
        })
    }

    async fn cancel_async(
        &mut self,
        envelope: &IntentEnvelopeRecord,
        client_order_id: &str,
    ) -> Result<GatewayAck, GatewayError> {
        if !self.venue_ids.contains_key(client_order_id) {
            let found = self
                .rest
                .get_order_by_client_order_id(client_order_id)
                .await
                .map_err(rest_err_to_gateway)?;
            if let Some(order) = found {
                self.venue_ids
                    .insert(client_order_id.to_string(), order.order_id);
            }
        }
        let Some(venue_order_id) = self.venue_ids.get(client_order_id).cloned() else {
            return Err(GatewayError::Transport(format!(
                "no venue_order_id cached for client_order_id `{client_order_id}`; \
                 KalshiVenueClient can only cancel orders it submitted in-process"
            )));
        };
        let order = self
            .rest
            .cancel_order(&venue_order_id)
            .await
            .map_err(rest_err_to_gateway)?;
        Ok(GatewayAck {
            correlation_id: envelope.correlation_id.clone(),
            accepted: true,
            acknowledged_at: order
                .last_update_time
                .or(order.created_time)
                .unwrap_or_else(|| envelope.emitted_at.clone()),
            venue_order_id: Some(order.order_id),
            reasons: vec![],
        })
    }

    async fn cancel_all_async(&mut self, now: &str) -> Result<Vec<GatewayAck>, GatewayError> {
        let orders = self
            .rest
            .cancel_all_open_orders()
            .await
            .map_err(rest_err_to_gateway)?;
        self.venue_ids.clear();
        Ok(orders
            .into_iter()
            .map(|order| GatewayAck {
                correlation_id: format!("cancel-all-{}", order.order_id),
                accepted: true,
                acknowledged_at: order
                    .last_update_time
                    .or(order.created_time)
                    .unwrap_or_else(|| now.to_string()),
                venue_order_id: Some(order.order_id),
                reasons: vec!["bulk_cancel".into()],
            })
            .collect())
    }
}

fn build_place_order_request(payload: &DecisionPayload) -> Result<PlaceOrderRequest, GatewayError> {
    let DecisionPayload::PlaceOrder {
        client_order_id,
        instrument_id,
        outcome_side,
        side,
        price,
        quantity,
        time_in_force,
        ..
    } = payload
    else {
        return Err(GatewayError::DecisionDecode(
            "build_place_order_request called with cancel payload".into(),
        ));
    };

    let ticker = instrument_id.strip_prefix("kalshi:").ok_or_else(|| {
        GatewayError::Transport(format!(
            "instrument_id `{instrument_id}` is not a kalshi instrument"
        ))
    })?;
    let cents = decimal_dollars_to_cents(price).ok_or_else(|| {
        GatewayError::DecisionDecode(format!(
            "price `{price}` is not an integer-cent value Kalshi will accept"
        ))
    })?;
    if !(1..=99).contains(&cents) {
        return Err(GatewayError::DecisionDecode(format!(
            "price `{price}` -> {cents}c is outside Kalshi's 1..=99 range"
        )));
    }
    let count: u32 = quantity.parse().map_err(|_| {
        GatewayError::DecisionDecode(format!("quantity `{quantity}` is not a positive integer"))
    })?;

    let (yes_price, no_price) = match outcome_side {
        OutcomeSide::Yes => (Some(cents), None),
        OutcomeSide::No => (None, Some(cents)),
    };

    Ok(PlaceOrderRequest {
        ticker: ticker.to_string(),
        client_order_id: client_order_id.clone(),
        side: match outcome_side {
            OutcomeSide::Yes => "yes".into(),
            OutcomeSide::No => "no".into(),
        },
        action: match side {
            Side::Buy => "buy".into(),
            Side::Sell => "sell".into(),
        },
        order_type: "limit".into(),
        count,
        yes_price,
        no_price,
        time_in_force: match time_in_force {
            TimeInForce::Gtc => "GTC".into(),
            TimeInForce::Ioc => "IOC".into(),
            TimeInForce::Fok => "FOK".into(),
        },
    })
}

fn decimal_dollars_to_cents(s: &str) -> Option<i64> {
    let price = eventcontracts_runtime_hot::parse_fixed_price(s).ok()?;
    let raw = price.raw();
    if raw % 100 != 0 {
        return None;
    }
    Some(raw / 100)
}

fn rest_err_to_gateway(e: RestError) -> GatewayError {
    GatewayError::Transport(e.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn place(price: &str, qty: &str, side: Side, outcome: OutcomeSide) -> DecisionPayload {
        DecisionPayload::PlaceOrder {
            client_order_id: "c-1".into(),
            instrument_id: "kalshi:KXHIGHNY-26MAY27-T68".into(),
            outcome_side: outcome,
            side,
            price: price.into(),
            quantity: qty.into(),
            fair_price: None,
            min_executable_edge_ticks: None,
            fee_rate_bps: None,
            time_in_force: TimeInForce::Ioc,
        }
    }

    #[test]
    fn builds_kalshi_yes_buy_request_from_decision_payload() {
        let p = place("0.42", "10", Side::Buy, OutcomeSide::Yes);
        let req = build_place_order_request(&p).unwrap();
        assert_eq!(req.ticker, "KXHIGHNY-26MAY27-T68");
        assert_eq!(req.client_order_id, "c-1");
        assert_eq!(req.side, "yes");
        assert_eq!(req.action, "buy");
        assert_eq!(req.order_type, "limit");
        assert_eq!(req.count, 10);
        assert_eq!(req.yes_price, Some(42));
        assert_eq!(req.no_price, None);
        assert_eq!(req.time_in_force, "IOC");
    }

    #[test]
    fn builds_no_sell_request_routes_price_to_no_price_field() {
        let p = place("0.65", "3", Side::Sell, OutcomeSide::No);
        let req = build_place_order_request(&p).unwrap();
        assert_eq!(req.side, "no");
        assert_eq!(req.action, "sell");
        assert_eq!(req.yes_price, None);
        assert_eq!(req.no_price, Some(65));
    }

    #[test]
    fn rejects_non_kalshi_instrument() {
        let mut p = place("0.42", "10", Side::Buy, OutcomeSide::Yes);
        if let DecisionPayload::PlaceOrder {
            ref mut instrument_id,
            ..
        } = p
        {
            *instrument_id = "polymarket:0xabc".into();
        }
        let err = build_place_order_request(&p).unwrap_err();
        assert!(matches!(err, GatewayError::Transport(msg) if msg.contains("not a kalshi")));
    }

    #[test]
    fn rejects_price_with_sub_cent_precision() {
        let p = place("0.425", "10", Side::Buy, OutcomeSide::Yes);
        let err = build_place_order_request(&p).unwrap_err();
        assert!(matches!(err, GatewayError::DecisionDecode(_)));
    }

    #[test]
    fn rejects_price_outside_1_to_99_cents() {
        let p = place("0", "10", Side::Buy, OutcomeSide::Yes);
        assert!(build_place_order_request(&p).is_err());
        let p2 = place("1.00", "10", Side::Buy, OutcomeSide::Yes);
        assert!(build_place_order_request(&p2).is_err());
    }
}
