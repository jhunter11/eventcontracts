//! Settlement-PnL backtester for the crypto ensemble.
//!
//! Mirrors `python/src/eventcontracts/crypto/backtest.py`. The
//! settlement model is identical: each `PlaceOrder` becomes a taker
//! fill at the observed Kalshi bid/ask, fees come from the Kalshi
//! per-fill formula, and payout is the venue's own `result` flag.
//!
//! Sizing policies match the Python ones:
//! * `FlatContracts` — fixed contract count
//! * `FixedPremium` — fixed maximum dollar loss per fill
//! * `FixedPayout` — fixed dollar win per winning fill

use std::collections::BTreeMap;

use chrono::{DateTime, Utc};
use eventcontracts_crypto_domain::{FilledTrade, OutcomeSide, PlaceOrder};
use eventcontracts_crypto_historical::{CohortSettlement, QuoteSample};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

// ----------------------------- Sizing policy -----------------------------

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "mode", rename_all = "snake_case")]
pub enum SizingPolicy {
    FlatContracts { contracts: Decimal },
    FixedPremium { dollars: Decimal },
    FixedPayout { dollars: Decimal },
}

impl SizingPolicy {
    pub fn quantity_for(&self, fill_price: Decimal, intent_quantity: Decimal) -> Decimal {
        match self {
            SizingPolicy::FlatContracts { contracts } => *contracts.max(&intent_quantity).min(contracts).max(&Decimal::ONE),
            SizingPolicy::FixedPremium { dollars } => {
                if fill_price <= Decimal::ZERO {
                    return Decimal::ONE;
                }
                let qty = (*dollars / fill_price).floor();
                qty.max(Decimal::ONE)
            }
            SizingPolicy::FixedPayout { dollars } => {
                let denom = Decimal::ONE - fill_price;
                if denom <= Decimal::ZERO {
                    return Decimal::ONE;
                }
                let qty = (*dollars / denom).floor();
                qty.max(Decimal::ONE)
            }
        }
    }
}

// ----------------------------- Fee model -----------------------------

/// Kalshi per-fill fee: `ceil(0.07 * price * (1 - price) * quantity * 100) / 100`
/// (rounded up to the cent, in USD). Maker fills are free.
pub fn kalshi_fee_usd(price: Decimal, quantity: Decimal, liquidity: &str) -> Decimal {
    if liquidity == "maker" {
        return Decimal::ZERO;
    }
    let rate = Decimal::new(7, 2); // 0.07
    let raw = rate * price * (Decimal::ONE - price) * quantity;
    
    (raw * Decimal::ONE_HUNDRED).ceil() / Decimal::ONE_HUNDRED
}

// ----------------------------- Cohort backtest -----------------------------

#[derive(Debug, Default, Clone)]
pub struct CohortBacktestResult {
    pub expiry_at: Option<DateTime<Utc>>,
    pub yes_market_ticker: Option<String>,
    pub settlement_price: Option<Decimal>,
    pub fills: Vec<FilledTrade>,
    pub skipped_no_quote: usize,
    pub skipped_unsettled: usize,
}

impl CohortBacktestResult {
    pub fn total_pnl(&self) -> Decimal {
        self.fills.iter().map(|f| f.pnl_total).sum()
    }

    pub fn total_fees(&self) -> Decimal {
        self.fills.iter().map(|f| f.fee_amount).sum()
    }

    pub fn settled_fills(&self) -> impl Iterator<Item = &FilledTrade> {
        self.fills.iter().filter(|f| f.settled)
    }

    pub fn win_rate(&self) -> f64 {
        let mut wins = 0usize;
        let mut total = 0usize;
        for f in self.settled_fills() {
            total += 1;
            if f.pnl_total > Decimal::ZERO {
                wins += 1;
            }
        }
        if total == 0 {
            0.0
        } else {
            wins as f64 / total as f64
        }
    }
}

#[derive(Debug, Default, Clone)]
pub struct WalkForwardReport {
    pub cohorts: Vec<CohortBacktestResult>,
}

impl WalkForwardReport {
    pub fn total_pnl(&self) -> Decimal {
        self.cohorts.iter().map(|c| c.total_pnl()).sum()
    }

    pub fn total_fees(&self) -> Decimal {
        self.cohorts.iter().map(|c| c.total_fees()).sum()
    }

    pub fn total_fills(&self) -> usize {
        self.cohorts.iter().map(|c| c.fills.len()).sum()
    }

    pub fn total_settled_fills(&self) -> usize {
        self.cohorts.iter().map(|c| c.settled_fills().count()).sum()
    }

    pub fn win_rate(&self) -> f64 {
        let mut wins = 0usize;
        let mut total = 0usize;
        for c in &self.cohorts {
            for f in c.settled_fills() {
                total += 1;
                if f.pnl_total > Decimal::ZERO {
                    wins += 1;
                }
            }
        }
        if total == 0 {
            0.0
        } else {
            wins as f64 / total as f64
        }
    }
}

/// Apply one PlaceOrder against the latest observed quote and settlement.
///
/// Returns `None` if no quote is known for the market — strategies must
/// not get filled in markets they haven't seen.
pub fn fill_decision(
    decision: &PlaceOrder,
    decision_at: DateTime<Utc>,
    latest_quote: &BTreeMap<String, QuoteSample>,
    settlement: &CohortSettlement,
    sizing: &SizingPolicy,
    sources: &[String],
) -> Option<FilledTrade> {
    let market_id = decision.instrument_id.market_id.clone();
    let q = latest_quote.get(&market_id)?;
    let fill_price = match decision.outcome_side {
        OutcomeSide::Yes => {
            if q.ask <= Decimal::ZERO || q.ask >= Decimal::ONE {
                return None;
            }
            q.ask
        }
        OutcomeSide::No => {
            if q.bid < Decimal::ZERO || q.bid >= Decimal::ONE {
                return None;
            }
            let p = Decimal::ONE - q.bid;
            if p <= Decimal::ZERO {
                return None;
            }
            p
        }
    };
    let quantity = sizing.quantity_for(fill_price, decision.quantity);
    let fee_total = kalshi_fee_usd(fill_price, quantity, "taker");
    let fee_per_contract = if quantity > Decimal::ZERO {
        fee_total / quantity
    } else {
        Decimal::ZERO
    };
    let result = settlement.bracket_results.get(&market_id);
    let (payout, pnl_per_contract, settled) = match result {
        Some(r) => {
            let won = r == decision.outcome_side.as_str();
            let payout = if won { Decimal::ONE } else { Decimal::ZERO };
            (payout, payout - fill_price - fee_per_contract, true)
        }
        None => (Decimal::ZERO, Decimal::ZERO, false),
    };
    Some(FilledTrade {
        market_id,
        outcome_side: decision.outcome_side,
        fill_price,
        quantity,
        fee_amount: fee_total,
        fee_currency: "USD".into(),
        payout_per_contract: payout,
        pnl_per_contract,
        pnl_total: pnl_per_contract * quantity,
        sources: sources.to_vec(),
        decision_at,
        settled,
    })
}

// ----------------------------- Tests -----------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use eventcontracts_crypto_domain::{
        ClientOrderId, InstrumentId, OrderSide, OrderType, TimeInForce, Venue,
    };
    use std::str::FromStr;

    fn d(s: &str) -> Decimal {
        Decimal::from_str(s).unwrap()
    }

    fn make_quote(market: &str, bid: &str, ask: &str) -> QuoteSample {
        QuoteSample {
            market_id: market.into(),
            timestamp: Utc::now(),
            bid: d(bid),
            ask: d(ask),
        }
    }

    fn make_decision(market: &str, side: OutcomeSide) -> PlaceOrder {
        PlaceOrder {
            client_order_id: ClientOrderId::new("co-1"),
            instrument_id: InstrumentId::new(Venue::Kalshi, market),
            outcome_side: side,
            order_side: OrderSide::Buy,
            order_type: OrderType::Limit,
            time_in_force: TimeInForce::Gtc,
            quantity: d("1"),
            price: None,
            reason: String::new(),
            expected_edge_bps: None,
        }
    }

    #[test]
    fn fixed_premium_caps_loss() {
        let sizing = SizingPolicy::FixedPremium { dollars: d("1") };
        // At fill price $0.05 a $1 budget = 20 contracts.
        assert_eq!(sizing.quantity_for(d("0.05"), d("0")), d("20"));
        // At fill price $0.95 a $1 budget = 1 contract.
        assert_eq!(sizing.quantity_for(d("0.95"), d("0")), d("1"));
    }

    #[test]
    fn fixed_payout_equalizes_upside() {
        let sizing = SizingPolicy::FixedPayout { dollars: d("1") };
        // win = (1 - fill_price) * qty = 1 → qty = 1 / (1 - 0.5) = 2
        assert_eq!(sizing.quantity_for(d("0.5"), d("0")), d("2"));
    }

    #[test]
    fn kalshi_fee_at_atm() {
        // 0.07 * 0.5 * 0.5 * 100 = $1.75
        assert_eq!(kalshi_fee_usd(d("0.5"), d("100"), "taker"), d("1.75"));
    }

    #[test]
    fn maker_pays_no_fee() {
        assert_eq!(kalshi_fee_usd(d("0.5"), d("100"), "maker"), Decimal::ZERO);
    }

    #[test]
    fn buy_yes_wins_when_market_settles_yes() {
        let mut quotes = BTreeMap::new();
        quotes.insert("M".into(), make_quote("M", "0.30", "0.32"));
        let mut settlement = CohortSettlement::default();
        settlement.bracket_results.insert("M".into(), "yes".into());
        let decision = make_decision("M", OutcomeSide::Yes);
        let sizing = SizingPolicy::FixedPremium { dollars: d("1") };
        let fill = fill_decision(&decision, Utc::now(), &quotes, &settlement, &sizing, &[])
            .expect("should fill");
        assert!(fill.settled);
        // Fill at ask 0.32; payout 1; PnL/contract ≈ 1 - 0.32 - fee
        assert!(fill.pnl_per_contract > Decimal::ZERO);
        // qty = floor(1 / 0.32) = 3
        assert_eq!(fill.quantity, d("3"));
    }

    #[test]
    fn buy_no_loses_when_market_settles_yes() {
        let mut quotes = BTreeMap::new();
        quotes.insert("M".into(), make_quote("M", "0.30", "0.32"));
        let mut settlement = CohortSettlement::default();
        settlement.bracket_results.insert("M".into(), "yes".into());
        let decision = make_decision("M", OutcomeSide::No);
        let sizing = SizingPolicy::FixedPremium { dollars: d("1") };
        let fill = fill_decision(&decision, Utc::now(), &quotes, &settlement, &sizing, &[])
            .expect("should fill");
        assert!(fill.settled);
        assert_eq!(fill.payout_per_contract, Decimal::ZERO);
        assert!(fill.pnl_per_contract < Decimal::ZERO);
    }

    #[test]
    fn fill_returns_none_when_no_quote() {
        let quotes = BTreeMap::new();
        let settlement = CohortSettlement::default();
        let decision = make_decision("M", OutcomeSide::Yes);
        let sizing = SizingPolicy::FixedPremium { dollars: d("1") };
        let result = fill_decision(&decision, Utc::now(), &quotes, &settlement, &sizing, &[]);
        assert!(result.is_none());
    }
}
