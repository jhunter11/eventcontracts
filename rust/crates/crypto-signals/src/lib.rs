//! Signal aggregation for the crypto ensemble.
//!
//! Mirrors `python/src/eventcontracts/crypto/signals.py`. Each
//! signal source is a pure function from a small state dataclass to
//! a vector of [`Signal`] values; [`combine_signals`] aggregates per
//! instrument into one [`EnsembleVerdict`].

use std::collections::BTreeMap;

use chrono::{DateTime, Utc};
use eventcontracts_crypto_domain::{
    EnsembleVerdict, InstrumentId, OutcomeSide, Signal, Venue,
};
use eventcontracts_crypto_pricing::{
    bracket_parity_deviation, bs_above_probability, bs_above_probability_unclipped,
    monotone_violations, realized_volatility,
};
use rust_decimal::Decimal;

// ----------------------------- Confidence defaults -----------------------------

/// Per-source default confidence values, matching the Python ranking.
pub fn default_confidence(source: &str) -> Decimal {
    match source {
        "parity" => Decimal::new(95, 2),
        "vol_surface" => Decimal::new(70, 2),
        "bracket_vol" => Decimal::new(65, 2),
        "terminal" => Decimal::new(80, 2),
        "regime" => Decimal::new(55, 2),
        "skew" => Decimal::new(85, 2),
        _ => Decimal::new(50, 2),
    }
}

// ----------------------------- Parity -----------------------------

#[derive(Debug, Clone)]
pub struct ParityState {
    pub bracket_market_ids: Vec<String>,
    pub mid_by_market: BTreeMap<String, Decimal>,
    pub spread_bps_by_market: BTreeMap<String, Decimal>,
}

impl ParityState {
    pub fn new(bracket_market_ids: Vec<String>) -> Self {
        Self {
            bracket_market_ids,
            mid_by_market: BTreeMap::new(),
            spread_bps_by_market: BTreeMap::new(),
        }
    }

    pub fn has_all_mids(&self) -> bool {
        self.bracket_market_ids
            .iter()
            .all(|id| self.mid_by_market.contains_key(id))
    }
}

pub fn parity_signals(
    state: &ParityState,
    venue: Venue,
    min_parity_edge: Decimal,
    max_spread_bps: Decimal,
    confidence: Option<Decimal>,
) -> Vec<Signal> {
    if !state.has_all_mids() {
        return Vec::new();
    }
    let widest = state
        .spread_bps_by_market
        .values()
        .copied()
        .fold(Decimal::ZERO, Decimal::max);
    if widest > max_spread_bps {
        return Vec::new();
    }
    let probs = state
        .bracket_market_ids
        .iter()
        .filter_map(|id| state.mid_by_market.get(id).copied());
    let deviation = bracket_parity_deviation(probs);
    if deviation.abs() < min_parity_edge {
        return Vec::new();
    }
    let side = if deviation > Decimal::ZERO {
        OutcomeSide::No
    } else {
        OutcomeSide::Yes
    };
    let edge_bps = deviation.abs() * Decimal::from(10_000);
    let conf = confidence.unwrap_or_else(|| default_confidence("parity"));
    state
        .bracket_market_ids
        .iter()
        .map(|market_id| Signal {
            instrument_id: InstrumentId::new(venue, market_id.clone()),
            side,
            edge_bps,
            confidence: conf,
            horizon_seconds: 0,
            source: "parity".into(),
            reason: format!("parity_dev={deviation:+.4}"),
        })
        .collect()
}

// ----------------------------- Vol surface (above-K markets) -----------------------------

#[derive(Debug, Clone)]
pub struct VolSurfaceState {
    pub strike_by_market: BTreeMap<String, Decimal>,
    pub spot: Option<Decimal>,
    pub sigma_annual: Option<Decimal>,
    pub expiry_at: Option<DateTime<Utc>>,
    pub mid_by_market: BTreeMap<String, Decimal>,
}

impl VolSurfaceState {
    pub fn new(strike_by_market: BTreeMap<String, Decimal>) -> Self {
        Self {
            strike_by_market,
            spot: None,
            sigma_annual: None,
            expiry_at: None,
            mid_by_market: BTreeMap::new(),
        }
    }
}

pub fn vol_surface_signals(
    state: &VolSurfaceState,
    venue: Venue,
    now: DateTime<Utc>,
    twap_window_seconds: Decimal,
    min_edge_bps: Decimal,
    confidence: Option<Decimal>,
) -> Vec<Signal> {
    let (Some(spot), Some(sigma), Some(expiry)) =
        (state.spot, state.sigma_annual, state.expiry_at)
    else {
        return Vec::new();
    };
    let tau_seconds = (expiry - now).num_seconds().max(0);
    if tau_seconds == 0 {
        return Vec::new();
    }
    let tau_dec = Decimal::from(tau_seconds);
    let conf = confidence.unwrap_or_else(|| default_confidence("vol_surface"));
    let mut out = Vec::new();
    for (market_id, strike) in &state.strike_by_market {
        let Some(&mid) = state.mid_by_market.get(market_id) else {
            continue;
        };
        let bs_prob = bs_above_probability(spot, *strike, sigma, tau_dec, twap_window_seconds);
        let edge_bps = (bs_prob - mid) * Decimal::from(10_000);
        if edge_bps.abs() < min_edge_bps {
            continue;
        }
        let side = if edge_bps > Decimal::ZERO {
            OutcomeSide::Yes
        } else {
            OutcomeSide::No
        };
        out.push(Signal {
            instrument_id: InstrumentId::new(venue, market_id.clone()),
            side,
            edge_bps: edge_bps.abs(),
            confidence: conf,
            horizon_seconds: tau_seconds,
            source: "vol_surface".into(),
            reason: format!("bs={bs_prob:.4}_mid={mid:.4}_sigma={sigma:.3}"),
        });
    }
    out
}

// ----------------------------- Bracket vol (between markets) -----------------------------

#[derive(Debug, Clone)]
pub struct BracketVolState {
    /// `(market_id, (lower, Some(upper) or None for unbounded))`.
    pub intervals_by_market: BTreeMap<String, (Decimal, Option<Decimal>)>,
    pub spot: Option<Decimal>,
    pub sigma_annual: Option<Decimal>,
    pub expiry_at: Option<DateTime<Utc>>,
    pub mid_by_market: BTreeMap<String, Decimal>,
}

impl BracketVolState {
    pub fn new(intervals_by_market: BTreeMap<String, (Decimal, Option<Decimal>)>) -> Self {
        Self {
            intervals_by_market,
            spot: None,
            sigma_annual: None,
            expiry_at: None,
            mid_by_market: BTreeMap::new(),
        }
    }
}

pub fn bracket_vol_signals(
    state: &BracketVolState,
    venue: Venue,
    now: DateTime<Utc>,
    twap_window_seconds: Decimal,
    min_edge_bps: Decimal,
    confidence: Option<Decimal>,
) -> Vec<Signal> {
    let (Some(spot), Some(sigma), Some(expiry)) =
        (state.spot, state.sigma_annual, state.expiry_at)
    else {
        return Vec::new();
    };
    let tau_seconds = (expiry - now).num_seconds().max(0);
    if tau_seconds == 0 {
        return Vec::new();
    }
    let tau_dec = Decimal::from(tau_seconds);
    let conf = confidence.unwrap_or_else(|| default_confidence("bracket_vol"));
    let mut out = Vec::new();
    let tiny = Decimal::new(1, 4);
    for (market_id, (lower, upper)) in &state.intervals_by_market {
        let Some(&mid) = state.mid_by_market.get(market_id) else {
            continue;
        };
        let lower_clamped = if *lower > Decimal::ZERO { *lower } else { tiny };
        let p_above_lower = bs_above_probability_unclipped(
            spot,
            lower_clamped,
            sigma,
            tau_dec,
            twap_window_seconds,
        );
        let p_above_upper = match upper {
            None => Decimal::ZERO,
            Some(u) if *u <= Decimal::ZERO => Decimal::ZERO,
            Some(u) => {
                bs_above_probability_unclipped(spot, *u, sigma, tau_dec, twap_window_seconds)
            }
        };
        let interval = (p_above_lower - p_above_upper)
            .max(Decimal::ZERO)
            .min(Decimal::ONE);
        let edge_bps = (interval - mid) * Decimal::from(10_000);
        if edge_bps.abs() < min_edge_bps {
            continue;
        }
        let side = if edge_bps > Decimal::ZERO {
            OutcomeSide::Yes
        } else {
            OutcomeSide::No
        };
        out.push(Signal {
            instrument_id: InstrumentId::new(venue, market_id.clone()),
            side,
            edge_bps: edge_bps.abs(),
            confidence: conf,
            horizon_seconds: tau_seconds,
            source: "bracket_vol".into(),
            reason: format!("bs_interval={interval:.4}_mid={mid:.4}_sigma={sigma:.3}"),
        });
    }
    out
}

// ----------------------------- Terminal -----------------------------

#[derive(Debug, Clone)]
pub struct TerminalState {
    pub strike_by_market: BTreeMap<String, Decimal>,
    pub spot_history: Vec<Decimal>,
    pub spot_capacity: usize,
    pub expiry_at: Option<DateTime<Utc>>,
    pub mid_by_market: BTreeMap<String, Decimal>,
}

impl TerminalState {
    pub fn new(strike_by_market: BTreeMap<String, Decimal>) -> Self {
        Self {
            strike_by_market,
            spot_history: Vec::new(),
            spot_capacity: 600,
            expiry_at: None,
            mid_by_market: BTreeMap::new(),
        }
    }

    pub fn push_spot(&mut self, price: Decimal) {
        self.spot_history.push(price);
        if self.spot_history.len() > self.spot_capacity {
            self.spot_history.remove(0);
        }
    }
}

pub fn terminal_signals(
    state: &TerminalState,
    venue: Venue,
    now: DateTime<Utc>,
    terminal_window_seconds: i64,
    min_terminal_edge: Decimal,
    min_realized_samples: usize,
    confidence: Option<Decimal>,
) -> Vec<Signal> {
    let Some(expiry) = state.expiry_at else {
        return Vec::new();
    };
    let tau = (expiry - now).num_seconds();
    if tau > terminal_window_seconds || tau <= 0 {
        return Vec::new();
    }
    if state.spot_history.len() < min_realized_samples {
        return Vec::new();
    }
    let sigma = realized_volatility(&state.spot_history);
    if sigma <= Decimal::ZERO {
        return Vec::new();
    }
    let spot = *state.spot_history.last().expect("checked non-empty");
    let tau_dec = Decimal::from(tau);
    let conf = confidence.unwrap_or_else(|| default_confidence("terminal"));
    let mut out = Vec::new();
    for (market_id, strike) in &state.strike_by_market {
        let Some(&mid) = state.mid_by_market.get(market_id) else {
            continue;
        };
        let bs_prob = bs_above_probability(spot, *strike, sigma, tau_dec, Decimal::ZERO);
        let edge = bs_prob - mid;
        if edge.abs() < min_terminal_edge {
            continue;
        }
        let side = if edge > Decimal::ZERO {
            OutcomeSide::Yes
        } else {
            OutcomeSide::No
        };
        out.push(Signal {
            instrument_id: InstrumentId::new(venue, market_id.clone()),
            side,
            edge_bps: edge.abs() * Decimal::from(10_000),
            confidence: conf,
            horizon_seconds: tau,
            source: "terminal".into(),
            reason: format!("tau={tau}s_bs={bs_prob:.4}_mid={mid:.4}"),
        });
    }
    out
}

// ----------------------------- Skew -----------------------------

#[derive(Debug, Clone)]
pub struct SkewState {
    pub strikes: Vec<(String, Decimal)>,
    pub mid_by_market: BTreeMap<String, Decimal>,
    pub spread_bps_by_market: BTreeMap<String, Decimal>,
}

impl SkewState {
    pub fn new(mut strikes: Vec<(String, Decimal)>) -> Self {
        strikes.sort_by(|a, b| a.1.cmp(&b.1));
        Self {
            strikes,
            mid_by_market: BTreeMap::new(),
            spread_bps_by_market: BTreeMap::new(),
        }
    }
}

pub fn skew_signals(
    state: &SkewState,
    venue: Venue,
    min_skew_edge: Decimal,
    max_spread_bps: Decimal,
    confidence: Option<Decimal>,
) -> Vec<Signal> {
    if state
        .strikes
        .iter()
        .any(|(mid, _)| !state.mid_by_market.contains_key(mid))
    {
        return Vec::new();
    }
    let by_strike: Vec<(Decimal, Decimal)> = state
        .strikes
        .iter()
        .map(|(market_id, strike)| (*strike, state.mid_by_market[market_id]))
        .collect();
    let violations = monotone_violations(&by_strike);
    if violations.is_empty() {
        return Vec::new();
    }
    let strike_to_market: BTreeMap<Decimal, &str> = state
        .strikes
        .iter()
        .map(|(m, s)| (*s, m.as_str()))
        .collect();
    let conf = confidence.unwrap_or_else(|| default_confidence("skew"));
    let mut out = Vec::new();
    for (strike_low, _p_low, strike_high, p_high) in violations {
        let low_market = strike_to_market[&strike_low].to_string();
        let high_market = strike_to_market[&strike_high].to_string();
        let edge = p_high - state.mid_by_market[&low_market];
        if edge < min_skew_edge {
            continue;
        }
        let (Some(low_spread), Some(high_spread)) = (
            state.spread_bps_by_market.get(&low_market),
            state.spread_bps_by_market.get(&high_market),
        ) else {
            continue;
        };
        if *low_spread > max_spread_bps || *high_spread > max_spread_bps {
            continue;
        }
        let edge_bps = edge * Decimal::from(10_000);
        let reason = format!(
            "butterfly_low={strike_low}_high={strike_high}"
        );
        out.push(Signal {
            instrument_id: InstrumentId::new(venue, low_market.clone()),
            side: OutcomeSide::Yes,
            edge_bps,
            confidence: conf,
            horizon_seconds: 0,
            source: "skew".into(),
            reason: reason.clone(),
        });
        out.push(Signal {
            instrument_id: InstrumentId::new(venue, high_market),
            side: OutcomeSide::No,
            edge_bps,
            confidence: conf,
            horizon_seconds: 0,
            source: "skew".into(),
            reason,
        });
    }
    out
}

// ----------------------------- Combiner -----------------------------

/// Aggregate signals into per-instrument verdicts.
///
/// Mirrors `combine_signals` in Python:
/// `net_edge = Σ weight[source] * edge_bps * confidence * sign(side)`
/// where `sign(YES) = +1`, `sign(NO) = -1`. The verdict carries the
/// dominant side when `|net| > min_combined_edge_bps`, otherwise HOLD.
pub fn combine_signals(
    signals: &[Signal],
    weights: &BTreeMap<String, Decimal>,
    min_combined_edge_bps: Decimal,
    min_confluence: usize,
) -> Vec<EnsembleVerdict> {
    // Group by instrument.
    let mut grouped: BTreeMap<String, Vec<&Signal>> = BTreeMap::new();
    for signal in signals {
        // Key by market id only — instrument equality includes venue
        // already and verdicts are returned with the original instrument.
        grouped
            .entry(signal.instrument_id.market_id.clone())
            .or_default()
            .push(signal);
    }
    let mut verdicts = Vec::new();
    for (_market_id, group) in grouped {
        let mut sources_seen: std::collections::BTreeSet<&str> =
            std::collections::BTreeSet::new();
        for s in &group {
            sources_seen.insert(s.source.as_str());
        }
        if sources_seen.len() < min_confluence {
            continue;
        }
        let mut per_source: BTreeMap<String, Decimal> = BTreeMap::new();
        for s in &group {
            let w = weights.get(&s.source).copied().unwrap_or(Decimal::ONE);
            let mut signed = s.edge_bps * s.confidence * w;
            if matches!(s.side, OutcomeSide::No) {
                signed = -signed;
            }
            *per_source.entry(s.source.clone()).or_insert(Decimal::ZERO) += signed;
        }
        let net: Decimal = per_source.values().copied().sum();
        let side = if net.abs() < min_combined_edge_bps {
            None
        } else if net > Decimal::ZERO {
            Some(OutcomeSide::Yes)
        } else {
            Some(OutcomeSide::No)
        };
        let representative = group[0].instrument_id.clone();
        verdicts.push(EnsembleVerdict {
            instrument_id: representative,
            side,
            net_edge_bps: net,
            contributing_sources: sources_seen.iter().map(|s| (*s).to_string()).collect(),
            per_source_edge_bps: per_source.into_iter().collect(),
        });
    }
    verdicts
}

// ----------------------------- Tests -----------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use std::str::FromStr;

    fn d(s: &str) -> Decimal {
        Decimal::from_str(s).unwrap()
    }

    fn instr(market_id: &str) -> InstrumentId {
        InstrumentId::new(Venue::Kalshi, market_id)
    }

    #[test]
    fn combine_agreeing_yes_emits_buy_yes() {
        let signals = vec![
            Signal {
                instrument_id: instr("M-1"),
                side: OutcomeSide::Yes,
                edge_bps: d("80"),
                confidence: d("0.7"),
                horizon_seconds: 900,
                source: "vol_surface".into(),
                reason: String::new(),
            },
            Signal {
                instrument_id: instr("M-1"),
                side: OutcomeSide::Yes,
                edge_bps: d("60"),
                confidence: d("0.9"),
                horizon_seconds: 0,
                source: "parity".into(),
                reason: String::new(),
            },
        ];
        let v = combine_signals(&signals, &BTreeMap::new(), d("20"), 2);
        assert_eq!(v.len(), 1);
        assert_eq!(v[0].side, Some(OutcomeSide::Yes));
        assert!(v[0].net_edge_bps > Decimal::ZERO);
    }

    #[test]
    fn combine_below_confluence_drops_instrument() {
        let signals = vec![Signal {
            instrument_id: instr("M-1"),
            side: OutcomeSide::Yes,
            edge_bps: d("200"),
            confidence: d("0.9"),
            horizon_seconds: 0,
            source: "vol_surface".into(),
            reason: String::new(),
        }];
        let v = combine_signals(&signals, &BTreeMap::new(), d("20"), 2);
        assert!(v.is_empty());
    }

    #[test]
    fn combine_equal_and_opposite_holds() {
        let signals = vec![
            Signal {
                instrument_id: instr("M-1"),
                side: OutcomeSide::Yes,
                edge_bps: d("80"),
                confidence: d("0.9"),
                horizon_seconds: 0,
                source: "parity".into(),
                reason: String::new(),
            },
            Signal {
                instrument_id: instr("M-1"),
                side: OutcomeSide::No,
                edge_bps: d("80"),
                confidence: d("0.9"),
                horizon_seconds: 0,
                source: "vol_surface".into(),
                reason: String::new(),
            },
        ];
        let v = combine_signals(&signals, &BTreeMap::new(), d("20"), 2);
        assert_eq!(v.len(), 1);
        assert_eq!(v[0].side, None);
        assert_eq!(v[0].net_edge_bps, Decimal::ZERO);
    }

    #[test]
    fn weights_can_flip_verdict() {
        let signals = vec![
            Signal {
                instrument_id: instr("M-1"),
                side: OutcomeSide::Yes,
                edge_bps: d("80"),
                confidence: d("0.9"),
                horizon_seconds: 0,
                source: "parity".into(),
                reason: String::new(),
            },
            Signal {
                instrument_id: instr("M-1"),
                side: OutcomeSide::No,
                edge_bps: d("80"),
                confidence: d("0.9"),
                horizon_seconds: 0,
                source: "vol_surface".into(),
                reason: String::new(),
            },
        ];
        let mut w = BTreeMap::new();
        w.insert("parity".into(), d("1.0"));
        w.insert("vol_surface".into(), d("0.1"));
        let v = combine_signals(&signals, &w, d("20"), 2);
        assert_eq!(v[0].side, Some(OutcomeSide::Yes));
    }

    #[test]
    fn parity_signals_fire_on_overpriced_partition() {
        let mut state = ParityState::new(vec!["A".into(), "B".into(), "C".into()]);
        state.mid_by_market.insert("A".into(), d("0.36"));
        state.mid_by_market.insert("B".into(), d("0.36"));
        state.mid_by_market.insert("C".into(), d("0.36"));
        state.spread_bps_by_market.insert("A".into(), d("100"));
        state.spread_bps_by_market.insert("B".into(), d("100"));
        state.spread_bps_by_market.insert("C".into(), d("100"));
        let sigs = parity_signals(&state, Venue::Kalshi, d("0.02"), d("500"), None);
        assert_eq!(sigs.len(), 3);
        for s in &sigs {
            assert_eq!(s.side, OutcomeSide::No);
        }
    }
}
