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
//! fixed-point integers on the hot path to avoid f64 drift.

pub mod fees;

use eventcontracts_oms::{OutcomeSide, Side};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use thiserror::Error;

const SCALE: i128 = 10_000;
pub const PRICE_TICKS_ONE: i64 = 10_000;

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
    pub max_open_orders: u64,
    pub max_gross_exposure: String,
    pub currency: String,
    /// Maximum age in seconds a market-data observation can have before the
    /// runner refuses to act on it.
    pub max_market_data_age_secs: u32,
}

#[derive(Clone, Debug, Default)]
pub struct SleeveState {
    pub open_orders: u64,
    /// Per-instrument signed position (+long, -short) and average price. The
    /// quantity is whole contracts; `avg_price_ticks` is dollars * 10_000.
    pub positions: HashMap<String, Position>,
    /// Daily realized loss in dollars * 10_000. Reset at UTC midnight by the
    /// runner; growth here folds in both fees and realized cash P&L on fills.
    pub daily_realized_loss: i64,
    /// Current liquidation-mark drawdown in dollars * 10_000. This is not
    /// realized P&L; it is the positive loss implied by marking open
    /// positions to executable exit prices. The daily-loss gate uses
    /// realized + this drawdown so held-to-expiry positions cannot bleed
    /// intraday without tripping policy.
    pub unrealized_drawdown_loss: i64,
    pub kill_switch_engaged: bool,
    /// Per-instrument **epoch seconds at which the last BBO observation was
    /// received**. Risk computes freshness on demand by subtracting this from
    /// `now_epoch_secs` at `evaluate` time. Storing the timestamp instead of
    /// the age means the freshness gate fires even when no further quotes
    /// arrive (which was the silent-no-op bug).
    pub last_quote_epoch_secs: HashMap<String, i64>,
    /// Per-instrument mark price in dollars * 10_000. Updated from fresh BBO.
    pub mark_price_ticks: HashMap<String, i64>,
    /// Side-specific best bid in dollars * 10_000. Keys are produced by
    /// `outcome_position_key`, with a legacy bare instrument fallback.
    pub best_bid_ticks: HashMap<String, i64>,
    /// Side-specific best ask in dollars * 10_000. Keys are produced by
    /// `outcome_position_key`, with a legacy bare instrument fallback.
    pub best_ask_ticks: HashMap<String, i64>,
    /// Side-specific best bid displayed quantity in contracts * 10_000.
    /// Missing means the gateway cannot prove L1 capacity.
    pub best_bid_qty_ticks: HashMap<String, i128>,
    /// Side-specific best ask displayed quantity in contracts * 10_000.
    /// Missing means the gateway cannot prove L1 capacity.
    pub best_ask_qty_ticks: HashMap<String, i128>,
    /// UTC day for which `daily_realized_loss` accumulates. Caller writes
    /// `today_utc_day` before each evaluate; on day rollover the loss is reset.
    pub daily_loss_day_utc: i32,
    /// Available settled cash in dollars * 10_000. `None` means "unknown" and
    /// the cash gate is skipped (paper, or a failed balance fetch). `Some(v)`
    /// is enforced: a BUY whose notional exceeds it is rejected. Seeded from the
    /// venue balance at startup reconciliation and maintained on every fill
    /// (buys debit, sells credit, fees debit) by the gateway.
    pub available_cash_ticks: Option<i64>,
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct Position {
    pub quantity: i64,
    pub avg_price_ticks: i64,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct IntentSnapshot {
    pub client_order_id: String,
    pub instrument_id: String,
    #[serde(default = "default_outcome_side")]
    pub outcome_side: OutcomeSide,
    pub side: Side,
    pub price: String,
    pub quantity: String,
    /// Optional strategy/model fair value in dollars. When present, the risk
    /// gate rejects orders whose expected edge after taker fees is below
    /// `min_executable_edge_ticks`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub fair_price: Option<String>,
    /// Minimum acceptable post-fee edge in 4-decimal price ticks.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub min_executable_edge_ticks: Option<i64>,
    /// Venue fee curve in basis points. Defaults to Kalshi's 7% taker curve.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub fee_rate_bps: Option<u32>,
}

fn default_outcome_side() -> OutcomeSide {
    OutcomeSide::Yes
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
        projected: u64,
        limit: u64,
    },
    DailyLossExceeded {
        realized: String,
        limit: String,
    },
    InsufficientCash {
        required: String,
        available: String,
    },
    KillSwitchEngaged,
    StaleMarketData {
        instrument_id: String,
        age_secs: u32,
        limit_secs: u32,
    },
    MissingMarketData {
        instrument_id: String,
    },
    RateLimitExceeded {
        action: String,
        limit_per_second: u32,
    },
    NegativeEdgeAfterFees {
        expected_edge_ticks: i64,
        minimum_edge_ticks: i64,
        fee_ticks_per_contract: i64,
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

    /// Validate the current sleeve state without adding a new intent.
    ///
    /// Startup reconciliation uses this after adopting venue truth. If the
    /// venue already has exposure outside the configured policy, the caller
    /// should halt new trading rather than silently running from a breached
    /// baseline.
    pub fn evaluate_state_only(&self, state: &SleeveState) -> Vec<RiskRejection> {
        let max_position =
            parse_or_return(&self.limits.max_position_notional, "max_position_notional");
        let max_gross = parse_or_return(&self.limits.max_gross_exposure, "max_gross_exposure");
        let max_loss = parse_or_return(&self.limits.max_daily_loss, "max_daily_loss");
        let (max_position, max_gross, max_loss) = match (max_position, max_gross, max_loss) {
            (Ok(a), Ok(b), Ok(c)) => (a, b, c),
            (Err(f), _, _) | (_, Err(f), _) | (_, _, Err(f)) => {
                return vec![RiskRejection::InvalidNumeric {
                    field: f.to_string(),
                }]
            }
        };

        let mut rejections = Vec::new();
        if state.kill_switch_engaged {
            rejections.push(RiskRejection::KillSwitchEngaged);
        }
        if state.open_orders > self.limits.max_open_orders {
            rejections.push(RiskRejection::OpenOrdersExceeded {
                projected: state.open_orders,
                limit: self.limits.max_open_orders,
            });
        }
        let total_daily_loss = i128::from(state.daily_realized_loss)
            .saturating_add(i128::from(state.unrealized_drawdown_loss.max(0)));
        if max_loss > 0 && total_daily_loss >= max_loss {
            rejections.push(RiskRejection::DailyLossExceeded {
                realized: format_money(total_daily_loss),
                limit: self.limits.max_daily_loss.clone(),
            });
        }

        let mut gross = 0_i128;
        for (key, pos) in &state.positions {
            let qty = i128::from(pos.quantity).saturating_mul(SCALE).abs();
            let price = state
                .mark_price_ticks
                .get(key)
                .copied()
                .map(i128::from)
                .unwrap_or_else(|| i128::from(pos.avg_price_ticks));
            let notional = mul_fixed(qty, price.abs());
            gross = gross.saturating_add(notional);
            if notional > max_position {
                rejections.push(RiskRejection::PositionNotionalExceeded {
                    projected: format_money(notional),
                    limit: self.limits.max_position_notional.clone(),
                });
            }
        }
        if gross > max_gross {
            rejections.push(RiskRejection::GrossExposureExceeded {
                projected: format_money(gross),
                limit: self.limits.max_gross_exposure.clone(),
            });
        }

        rejections
    }

    /// Evaluate an intent against a sleeve snapshot at a given wall clock.
    ///
    /// `now_epoch_secs` is required so the freshness gate is reactive to
    /// elapsed wall-time rather than the writer's last-seen self-report. A
    /// stale market gets caught even when no further quotes arrive.
    pub fn evaluate(
        &self,
        state: &SleeveState,
        intent: &IntentSnapshot,
        now_epoch_secs: i64,
    ) -> RiskDecision {
        if state.kill_switch_engaged {
            return RiskDecision::Rejected(RiskRejection::KillSwitchEngaged);
        }
        let market_data_key = outcome_position_key(&intent.instrument_id, intent.outcome_side);
        let Some(last_seen) = state
            .last_quote_epoch_secs
            .get(&market_data_key)
            .or_else(|| state.last_quote_epoch_secs.get(&intent.instrument_id))
        else {
            return RiskDecision::Rejected(RiskRejection::MissingMarketData {
                instrument_id: intent.instrument_id.clone(),
            });
        };
        let age = now_epoch_secs.saturating_sub(*last_seen).max(0) as u32;
        if age > self.limits.max_market_data_age_secs {
            return RiskDecision::Rejected(RiskRejection::StaleMarketData {
                instrument_id: intent.instrument_id.clone(),
                age_secs: age,
                limit_secs: self.limits.max_market_data_age_secs,
            });
        }
        let price = match parse_fixed(&intent.price) {
            Ok(v) => v,
            Err(_) => return reject_numeric("price"),
        };
        let qty = match parse_fixed(&intent.quantity) {
            Ok(v) => v,
            Err(_) => return reject_numeric("quantity"),
        };
        let order_notional = mul_fixed(price, qty);

        // Compute the current position early so the daily-loss gate can let a
        // risk-reducing exit through. A SELL that reduces an existing long is an
        // exit; blocking it on a loss breach would prevent de-risking. This
        // mirrors the Python soft-halt, which always permits exits (V6-T1/T2).
        let position_key = outcome_position_key(&intent.instrument_id, intent.outcome_side);
        let current = state
            .positions
            .get(&position_key)
            .cloned()
            .unwrap_or_default();
        let is_risk_reducing = matches!(intent.side, Side::Sell) && current.quantity > 0;

        let max_order = parse_or_return(&self.limits.max_order_notional, "max_order_notional");
        let max_position =
            parse_or_return(&self.limits.max_position_notional, "max_position_notional");
        let max_gross = parse_or_return(&self.limits.max_gross_exposure, "max_gross_exposure");
        let max_loss = parse_or_return(&self.limits.max_daily_loss, "max_daily_loss");
        let (max_order, max_position, max_gross, max_loss) =
            match (max_order, max_position, max_gross, max_loss) {
                (Ok(a), Ok(b), Ok(c), Ok(d)) => (a, b, c, d),
                (Err(f), _, _, _) | (_, Err(f), _, _) | (_, _, Err(f), _) | (_, _, _, Err(f)) => {
                    return reject_numeric(f)
                }
            };

        if order_notional > max_order {
            return RiskDecision::Rejected(RiskRejection::OrderNotionalExceeded {
                order_notional: format_money(order_notional),
                limit: self.limits.max_order_notional.clone(),
            });
        }

        // Available-cash gate (BUYs only). A SELL closes inventory and returns
        // cash on a binary venue, so it never needs buying power. When cash is
        // unknown (`None`, e.g. paper or a failed balance fetch) the gate is
        // skipped and the notional/gross caps remain the only bound. Mirrors the
        // Python `check_available_cash`.
        if matches!(intent.side, Side::Buy) {
            if let Some(available) = state.available_cash_ticks {
                if order_notional > i128::from(available) {
                    return RiskDecision::Rejected(RiskRejection::InsufficientCash {
                        required: format_money(order_notional),
                        available: format_money(i128::from(available)),
                    });
                }
            }
        }

        if let Some(fair_raw) = intent.fair_price.as_deref() {
            let fair = match parse_fixed(fair_raw) {
                Ok(v) => v,
                Err(_) => return reject_numeric("fair_price"),
            };
            let fee_per_contract =
                taker_fee_per_contract_ticks(price, qty, intent.fee_rate_bps.unwrap_or(700));
            let expected_edge = match intent.side {
                Side::Buy => fair.saturating_sub(price),
                Side::Sell => price.saturating_sub(fair),
            }
            .saturating_sub(fee_per_contract);
            let minimum = i128::from(intent.min_executable_edge_ticks.unwrap_or(0));
            if expected_edge < minimum {
                return RiskDecision::Rejected(RiskRejection::NegativeEdgeAfterFees {
                    expected_edge_ticks: clamp_i128_to_i64(expected_edge),
                    minimum_edge_ticks: clamp_i128_to_i64(minimum),
                    fee_ticks_per_contract: clamp_i128_to_i64(fee_per_contract),
                });
            }
        }

        let projected_open = state.open_orders + 1;
        if projected_open > self.limits.max_open_orders {
            return RiskDecision::Rejected(RiskRejection::OpenOrdersExceeded {
                projected: projected_open,
                limit: self.limits.max_open_orders,
            });
        }

        let total_daily_loss = i128::from(state.daily_realized_loss)
            .saturating_add(i128::from(state.unrealized_drawdown_loss.max(0)));
        if max_loss > 0 && total_daily_loss >= max_loss && !is_risk_reducing {
            return RiskDecision::Rejected(RiskRejection::DailyLossExceeded {
                realized: format_money(total_daily_loss),
                limit: self.limits.max_daily_loss.clone(),
            });
        }

        let signed_qty = match intent.side {
            Side::Buy => qty,
            Side::Sell => -qty,
        };
        let projected_qty = i128::from(current.quantity).saturating_mul(SCALE) + signed_qty;
        let projected_position_notional = mul_fixed(projected_qty.abs(), price);
        if projected_position_notional > max_position {
            return RiskDecision::Rejected(RiskRejection::PositionNotionalExceeded {
                projected: format_money(projected_position_notional),
                limit: self.limits.max_position_notional.clone(),
            });
        }

        let mut projected_gross = 0;
        for (key, pos) in &state.positions {
            if key == &position_key {
                continue;
            }
            let pos_qty = i128::from(pos.quantity).saturating_mul(SCALE).abs();
            let pos_price = state
                .mark_price_ticks
                .get(key)
                .copied()
                .map(i128::from)
                .unwrap_or_else(|| i128::from(pos.avg_price_ticks));
            projected_gross += mul_fixed(pos_qty, pos_price);
        }
        projected_gross += projected_position_notional;
        if projected_gross > max_gross {
            return RiskDecision::Rejected(RiskRejection::GrossExposureExceeded {
                projected: format_money(projected_gross),
                limit: self.limits.max_gross_exposure.clone(),
            });
        }

        RiskDecision::Approved
    }
}

pub fn outcome_position_key(instrument_id: &str, outcome_side: OutcomeSide) -> String {
    let suffix = match outcome_side {
        OutcomeSide::Yes => "yes",
        OutcomeSide::No => "no",
    };
    format!("{instrument_id}|{suffix}")
}

pub fn record_quote_bbo(
    state: &mut SleeveState,
    instrument_id: &str,
    bid_ticks: i64,
    ask_ticks: i64,
    now_epoch_secs: i64,
) {
    let yes_key = outcome_position_key(instrument_id, OutcomeSide::Yes);
    let no_key = outcome_position_key(instrument_id, OutcomeSide::No);
    let mark = (bid_ticks.saturating_add(ask_ticks)) / 2;
    let no_bid = PRICE_TICKS_ONE.saturating_sub(ask_ticks);
    let no_ask = PRICE_TICKS_ONE.saturating_sub(bid_ticks);
    let no_mark = PRICE_TICKS_ONE.saturating_sub(mark);

    for key in [instrument_id.to_string(), yes_key.clone(), no_key.clone()] {
        state.best_bid_qty_ticks.remove(&key);
        state.best_ask_qty_ticks.remove(&key);
        state.last_quote_epoch_secs.insert(key, now_epoch_secs);
    }
    state.best_bid_ticks.insert(yes_key.clone(), bid_ticks);
    state.best_ask_ticks.insert(yes_key.clone(), ask_ticks);
    state.mark_price_ticks.insert(yes_key, mark);
    state.best_bid_ticks.insert(no_key.clone(), no_bid);
    state.best_ask_ticks.insert(no_key.clone(), no_ask);
    state.mark_price_ticks.insert(no_key, no_mark);

    // Legacy bare-instrument mark keeps old callers/tests working while live
    // risk and accounting consume side-specific keys.
    state
        .best_bid_ticks
        .insert(instrument_id.to_string(), bid_ticks);
    state
        .best_ask_ticks
        .insert(instrument_id.to_string(), ask_ticks);
    state
        .mark_price_ticks
        .insert(instrument_id.to_string(), mark);
}

pub fn record_book_bbo(
    state: &mut SleeveState,
    instrument_id: &str,
    bid_ticks: i64,
    bid_qty: u32,
    ask_ticks: i64,
    ask_qty: u32,
    now_epoch_secs: i64,
) {
    record_quote_bbo(state, instrument_id, bid_ticks, ask_ticks, now_epoch_secs);
    let yes_key = outcome_position_key(instrument_id, OutcomeSide::Yes);
    let no_key = outcome_position_key(instrument_id, OutcomeSide::No);
    let bid_qty_ticks = i128::from(bid_qty).saturating_mul(SCALE);
    let ask_qty_ticks = i128::from(ask_qty).saturating_mul(SCALE);

    state
        .best_bid_qty_ticks
        .insert(yes_key.clone(), bid_qty_ticks);
    state.best_ask_qty_ticks.insert(yes_key, ask_qty_ticks);
    state
        .best_bid_qty_ticks
        .insert(no_key.clone(), ask_qty_ticks);
    state.best_ask_qty_ticks.insert(no_key, bid_qty_ticks);
    state
        .best_bid_qty_ticks
        .insert(instrument_id.to_string(), bid_qty_ticks);
    state
        .best_ask_qty_ticks
        .insert(instrument_id.to_string(), ask_qty_ticks);
}

pub fn invalidate_quote_bbo(state: &mut SleeveState, instrument_id: &str, now_epoch_secs: i64) {
    let keys = [
        instrument_id.to_string(),
        outcome_position_key(instrument_id, OutcomeSide::Yes),
        outcome_position_key(instrument_id, OutcomeSide::No),
    ];
    for key in keys {
        state
            .last_quote_epoch_secs
            .insert(key.clone(), now_epoch_secs);
        state.best_bid_ticks.remove(&key);
        state.best_ask_ticks.remove(&key);
        state.best_bid_qty_ticks.remove(&key);
        state.best_ask_qty_ticks.remove(&key);
        state.mark_price_ticks.remove(&key);
    }
}

/// Net liquidation-mark unrealized drawdown across all open positions, in
/// dollars * 10_000 (>= 0; gains net against losses and a net gain returns 0).
///
/// Each position is marked to the price it would *exit* into: a long (qty > 0)
/// to its side-specific best bid, a short (qty < 0) to its best ask. A position
/// with no recorded executable quote keeps its entry price (no mark move), so a
/// stale book never manufactures phantom drawdown. The result is written into
/// `SleeveState::unrealized_drawdown_loss`, which the daily-loss gate folds in
/// alongside realized loss — so a held position bleeding intraday counts toward
/// `max_daily_loss` before it is realized at settlement.
pub fn liquidation_unrealized_drawdown_ticks(state: &SleeveState) -> i64 {
    let mut net: i128 = 0;
    for (key, pos) in &state.positions {
        if pos.quantity == 0 {
            continue;
        }
        let exit_ticks = if pos.quantity > 0 {
            state.best_bid_ticks.get(key)
        } else {
            state.best_ask_ticks.get(key)
        }
        .copied()
        .unwrap_or(pos.avg_price_ticks);
        let pnl = i128::from(exit_ticks.saturating_sub(pos.avg_price_ticks))
            .saturating_mul(i128::from(pos.quantity));
        net = net.saturating_add(pnl);
    }
    if net < 0 {
        clamp_i128_to_i64(-net)
    } else {
        0
    }
}

/// Parse an RFC3339 `YYYY-MM-DDTHH:MM:SS[.frac][Z|±HH:MM]` string into UNIX
/// epoch seconds. Sub-second precision is truncated. Returns 0 for malformed
/// input — callers treat the result as "earliest possible time", which is
/// the safest default for a freshness gate (the intent is then trivially
/// stale).
///
/// Lives here so risk, runner, and live-runner don't each maintain their own
/// copy of this small parser.
pub fn epoch_seconds_from_rfc3339(ts: &str) -> i64 {
    if ts.len() < 19 {
        return 0;
    }
    let year: i64 = match ts[0..4].parse() {
        Ok(v) => v,
        Err(_) => return 0,
    };
    let month: i64 = match ts[5..7].parse() {
        Ok(v) => v,
        Err(_) => return 0,
    };
    let day: i64 = match ts[8..10].parse() {
        Ok(v) => v,
        Err(_) => return 0,
    };
    let hour: i64 = match ts[11..13].parse() {
        Ok(v) => v,
        Err(_) => return 0,
    };
    let minute: i64 = match ts[14..16].parse() {
        Ok(v) => v,
        Err(_) => return 0,
    };
    let second: i64 = match ts[17..19].parse() {
        Ok(v) => v,
        Err(_) => return 0,
    };
    // civil-from-days, after Howard Hinnant.
    let y = if month <= 2 { year - 1 } else { year };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let m = month;
    let doy = (153 * (if m > 2 { m - 3 } else { m + 9 }) + 2) / 5 + day - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    let days = era * 146097 + doe - 719468;
    days * 86_400 + hour * 3600 + minute * 60 + second
}

/// UTC day number (days since UNIX epoch) for a given epoch-second timestamp.
/// Used to detect day rollover for daily-loss reset.
pub fn utc_day_from_epoch_secs(epoch_secs: i64) -> i32 {
    (epoch_secs.div_euclid(86_400)) as i32
}

fn parse_or_return(s: &str, field: &'static str) -> Result<i128, &'static str> {
    parse_fixed(s).map_err(|_| field)
}

fn reject_numeric(field: &'static str) -> RiskDecision {
    RiskDecision::Rejected(RiskRejection::InvalidNumeric {
        field: field.to_string(),
    })
}

fn parse_fixed(s: &str) -> Result<i128, ()> {
    if s.is_empty() {
        return Err(());
    }
    let negative = s.starts_with('-');
    let body = if negative { &s[1..] } else { s };
    if body.is_empty() {
        return Err(());
    }
    let mut parts = body.split('.');
    let whole_raw = parts.next().ok_or(())?;
    let frac_raw = parts.next();
    if parts.next().is_some() {
        return Err(());
    }
    if whole_raw.is_empty() || !whole_raw.bytes().all(|b| b.is_ascii_digit()) {
        return Err(());
    }
    let whole = whole_raw.parse::<i128>().map_err(|_| ())?;
    let frac = match frac_raw {
        None => 0,
        Some(raw) => {
            if raw.len() > 4 || raw.bytes().any(|b| !b.is_ascii_digit()) {
                return Err(());
            }
            let mut padded = raw.to_string();
            while padded.len() < 4 {
                padded.push('0');
            }
            padded.parse::<i128>().map_err(|_| ())?
        }
    };
    let value = whole
        .checked_mul(SCALE)
        .and_then(|v| v.checked_add(frac))
        .ok_or(())?;
    Ok(if negative { -value } else { value })
}

fn mul_fixed(left: i128, right: i128) -> i128 {
    left.saturating_mul(right) / SCALE
}

fn taker_fee_per_contract_ticks(price_ticks: i128, quantity_ticks: i128, rate_bps: u32) -> i128 {
    if quantity_ticks <= 0 {
        return 0;
    }
    let contracts = div_ceil_i128(quantity_ticks, SCALE).max(1);
    let price_i64 = clamp_i128_to_i64(price_ticks);
    let contracts_i64 = clamp_i128_to_i64(contracts);
    let total_fee = fees::kalshi_taker_fee_ticks(price_i64, contracts_i64, rate_bps).max(0) as i128;
    div_ceil_i128(total_fee, contracts)
}

fn div_ceil_i128(numerator: i128, denominator: i128) -> i128 {
    if numerator <= 0 || denominator <= 0 {
        return 0;
    }
    numerator
        .saturating_add(denominator.saturating_sub(1))
        .saturating_div(denominator)
}

fn clamp_i128_to_i64(value: i128) -> i64 {
    value.clamp(i128::from(i64::MIN), i128::from(i64::MAX)) as i64
}

fn format_money(value: i128) -> String {
    let sign = if value < 0 { "-" } else { "" };
    let abs = value.abs();
    let whole = abs / SCALE;
    let cents = ((abs % SCALE) * 100) / SCALE;
    format!("{sign}{whole}.{cents:02}")
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
            outcome_side: OutcomeSide::Yes,
            side,
            price: price.into(),
            quantity: qty.into(),
            fair_price: None,
            min_executable_edge_ticks: None,
            fee_rate_bps: None,
        }
    }

    const NOW: i64 = 1_700_000_000;

    fn fresh_state() -> SleeveState {
        let mut state = SleeveState::default();
        record_quote_bbo(&mut state, "kalshi:M-1", 4900, 5100, NOW);
        state
    }

    #[test]
    fn approves_well_within_limits() {
        let gate = RiskGate::new(limits());
        let state = fresh_state();
        let dec = gate.evaluate(&state, &intent(Side::Buy, "0.5", "10"), NOW);
        assert_eq!(dec, RiskDecision::Approved);
    }

    #[test]
    fn rejects_order_notional_above_limit() {
        let gate = RiskGate::new(limits());
        let state = fresh_state();
        let dec = gate.evaluate(&state, &intent(Side::Buy, "0.5", "10000"), NOW);
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
            ..fresh_state()
        };
        let dec = gate.evaluate(&state, &intent(Side::Buy, "0.5", "10"), NOW);
        assert_eq!(
            dec,
            RiskDecision::Rejected(RiskRejection::KillSwitchEngaged)
        );
    }

    #[test]
    fn rejects_stale_market_data_by_elapsed_wallclock() {
        let gate = RiskGate::new(limits());
        // Last quote 120 seconds ago, limit is 30.
        let mut state = SleeveState::default();
        state.last_quote_epoch_secs.insert(
            outcome_position_key("kalshi:M-1", OutcomeSide::Yes),
            NOW - 120,
        );
        let dec = gate.evaluate(&state, &intent(Side::Buy, "0.5", "10"), NOW);
        assert!(matches!(
            dec,
            RiskDecision::Rejected(RiskRejection::StaleMarketData { age_secs: 120, .. })
        ));
    }

    #[test]
    fn rejects_missing_market_data() {
        let gate = RiskGate::new(limits());
        let state = SleeveState::default();
        let dec = gate.evaluate(&state, &intent(Side::Buy, "0.5", "10"), NOW);
        assert!(matches!(
            dec,
            RiskDecision::Rejected(RiskRejection::MissingMarketData { .. })
        ));
    }

    #[test]
    fn rejects_open_order_cap_exceeded() {
        let gate = RiskGate::new(limits());
        let state = SleeveState {
            open_orders: 10,
            ..fresh_state()
        };
        let dec = gate.evaluate(&state, &intent(Side::Buy, "0.5", "1"), NOW);
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
            daily_realized_loss: 300 * SCALE as i64,
            ..fresh_state()
        };
        let dec = gate.evaluate(&state, &intent(Side::Buy, "0.5", "10"), NOW);
        assert!(matches!(
            dec,
            RiskDecision::Rejected(RiskRejection::DailyLossExceeded { .. })
        ));
    }

    #[test]
    fn rejects_daily_loss_from_unrealized_liquidation_drawdown() {
        let gate = RiskGate::new(limits());
        let state = SleeveState {
            daily_realized_loss: 0,
            unrealized_drawdown_loss: 250 * SCALE as i64,
            ..fresh_state()
        };
        let dec = gate.evaluate(&state, &intent(Side::Buy, "0.5", "1"), NOW);
        assert_eq!(
            dec,
            RiskDecision::Rejected(RiskRejection::DailyLossExceeded {
                realized: "250.00".into(),
                limit: "250".into(),
            })
        );
    }

    #[test]
    fn allows_risk_reducing_sell_during_daily_loss_breach() {
        // V6-T1/T2 parity: an exit (SELL reducing a long) is never blocked by a
        // daily-loss breach, while a BUY in the same state is rejected.
        let gate = RiskGate::new(limits());
        let mut state = fresh_state();
        state.daily_realized_loss = 300 * SCALE as i64; // cap is 250 -> breached
        state.positions.insert(
            outcome_position_key("kalshi:M-1", OutcomeSide::Yes),
            Position {
                quantity: 100,
                avg_price_ticks: 5000,
            },
        );
        let sell = gate.evaluate(&state, &intent(Side::Sell, "0.5", "10"), NOW);
        assert_eq!(sell, RiskDecision::Approved);
        let buy = gate.evaluate(&state, &intent(Side::Buy, "0.5", "10"), NOW);
        assert!(matches!(
            buy,
            RiskDecision::Rejected(RiskRejection::DailyLossExceeded { .. })
        ));
    }

    /// F4 no-latch invariant: unlike the Python soft-halt (which latches a
    /// one-way kill switch on a realized breach until an operator resets it),
    /// the Rust live gate re-evaluates the daily-loss cap on every event and
    /// holds no breach memory. So if realized loss recovers below the cap, a new
    /// risk-increasing BUY is allowed again on the very next evaluation. This is
    /// the documented, intended live behaviour ("keep de-risking, block new buys
    /// only while over the cap"); the hard process-level stops are the
    /// kill-switch file, --max-live-orders, and the toxicity breaker.
    #[test]
    fn daily_loss_gate_does_not_latch_and_reopens_when_loss_recovers() {
        let gate = RiskGate::new(limits());
        let mut state = fresh_state();

        // Over the cap (250) -> a risk-increasing BUY is rejected.
        state.daily_realized_loss = 300 * SCALE as i64;
        assert!(matches!(
            gate.evaluate(&state, &intent(Side::Buy, "0.5", "10"), NOW),
            RiskDecision::Rejected(RiskRejection::DailyLossExceeded { .. })
        ));

        // Same gate, loss now recovered below the cap -> the identical BUY is
        // approved. A latching gate would still reject here.
        state.daily_realized_loss = 10 * SCALE as i64;
        assert_eq!(
            gate.evaluate(&state, &intent(Side::Buy, "0.5", "10"), NOW),
            RiskDecision::Approved
        );
    }

    #[test]
    fn rejects_projected_position_notional_breach() {
        let mut lim = limits();
        lim.max_order_notional = "10000".into();
        let gate = RiskGate::new(lim);
        let mut state = fresh_state();
        state.positions.insert(
            outcome_position_key("kalshi:M-1", OutcomeSide::Yes),
            Position {
                quantity: 4000,
                avg_price_ticks: 5000,
            },
        );
        // 4000 + 1000 buys = 5000 contracts * 0.5 = 2500, exactly at limit (allowed).
        let approved = gate.evaluate(&state, &intent(Side::Buy, "0.5", "1000"), NOW);
        assert_eq!(approved, RiskDecision::Approved);
        // 4000 + 2000 buys = 6000 * 0.5 = 3000, exceeds 2500.
        let rejected = gate.evaluate(&state, &intent(Side::Buy, "0.5", "2000"), NOW);
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
        let mut state = fresh_state();
        state.positions.insert(
            outcome_position_key("kalshi:M-1", OutcomeSide::Yes),
            Position {
                quantity: 4000,
                avg_price_ticks: 5000,
            },
        );
        let dec = gate.evaluate(&state, &intent(Side::Sell, "0.5", "2000"), NOW);
        assert_eq!(dec, RiskDecision::Approved);
    }

    #[test]
    fn yes_and_no_positions_do_not_net_for_position_limit() {
        let mut lim = limits();
        lim.max_order_notional = "10000".into();
        lim.max_gross_exposure = "10000".into();
        let gate = RiskGate::new(lim);
        let mut state = fresh_state();
        state.positions.insert(
            outcome_position_key("kalshi:M-1", OutcomeSide::Yes),
            Position {
                quantity: 4000,
                avg_price_ticks: 5000,
            },
        );

        let mut no_intent = intent(Side::Buy, "0.5", "4000");
        no_intent.outcome_side = OutcomeSide::No;
        let dec = gate.evaluate(&state, &no_intent, NOW);
        assert_eq!(dec, RiskDecision::Approved);
    }

    #[test]
    fn rejects_gross_exposure_breach_across_instruments() {
        let mut lim = limits();
        lim.max_gross_exposure = "1000".into();
        lim.max_position_notional = "1000".into();
        lim.max_order_notional = "1000".into();
        let gate = RiskGate::new(lim);
        let mut state = fresh_state();
        state.positions.insert(
            outcome_position_key("kalshi:OTHER", OutcomeSide::Yes),
            Position {
                quantity: 1000,
                avg_price_ticks: 8000,
            },
        );
        let dec = gate.evaluate(&state, &intent(Side::Buy, "0.5", "1000"), NOW);
        assert!(matches!(
            dec,
            RiskDecision::Rejected(RiskRejection::GrossExposureExceeded { .. })
        ));
    }

    #[test]
    fn rejects_when_price_or_quantity_unparseable() {
        let gate = RiskGate::new(limits());
        let state = fresh_state();
        let dec = gate.evaluate(&state, &intent(Side::Buy, "abc", "1"), NOW);
        match dec {
            RiskDecision::Rejected(RiskRejection::InvalidNumeric { field }) => {
                assert_eq!(field, "price");
            }
            other => panic!("expected InvalidNumeric, got {other:?}"),
        }
    }

    #[test]
    fn fixed_decimal_rejects_unrepresentable_precision() {
        assert_eq!(parse_fixed("0.1234"), Ok(1234));
        assert!(parse_fixed("0.12345").is_err());
        assert!(parse_fixed("1e-2").is_err());
    }

    #[test]
    fn rejects_negative_edge_after_kalshi_taker_fee() {
        let gate = RiskGate::new(limits());
        let state = fresh_state();
        let mut intent = intent(Side::Buy, "0.50", "1");
        intent.fair_price = Some("0.51".into());
        intent.min_executable_edge_ticks = Some(0);

        let dec = gate.evaluate(&state, &intent, NOW);

        assert_eq!(
            dec,
            RiskDecision::Rejected(RiskRejection::NegativeEdgeAfterFees {
                expected_edge_ticks: -100,
                minimum_edge_ticks: 0,
                fee_ticks_per_contract: 200,
            })
        );
    }

    #[test]
    fn approves_positive_edge_after_kalshi_taker_fee() {
        let gate = RiskGate::new(limits());
        let state = fresh_state();
        let mut intent = intent(Side::Buy, "0.50", "1");
        intent.fair_price = Some("0.53".into());
        intent.min_executable_edge_ticks = Some(0);

        let dec = gate.evaluate(&state, &intent, NOW);

        assert_eq!(dec, RiskDecision::Approved);
    }

    #[test]
    fn rejects_buy_exceeding_available_cash_but_allows_sell() {
        // F6: BUY consumes cash and is gated; SELL returns cash and is never
        // blocked by the cash gate. $5 cash, BUY 100 @ 0.90 = $90 notional.
        let gate = RiskGate::new(limits());
        let mut state = fresh_state();
        state.available_cash_ticks = Some(5 * SCALE as i64);
        let buy = gate.evaluate(&state, &intent(Side::Buy, "0.90", "100"), NOW);
        assert!(matches!(
            buy,
            RiskDecision::Rejected(RiskRejection::InsufficientCash { .. })
        ));
        // A SELL of a held long is an exit; the cash gate must not block it.
        state.positions.insert(
            outcome_position_key("kalshi:M-1", OutcomeSide::Yes),
            Position {
                quantity: 100,
                avg_price_ticks: 5000,
            },
        );
        let sell = gate.evaluate(&state, &intent(Side::Sell, "0.90", "100"), NOW);
        assert_eq!(sell, RiskDecision::Approved);
    }

    #[test]
    fn cash_gate_skipped_when_balance_unknown() {
        // None == unknown: the gate is inert (paper / failed balance fetch).
        let gate = RiskGate::new(limits());
        let mut state = fresh_state();
        state.available_cash_ticks = None;
        let dec = gate.evaluate(&state, &intent(Side::Buy, "0.90", "100"), NOW);
        assert_eq!(dec, RiskDecision::Approved);
    }

    #[test]
    fn liquidation_drawdown_marks_long_to_bid() {
        // F3: a long 10 @ $0.50 with the bid at $0.49 is $0.10/contract * 10 =
        // $1.00 underwater → 1.00 * SCALE drawdown ticks.
        let mut state = fresh_state(); // records bid 4900 / ask 5100 for M-1
        state.positions.insert(
            outcome_position_key("kalshi:M-1", OutcomeSide::Yes),
            Position {
                quantity: 10,
                avg_price_ticks: 5000,
            },
        );
        assert_eq!(liquidation_unrealized_drawdown_ticks(&state), 1000);
        // A position in the green (entry below the bid) contributes no drawdown.
        state.positions.insert(
            outcome_position_key("kalshi:M-1", OutcomeSide::Yes),
            Position {
                quantity: 10,
                avg_price_ticks: 4800,
            },
        );
        assert_eq!(liquidation_unrealized_drawdown_ticks(&state), 0);
    }

    #[test]
    fn unrealized_drawdown_feeds_daily_loss_gate() {
        // The drawdown the helper computes, written into the sleeve state, trips
        // the daily-loss gate exactly like realized loss does.
        let gate = RiskGate::new(limits()); // max_daily_loss = 250
        let mut state = fresh_state();
        state.positions.insert(
            outcome_position_key("kalshi:OTHER", OutcomeSide::Yes),
            Position {
                quantity: 1000,
                avg_price_ticks: 5000,
            },
        );
        // Mark OTHER's bid far below entry: (200 - 5000) * 1000 = -$480 drawdown.
        state
            .best_bid_ticks
            .insert(outcome_position_key("kalshi:OTHER", OutcomeSide::Yes), 200);
        state.unrealized_drawdown_loss = liquidation_unrealized_drawdown_ticks(&state);
        assert!(state.unrealized_drawdown_loss >= 250 * SCALE as i64);
        let dec = gate.evaluate(&state, &intent(Side::Buy, "0.50", "1"), NOW);
        assert!(matches!(
            dec,
            RiskDecision::Rejected(RiskRejection::DailyLossExceeded { .. })
        ));
    }
}
