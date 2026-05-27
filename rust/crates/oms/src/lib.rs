//! Order management state machine.
//!
//! OMS is the source of truth for order state. Every transition is explicit,
//! illegal transitions return a typed error, and fill ingestion is idempotent
//! via `(order_id, fill_id)` dedupe. Cash and position keeping live elsewhere
//! — OMS only owns order lifecycle.
//!
//! State diagram:
//!
//! ```text
//!                +--> Rejected
//!                |
//!   Created --> Submitted --> Acked --+--> PartiallyFilled --+--> Filled
//!                                     |        ^             |
//!                                     |        |             +--> Canceled
//!                                     +--> Canceled <--------+
//!                                     |
//!                                     +--> Expired
//! ```

use eventcontracts_contracts::{require_decimal_string, require_non_empty, ContractError};
use serde::{Deserialize, Serialize};
use std::collections::{HashMap, HashSet};
use thiserror::Error;

// ---------- error ----------

#[derive(Debug, Error, PartialEq, Eq, Clone)]
pub enum OmsError {
    #[error("illegal transition from {from:?} to {to:?}")]
    IllegalTransition { from: OrderState, to: OrderState },
    #[error("unknown order {0}")]
    UnknownOrder(String),
    #[error("duplicate order id {0}")]
    DuplicateOrder(String),
    #[error("fill quantity {fill} would exceed remaining {remaining} on order {order_id}")]
    Overfill {
        order_id: String,
        fill: String,
        remaining: String,
    },
    #[error("contract validation failed: {0}")]
    Contract(#[from] ContractError),
}

// ---------- types ----------

#[derive(Clone, Copy, Debug, Eq, PartialEq, Hash, Serialize, Deserialize)]
pub enum Side {
    Buy,
    Sell,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Hash, Serialize, Deserialize)]
pub enum TimeInForce {
    Gtc,
    Ioc,
    Fok,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Hash, Serialize, Deserialize)]
pub enum OrderState {
    Created,
    Submitted,
    Acked,
    PartiallyFilled,
    Filled,
    Canceled,
    Rejected,
    Expired,
}

impl OrderState {
    pub fn is_terminal(&self) -> bool {
        matches!(
            self,
            OrderState::Filled | OrderState::Canceled | OrderState::Rejected | OrderState::Expired
        )
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Order {
    pub client_order_id: String,
    pub venue_order_id: Option<String>,
    pub instrument_id: String,
    pub side: Side,
    pub price: String,
    pub quantity: String,
    pub filled_quantity: String,
    pub time_in_force: TimeInForce,
    pub state: OrderState,
    pub created_at: String,
    pub last_update_at: String,
    pub last_reject_reason: Option<String>,
}

impl Order {
    pub fn new(
        client_order_id: impl Into<String>,
        instrument_id: impl Into<String>,
        side: Side,
        price: impl Into<String>,
        quantity: impl Into<String>,
        tif: TimeInForce,
        created_at: impl Into<String>,
    ) -> Result<Self, OmsError> {
        let client_order_id = client_order_id.into();
        let instrument_id = instrument_id.into();
        let price = price.into();
        let quantity = quantity.into();
        let created_at = created_at.into();
        require_non_empty("client_order_id", &client_order_id)?;
        require_non_empty("instrument_id", &instrument_id)?;
        require_decimal_string("price", &price)?;
        require_decimal_string("quantity", &quantity)?;
        Ok(Order {
            client_order_id,
            venue_order_id: None,
            instrument_id,
            side,
            price,
            quantity,
            filled_quantity: "0".to_string(),
            time_in_force: tif,
            state: OrderState::Created,
            created_at: created_at.clone(),
            last_update_at: created_at,
            last_reject_reason: None,
        })
    }

    pub fn remaining(&self) -> f64 {
        parse_decimal(&self.quantity) - parse_decimal(&self.filled_quantity)
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Fill {
    pub fill_id: String,
    pub client_order_id: String,
    pub price: String,
    pub quantity: String,
    pub fee: String,
    pub trade_ts: String,
}

// ---------- transition table ----------

fn transition_allowed(from: OrderState, to: OrderState) -> bool {
    use OrderState::*;
    matches!(
        (from, to),
        (Created, Submitted)
            | (Submitted, Acked)
            | (Submitted, Rejected)
            | (Submitted, Canceled)
            | (Acked, PartiallyFilled)
            | (Acked, Filled)
            | (Acked, Canceled)
            | (Acked, Expired)
            | (PartiallyFilled, PartiallyFilled)
            | (PartiallyFilled, Filled)
            | (PartiallyFilled, Canceled)
            | (PartiallyFilled, Expired)
    )
}

// ---------- store ----------

#[derive(Debug, Default)]
pub struct InMemoryOms {
    orders: HashMap<String, Order>,
    applied_fills: HashSet<(String, String)>,
}

impl InMemoryOms {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn open_order(&mut self, order: Order) -> Result<(), OmsError> {
        if self.orders.contains_key(&order.client_order_id) {
            return Err(OmsError::DuplicateOrder(order.client_order_id.clone()));
        }
        self.orders.insert(order.client_order_id.clone(), order);
        Ok(())
    }

    pub fn get(&self, client_order_id: &str) -> Option<&Order> {
        self.orders.get(client_order_id)
    }

    pub fn transition(
        &mut self,
        client_order_id: &str,
        to: OrderState,
        now: &str,
        reject_reason: Option<&str>,
    ) -> Result<&Order, OmsError> {
        let order = self
            .orders
            .get_mut(client_order_id)
            .ok_or_else(|| OmsError::UnknownOrder(client_order_id.to_string()))?;
        if !transition_allowed(order.state, to) {
            return Err(OmsError::IllegalTransition {
                from: order.state,
                to,
            });
        }
        order.state = to;
        order.last_update_at = now.to_string();
        if let Some(reason) = reject_reason {
            order.last_reject_reason = Some(reason.to_string());
        }
        Ok(order)
    }

    pub fn set_venue_order_id(
        &mut self,
        client_order_id: &str,
        venue_order_id: impl Into<String>,
    ) -> Result<(), OmsError> {
        let order = self
            .orders
            .get_mut(client_order_id)
            .ok_or_else(|| OmsError::UnknownOrder(client_order_id.to_string()))?;
        order.venue_order_id = Some(venue_order_id.into());
        Ok(())
    }

    /// Idempotent: a `(client_order_id, fill_id)` already applied is a no-op
    /// returning `Ok(false)`. Returns `Ok(true)` when a new fill was applied.
    pub fn apply_fill(&mut self, fill: Fill) -> Result<bool, OmsError> {
        let key = (fill.client_order_id.clone(), fill.fill_id.clone());
        if self.applied_fills.contains(&key) {
            return Ok(false);
        }
        require_decimal_string("price", &fill.price)?;
        require_decimal_string("quantity", &fill.quantity)?;

        let order = self
            .orders
            .get_mut(&fill.client_order_id)
            .ok_or_else(|| OmsError::UnknownOrder(fill.client_order_id.clone()))?;
        let fill_qty = parse_decimal(&fill.quantity);
        let remaining = parse_decimal(&order.quantity) - parse_decimal(&order.filled_quantity);
        if fill_qty - remaining > 1e-9 {
            return Err(OmsError::Overfill {
                order_id: order.client_order_id.clone(),
                fill: fill.quantity.clone(),
                remaining: format_decimal(remaining),
            });
        }
        let new_filled = parse_decimal(&order.filled_quantity) + fill_qty;
        order.filled_quantity = format_decimal(new_filled);
        order.last_update_at = fill.trade_ts.clone();
        let total = parse_decimal(&order.quantity);
        let next_state = if (new_filled - total).abs() < 1e-9 {
            OrderState::Filled
        } else {
            OrderState::PartiallyFilled
        };
        if !transition_allowed(order.state, next_state) {
            return Err(OmsError::IllegalTransition {
                from: order.state,
                to: next_state,
            });
        }
        order.state = next_state;
        self.applied_fills.insert(key);
        Ok(true)
    }

    pub fn open_orders(&self) -> impl Iterator<Item = &Order> {
        self.orders.values().filter(|o| !o.state.is_terminal())
    }
}

// ---------- numeric helpers ----------
// Decimal-as-string is the cross-language contract; OMS uses f64 only for
// internal arithmetic. Production grade decimal math would use `rust_decimal`;
// this is intentional to keep the dependency set minimal until the parity gate.

fn parse_decimal(s: &str) -> f64 {
    s.parse::<f64>().unwrap_or(0.0)
}

fn format_decimal(v: f64) -> String {
    format!("{v}")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn place(oms: &mut InMemoryOms, id: &str, qty: &str) {
        let order = Order::new(
            id,
            "kalshi:KXMARKET-1",
            Side::Buy,
            "0.50",
            qty,
            TimeInForce::Gtc,
            "2026-05-26T12:00:00Z",
        )
        .unwrap();
        oms.open_order(order).unwrap();
    }

    fn t(oms: &mut InMemoryOms, id: &str, to: OrderState) {
        oms.transition(id, to, "2026-05-26T12:00:01Z", None).unwrap();
    }

    #[test]
    fn happy_path_created_submitted_acked_filled() {
        let mut oms = InMemoryOms::new();
        place(&mut oms, "c-1", "10");
        t(&mut oms, "c-1", OrderState::Submitted);
        t(&mut oms, "c-1", OrderState::Acked);
        let applied = oms
            .apply_fill(Fill {
                fill_id: "f-1".into(),
                client_order_id: "c-1".into(),
                price: "0.50".into(),
                quantity: "10".into(),
                fee: "0".into(),
                trade_ts: "2026-05-26T12:00:02Z".into(),
            })
            .unwrap();
        assert!(applied);
        let order = oms.get("c-1").unwrap();
        assert_eq!(order.state, OrderState::Filled);
        assert_eq!(order.filled_quantity, "10");
    }

    #[test]
    fn partial_fills_aggregate_then_complete() {
        let mut oms = InMemoryOms::new();
        place(&mut oms, "c-2", "10");
        t(&mut oms, "c-2", OrderState::Submitted);
        t(&mut oms, "c-2", OrderState::Acked);
        oms.apply_fill(Fill {
            fill_id: "f-a".into(),
            client_order_id: "c-2".into(),
            price: "0.50".into(),
            quantity: "4".into(),
            fee: "0".into(),
            trade_ts: "2026-05-26T12:00:02Z".into(),
        })
        .unwrap();
        assert_eq!(oms.get("c-2").unwrap().state, OrderState::PartiallyFilled);
        oms.apply_fill(Fill {
            fill_id: "f-b".into(),
            client_order_id: "c-2".into(),
            price: "0.50".into(),
            quantity: "6".into(),
            fee: "0".into(),
            trade_ts: "2026-05-26T12:00:03Z".into(),
        })
        .unwrap();
        assert_eq!(oms.get("c-2").unwrap().state, OrderState::Filled);
    }

    #[test]
    fn duplicate_fill_is_idempotent() {
        let mut oms = InMemoryOms::new();
        place(&mut oms, "c-3", "10");
        t(&mut oms, "c-3", OrderState::Submitted);
        t(&mut oms, "c-3", OrderState::Acked);
        let fill = Fill {
            fill_id: "f-x".into(),
            client_order_id: "c-3".into(),
            price: "0.50".into(),
            quantity: "10".into(),
            fee: "0".into(),
            trade_ts: "2026-05-26T12:00:02Z".into(),
        };
        assert!(oms.apply_fill(fill.clone()).unwrap());
        assert!(!oms.apply_fill(fill).unwrap());
        assert_eq!(oms.get("c-3").unwrap().filled_quantity, "10");
    }

    #[test]
    fn overfill_rejected() {
        let mut oms = InMemoryOms::new();
        place(&mut oms, "c-4", "5");
        t(&mut oms, "c-4", OrderState::Submitted);
        t(&mut oms, "c-4", OrderState::Acked);
        let err = oms
            .apply_fill(Fill {
                fill_id: "f-bad".into(),
                client_order_id: "c-4".into(),
                price: "0.50".into(),
                quantity: "6".into(),
                fee: "0".into(),
                trade_ts: "2026-05-26T12:00:02Z".into(),
            })
            .unwrap_err();
        assert!(matches!(err, OmsError::Overfill { .. }));
    }

    #[test]
    fn illegal_transition_rejected() {
        let mut oms = InMemoryOms::new();
        place(&mut oms, "c-5", "10");
        let err = oms
            .transition("c-5", OrderState::Filled, "2026-05-26T12:00:01Z", None)
            .unwrap_err();
        assert!(matches!(err, OmsError::IllegalTransition { .. }));
    }

    #[test]
    fn duplicate_client_order_id_rejected() {
        let mut oms = InMemoryOms::new();
        place(&mut oms, "c-6", "10");
        let order = Order::new(
            "c-6",
            "kalshi:KXMARKET-1",
            Side::Buy,
            "0.50",
            "10",
            TimeInForce::Gtc,
            "2026-05-26T12:00:00Z",
        )
        .unwrap();
        let err = oms.open_order(order).unwrap_err();
        assert!(matches!(err, OmsError::DuplicateOrder(_)));
    }

    #[test]
    fn cancel_after_partial_fill_terminates_order() {
        let mut oms = InMemoryOms::new();
        place(&mut oms, "c-7", "10");
        t(&mut oms, "c-7", OrderState::Submitted);
        t(&mut oms, "c-7", OrderState::Acked);
        oms.apply_fill(Fill {
            fill_id: "f-p".into(),
            client_order_id: "c-7".into(),
            price: "0.50".into(),
            quantity: "3".into(),
            fee: "0".into(),
            trade_ts: "2026-05-26T12:00:02Z".into(),
        })
        .unwrap();
        t(&mut oms, "c-7", OrderState::Canceled);
        let o = oms.get("c-7").unwrap();
        assert_eq!(o.state, OrderState::Canceled);
        assert!(o.state.is_terminal());
    }

    #[test]
    fn open_orders_excludes_terminal_states() {
        let mut oms = InMemoryOms::new();
        place(&mut oms, "live", "5");
        place(&mut oms, "done", "5");
        t(&mut oms, "live", OrderState::Submitted);
        t(&mut oms, "live", OrderState::Acked);
        t(&mut oms, "done", OrderState::Submitted);
        oms.transition("done", OrderState::Rejected, "2026-05-26T12:00:01Z", Some("venue_busy"))
            .unwrap();
        let ids: Vec<&str> = oms
            .open_orders()
            .map(|o| o.client_order_id.as_str())
            .collect();
        assert_eq!(ids, vec!["live"]);
    }
}
