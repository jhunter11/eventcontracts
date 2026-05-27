//! Stateful pre-trade risk.
//!
//! Risk runs once in the runner (this crate) and again in the gateway from its
//! own snapshot — by design. An intent is approved only if every check passes
//! against a **projected** sleeve state (existing exposure + this intent).
//!
//! Checks implemented:
//! - max_order_notional
//! - max_position_notional (projected)
//! - max_gross_exposure (projected, signed-side aware)
//! - max_open_orders
//! - max_daily_loss
//! - kill_switch (operator halt)
//! - stale_market_data (per-instrument freshness window)
//!
//! Decision values are decimal strings to preserve precision; arithmetic uses
//! `f64` and is replaced with `rust_decimal` once parity tests are wired in.

use eventcontracts_oms::Side;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use thiserror::Error;

#[derive(Debug, Error, PartialEq, Clone)]
pub enum RiskError {
    #[error("decimal parse failed for field `{0}`")]
    Decimal(&'static str),
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct RiskLimits {
    pub max_order_notional: String,
    pub max_position_notional: String,
    pub max_daily_loss: String,
    pub max_open_orders: u32,
    pub max_gross_exposure: String,
    pub currency: String,
    /// Maximum age in seconds a market-data observation can have before the
    /// runner refuses to act on it.
    pub max_market_data_age_secs: u32,
}

#[derive(Clone, Debug, Default)]
pub struct SleeveState {
    pub open_orders: u32,
    /// Per-instrument signed position (+long, -short) and average price, in
    /// decimal-string form.
    pub positions: HashMap<String, Position>,
    pub daily_realized_loss: f64,
    pub kill_switch_engaged: bool,
    /// Per-instrument last market-data observation timestamp (RFC3339).
    pub market_data_age_secs: HashMap<String, u32>,
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct Position {
    pub quantity: f64,
    pub avg_price: f64,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct IntentSnapshot {
    pub client_order_id: String,
    pub instrument_id: String,
    pub side: Side,
    pub price: String,
    pub quantity: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum RiskRejection {
    OrderNotionalExceeded {
        order_notional: String,
        limit: String,
    },
    PositionNotionalExceeded {
        projected: String,
        limit: String,
    },
    GrossExposureExceeded {
        projected: String,
        limit: String,
    },
    OpenOrdersExceeded {
        projected: u32,
        limit: u32,
    },
    DailyLossExceeded {
        realized: String,
        limit: String,
    },
    KillSwitchEngaged,
    StaleMarketData {
        instrument_id: String,
        age_secs: u32,
        limit_secs: u32,
    },
    InvalidNumeric {
        field: String,
    },
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum RiskDecision {
    Approved,
    Rejected(RiskRejection),
}

pub struct RiskGate {
    pub limits: RiskLimits,
}

impl RiskGate {
    pub fn new(limits: RiskLimits) -> Self {
        Self { limits }
    }

    pub fn evaluate(&self, state: &SleeveState, intent: &IntentSnapshot) -> RiskDecision {
        if state.kill_switch_engaged {
            return RiskDecision::Rejected(RiskRejection::KillSwitchEngaged);
        }
        if let Some(age) = state.market_data_age_secs.get(&intent.instrument_id) {
            if *age > self.limits.max_market_data_age_secs {
                return RiskDecision::Rejected(RiskRejection::StaleMarketData {
                    instrument_id: intent.instrument_id.clone(),
                    age_secs: *age,
                    limit_secs: self.limits.max_market_data_age_secs,
                });
            }
        }
        let price = match intent.price.parse::<f64>() {
            Ok(v) => v,
            Err(_) => return reject_numeric("price"),
        };
        let qty = match intent.quantity.parse::<f64>() {
            Ok(v) => v,
            Err(_) => return reject_numeric("quantity"),
        };
        let order_notional = price * qty;
        let max_order = parse_or_return(&self.limits.max_order_notional, "max_order_notional");
        let max_position = parse_or_return(&self.limits.max_position_notional, "max_position_notional");
        let max_gross = parse_or_return(&self.limits.max_gross_exposure, "max_gross_exposure");
        let max_loss = parse_or_return(&self.limits.max_daily_loss, "max_daily_loss");
        let (max_order, max_position, max_gross, max_loss) =
            match (max_order, max_position, max_gross, max_loss) {
                (Ok(a), Ok(b), Ok(c), Ok(d)) => (a, b, c, d),
                (Err(f), _, _, _)
                | (_, Err(f), _, _)
                | (_, _, Err(f), _)
                | (_, _, _, Err(f)) => return reject_numeric(f),
            };

        if order_notional > max_order + 1e-9 {
            return RiskDecision::Rejected(RiskRejection::OrderNotionalExceeded {
                order_notional: format_money(order_notional),
                limit: self.limits.max_order_notional.clone(),
            });
        }

        let projected_open = state.open_orders + 1;
        if projected_open > self.limits.max_open_orders {
            return RiskDecision::Rejected(RiskRejection::OpenOrdersExceeded {
                projected: projected_open,
                limit: self.limits.max_open_orders,
            });
        }

        if state.daily_realized_loss > max_loss + 1e-9 {
            return RiskDecision::Rejected(RiskRejection::DailyLossExceeded {
                realized: format_money(state.daily_realized_loss),
                limit: self.limits.max_daily_loss.clone(),
            });
        }

        let current = state
            .positions
            .get(&intent.instrument_id)
            .cloned()
            .unwrap_or_default();
        let signed_qty = match intent.side {
            Side::Buy => qty,
            Side::Sell => -qty,
        };
        let projected_qty = current.quantity + signed_qty;
        let projected_position_notional = projected_qty.abs() * price;
        if projected_position_notional > max_position + 1e-9 {
            return RiskDecision::Rejected(RiskRejection::PositionNotionalExceeded {
                projected: format_money(projected_position_notional),
                limit: self.limits.max_position_notional.clone(),
            });
        }

        let mut projected_gross = 0.0;
        for (instr, pos) in &state.positions {
            if instr == &intent.instrument_id {
                continue;
            }
            projected_gross += pos.quantity.abs() * pos.avg_price.max(price);
        }
        projected_gross += projected_position_notional;
        if projected_gross > max_gross + 1e-9 {
            return RiskDecision::Rejected(RiskRejection::GrossExposureExceeded {
                projected: format_money(projected_gross),
                limit: self.limits.max_gross_exposure.clone(),
            });
        }

        RiskDecision::Approved
    }
}

fn parse_or_return(s: &str, field: &'static str) -> Result<f64, &'static str> {
    s.parse::<f64>().map_err(|_| field)
}

fn reject_numeric(field: &'static str) -> RiskDecision {
    RiskDecision::Rejected(RiskRejection::InvalidNumeric {
        field: field.to_string(),
    })
}

fn format_money(v: f64) -> String {
    format!("{v:.2}")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn limits() -> RiskLimits {
        RiskLimits {
            max_order_notional: "500".into(),
            max_position_notional: "2500".into(),
            max_daily_loss: "250".into(),
            max_open_orders: 10,
            max_gross_exposure: "5000".into(),
            currency: "USD".into(),
            max_market_data_age_secs: 30,
        }
    }

    fn intent(side: Side, price: &str, qty: &str) -> IntentSnapshot {
        IntentSnapshot {
            client_order_id: "c-1".into(),
            instrument_id: "kalshi:M-1".into(),
            side,
            price: price.into(),
            quantity: qty.into(),
        }
    }

    #[test]
    fn approves_well_within_limits() {
        let gate = RiskGate::new(limits());
        let state = SleeveState::default();
        let dec = gate.evaluate(&state, &intent(Side::Buy, "0.5", "10"));
        assert_eq!(dec, RiskDecision::Approved);
    }

    #[test]
    fn rejects_order_notional_above_limit() {
        let gate = RiskGate::new(limits());
        let state = SleeveState::default();
        let dec = gate.evaluate(&state, &intent(Side::Buy, "0.5", "10000"));
        assert!(matches!(
            dec,
            RiskDecision::Rejected(RiskRejection::OrderNotionalExceeded { .. })
        ));
    }

    #[test]
    fn rejects_when_kill_switch_engaged() {
        let gate = RiskGate::new(limits());
        let state = SleeveState {
            kill_switch_engaged: true,
            ..Default::default()
        };
        let dec = gate.evaluate(&state, &intent(Side::Buy, "0.5", "10"));
        assert_eq!(dec, RiskDecision::Rejected(RiskRejection::KillSwitchEngaged));
    }

    #[test]
    fn rejects_stale_market_data() {
        let gate = RiskGate::new(limits());
        let mut market_data_age_secs = HashMap::new();
        market_data_age_secs.insert("kalshi:M-1".into(), 120);
        let state = SleeveState {
            market_data_age_secs,
            ..Default::default()
        };
        let dec = gate.evaluate(&state, &intent(Side::Buy, "0.5", "10"));
        assert!(matches!(
            dec,
            RiskDecision::Rejected(RiskRejection::StaleMarketData { age_secs: 120, .. })
        ));
    }

    #[test]
    fn rejects_open_order_cap_exceeded() {
        let gate = RiskGate::new(limits());
        let state = SleeveState {
            open_orders: 10,
            ..Default::default()
        };
        let dec = gate.evaluate(&state, &intent(Side::Buy, "0.5", "1"));
        assert!(matches!(
            dec,
            RiskDecision::Rejected(RiskRejection::OpenOrdersExceeded {
                projected: 11,
                limit: 10
            })
        ));
    }

    #[test]
    fn rejects_daily_loss_breached() {
        let gate = RiskGate::new(limits());
        let state = SleeveState {
            daily_realized_loss: 300.0,
            ..Default::default()
        };
        let dec = gate.evaluate(&state, &intent(Side::Buy, "0.5", "10"));
        assert!(matches!(
            dec,
            RiskDecision::Rejected(RiskRejection::DailyLossExceeded { .. })
        ));
    }

    #[test]
    fn rejects_projected_position_notional_breach() {
        let mut lim = limits();
        lim.max_order_notional = "10000".into();
        let gate = RiskGate::new(lim);
        let mut state = SleeveState::default();
        state.positions.insert(
            "kalshi:M-1".into(),
            Position {
                quantity: 4000.0,
                avg_price: 0.5,
            },
        );
        // 4000 + 1000 buys = 5000 contracts * 0.5 = 2500, exactly at limit (allowed).
        let approved = gate.evaluate(&state, &intent(Side::Buy, "0.5", "1000"));
        assert_eq!(approved, RiskDecision::Approved);
        // 4000 + 2000 buys = 6000 * 0.5 = 3000, exceeds 2500.
        let rejected = gate.evaluate(&state, &intent(Side::Buy, "0.5", "2000"));
        assert!(matches!(
            rejected,
            RiskDecision::Rejected(RiskRejection::PositionNotionalExceeded { .. })
        ));
    }

    #[test]
    fn sell_reduces_position_notional() {
        let mut lim = limits();
        lim.max_order_notional = "10000".into();
        let gate = RiskGate::new(lim);
        let mut state = SleeveState::default();
        state.positions.insert(
            "kalshi:M-1".into(),
            Position {
                quantity: 4000.0,
                avg_price: 0.5,
            },
        );
        let dec = gate.evaluate(&state, &intent(Side::Sell, "0.5", "2000"));
        assert_eq!(dec, RiskDecision::Approved);
    }

    #[test]
    fn rejects_gross_exposure_breach_across_instruments() {
        let mut lim = limits();
        lim.max_gross_exposure = "1000".into();
        lim.max_position_notional = "1000".into();
        lim.max_order_notional = "1000".into();
        let gate = RiskGate::new(lim);
        let mut state = SleeveState::default();
        state.positions.insert(
            "kalshi:OTHER".into(),
            Position {
                quantity: 1000.0,
                avg_price: 0.8,
            },
        );
        // Existing other instrument contributes 800 at max(avg, price)=0.8 -> 800.
        // New intent adds 500 on M-1 -> projected gross 1300, > 1000.
        let dec = gate.evaluate(&state, &intent(Side::Buy, "0.5", "1000"));
        assert!(matches!(
            dec,
            RiskDecision::Rejected(RiskRejection::GrossExposureExceeded { .. })
        ));
    }

    #[test]
    fn rejects_when_price_or_quantity_unparseable() {
        let gate = RiskGate::new(limits());
        let state = SleeveState::default();
        let dec = gate.evaluate(&state, &intent(Side::Buy, "abc", "1"));
        match dec {
            RiskDecision::Rejected(RiskRejection::InvalidNumeric { field }) => {
                assert_eq!(field, "price");
            }
            other => panic!("expected InvalidNumeric, got {other:?}"),
        }
    }
}
