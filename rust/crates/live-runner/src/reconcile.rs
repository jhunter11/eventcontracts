//! Production startup reconciliation: make local state match venue truth, and
//! refuse to trade if that truth already breaches risk.
//!
//! After a crash or restart the process must not size from an empty local book.
//! This module seeds the risk sleeve state from the venue's reported positions,
//! records the account balance and resting-order count, and asks the risk gate
//! whether the *adopted* state is within policy. If it is not, the runner halts
//! before placing a single order — a breached baseline is an operator decision,
//! not something to trade through.
//!
//! Daily realized loss is restored separately (and idempotently) by re-summing
//! the venue's fills since UTC midnight; the venue is authoritative there, so no
//! local accumulator is kept that could drift from it.

use eventcontracts_kalshi::KalshiPosition;
use eventcontracts_oms::OutcomeSide;
use eventcontracts_risk::{outcome_position_key, Position, RiskGate, SleeveState};

/// Seed venue positions into the risk sleeve state.
///
/// Kalshi reports a signed net `position` per market: positive = long YES,
/// negative = long NO. We split it into the side-specific position buckets the
/// risk gate keys on, using the venue's exposure as a conservative startup mark
/// until the first live quote arrives. Returns the number of seeded buckets.
pub fn seed_positions_into_state(state: &mut SleeveState, positions: &[KalshiPosition]) -> usize {
    let mut seeded = 0;
    for position in positions {
        let Some(contracts) = position.signed_contracts() else {
            continue;
        };
        if contracts == 0 {
            continue;
        }
        let Some(ticker) = position.ticker() else {
            continue;
        };
        let instrument = format!("kalshi:{ticker}");
        let (side, quantity) = if contracts > 0 {
            (OutcomeSide::Yes, contracts)
        } else {
            (OutcomeSide::No, -contracts)
        };
        state.positions.insert(
            outcome_position_key(&instrument, side),
            Position {
                quantity,
                avg_price_ticks: position.avg_price_ticks(),
            },
        );
        seeded += 1;
    }
    seeded
}

/// Outcome of startup reconciliation. Serializable so the operator gets a
/// durable diff record next to the run.
#[derive(Debug, Clone, Default, serde::Serialize)]
pub struct ReconcileReport {
    pub positions_seeded: usize,
    pub balance_ticks: i64,
    pub daily_realized_loss_ticks: i64,
    pub resting_orders_adopted: usize,
    pub resting_orders_cancelled: usize,
    /// Risk rejections produced by evaluating the adopted state. Empty == clean.
    pub risk_breaches: Vec<String>,
}

impl ReconcileReport {
    /// A clean reconcile has no risk breaches in the adopted baseline.
    pub fn is_clean(&self) -> bool {
        self.risk_breaches.is_empty()
    }

    /// Evaluate the adopted sleeve state and record any breaches. Returns
    /// whether the state is clean.
    pub fn evaluate_state(&mut self, risk: &RiskGate, state: &SleeveState) -> bool {
        self.risk_breaches = risk
            .evaluate_state_only(state)
            .iter()
            .map(|rejection| format!("{rejection:?}"))
            .collect();
        self.is_clean()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use eventcontracts_risk::RiskLimits;
    use serde_json::json;

    fn positions(value: serde_json::Value) -> Vec<KalshiPosition> {
        serde_json::from_value(value).unwrap()
    }

    #[test]
    fn seeds_yes_and_no_positions_with_correct_sign_and_mark() {
        let mut state = SleeveState::default();
        let seeded = seed_positions_into_state(
            &mut state,
            &positions(json!([
                {"ticker": "KXA", "position": 40, "market_exposure": 2000},
                {"ticker": "KXB", "position": -15, "market_exposure": 600},
                {"ticker": "KXFLAT", "position": 0}
            ])),
        );
        assert_eq!(seeded, 2); // the flat position is skipped

        let yes = state
            .positions
            .get(&outcome_position_key("kalshi:KXA", OutcomeSide::Yes))
            .expect("yes bucket");
        assert_eq!(yes.quantity, 40);
        assert_eq!(yes.avg_price_ticks, 5_000); // $20 / 40 = $0.50

        let no = state
            .positions
            .get(&outcome_position_key("kalshi:KXB", OutcomeSide::No))
            .expect("no bucket");
        assert_eq!(no.quantity, 15);
    }

    fn limits() -> RiskLimits {
        RiskLimits {
            max_order_notional: "500".into(),
            max_position_notional: "100".into(),
            max_daily_loss: "250".into(),
            max_open_orders: 10,
            max_gross_exposure: "150".into(),
            currency: "USD".into(),
            max_market_data_age_secs: 30,
        }
    }

    #[test]
    fn clean_when_adopted_state_is_within_limits() {
        let mut state = SleeveState::default();
        seed_positions_into_state(
            &mut state,
            &positions(json!([{"ticker": "KXA", "position": 10, "market_exposure": 500}])),
        );
        // 10 contracts * $0.50 = $5 notional, well under the $100 cap.
        let mut report = ReconcileReport::default();
        assert!(report.evaluate_state(&RiskGate::new(limits()), &state));
        assert!(report.is_clean());
    }

    #[test]
    fn breached_when_adopted_position_exceeds_limit() {
        let mut state = SleeveState::default();
        // 400 contracts * $0.50 = $200 notional > $100 position cap and > $150 gross.
        seed_positions_into_state(
            &mut state,
            &positions(json!([{"ticker": "KXA", "position": 400, "market_exposure": 20_000}])),
        );
        let mut report = ReconcileReport::default();
        assert!(!report.evaluate_state(&RiskGate::new(limits()), &state));
        assert!(!report.is_clean());
        assert!(!report.risk_breaches.is_empty());
    }
}
