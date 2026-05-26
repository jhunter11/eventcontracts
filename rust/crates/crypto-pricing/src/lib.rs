//! Pricing math shared by the crypto signal sources.
//!
//! Mirrors `python/src/eventcontracts/crypto/pricing.py` line-for-line:
//! every function takes the same arguments and returns the same shape so
//! Python and Rust runs of the ensemble produce byte-identical decisions
//! when fed identical inputs (the cross-language parity contract).
//!
//! All inputs are [`Decimal`] so the public boundary stays exact;
//! transcendental work happens in `f64` internally and the result
//! returns to `Decimal`.

use rust_decimal::prelude::ToPrimitive;
use rust_decimal::Decimal;

/// Seconds in a 365.25-day year. Matches the Python constant.
pub const SECONDS_PER_YEAR: f64 = 365.25 * 24.0 * 60.0 * 60.0;

// ----------------------------- Normal CDF -----------------------------

/// Standard normal CDF Φ(x) via an Abramowitz & Stegun-style `erf`
/// approximation. Maximum absolute error ≈ 1.5e-7 across `R`.
pub fn norm_cdf(x: f64) -> f64 {
    0.5 * (1.0 + erf(x / std::f64::consts::SQRT_2))
}

fn erf(x: f64) -> f64 {
    // Abramowitz & Stegun 7.1.26.
    let sign = x.signum();
    let x = x.abs();
    let t = 1.0 / (1.0 + 0.3275911 * x);
    let y = 1.0
        - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t
            + 0.254829592)
            * t
            * (-x * x).exp();
    sign * y
}

// ----------------------------- Black-Scholes -----------------------------

/// Unclipped lognormal probability that `S_T >= strike` at expiry.
///
/// Use this when computing bracket interval probabilities as the
/// difference of two CDF values — clipping inputs breaks parity.
/// Strategies emitting decisions should call [`bs_above_probability`]
/// which floors and caps the result.
pub fn bs_above_probability_unclipped(
    spot: Decimal,
    strike: Decimal,
    sigma_annual: Decimal,
    tau_seconds: Decimal,
    twap_window_seconds: Decimal,
) -> Decimal {
    if spot <= Decimal::ZERO || strike <= Decimal::ZERO || sigma_annual <= Decimal::ZERO {
        return half();
    }
    let spot_f = spot.to_f64().unwrap_or(0.0);
    let strike_f = strike.to_f64().unwrap_or(0.0);
    let sigma_f = sigma_annual.to_f64().unwrap_or(0.0);
    let tau_raw = tau_seconds.to_f64().unwrap_or(0.0).max(1.0);
    let twap_raw = twap_window_seconds.to_f64().unwrap_or(0.0).max(0.0);

    let tau = tau_raw / SECONDS_PER_YEAR;
    let twap_correction = twap_raw / 3.0 / SECONDS_PER_YEAR;
    let effective_tau = (tau - twap_correction).max(1.0 / SECONDS_PER_YEAR);

    let vol_root = sigma_f * effective_tau.sqrt();
    if vol_root == 0.0 {
        return half();
    }
    let d2 = ((spot_f / strike_f).ln() - 0.5 * sigma_f * sigma_f * effective_tau) / vol_root;
    let prob = norm_cdf(d2);
    Decimal::from_f64_retain(prob).unwrap_or_else(half)
}

/// Probability that `S_T >= strike` at expiry, clipped to `[0.0001, 0.9999]`.
pub fn bs_above_probability(
    spot: Decimal,
    strike: Decimal,
    sigma_annual: Decimal,
    tau_seconds: Decimal,
    twap_window_seconds: Decimal,
) -> Decimal {
    clip_probability(bs_above_probability_unclipped(
        spot,
        strike,
        sigma_annual,
        tau_seconds,
        twap_window_seconds,
    ))
}

fn clip_probability(p: Decimal) -> Decimal {
    let lo = Decimal::new(1, 4); // 0.0001
    let hi = Decimal::new(9999, 4); // 0.9999
    p.max(lo).min(hi)
}

fn half() -> Decimal {
    Decimal::new(5, 1)
}

// ----------------------------- Bracket parity -----------------------------

/// ``sum(probs) - 1`` for a disjoint, exhaustive bracket partition.
///
/// Positive deviation means brackets are collectively overpriced (sell
/// all); negative means underpriced (buy all).
pub fn bracket_parity_deviation<I>(probs: I) -> Decimal
where
    I: IntoIterator<Item = Decimal>,
{
    let total: Decimal = probs.into_iter().sum();
    total - Decimal::ONE
}

// ----------------------------- Skew -----------------------------

/// Detect non-monotonicity in ``P(S_T >= K)`` across ascending ``K``.
///
/// Returns tuples `(strike_low, p_low, strike_high, p_high)` where
/// `p_high > p_low` despite `strike_high > strike_low` — a butterfly
/// arbitrage signal.
pub fn monotone_violations(
    quotes_by_strike: &[(Decimal, Decimal)],
) -> Vec<(Decimal, Decimal, Decimal, Decimal)> {
    let mut out = Vec::new();
    for window in quotes_by_strike.windows(2) {
        let (strike_low, p_low) = (window[0].0, window[0].1);
        let (strike_high, p_high) = (window[1].0, window[1].1);
        if p_high > p_low {
            out.push((strike_low, p_low, strike_high, p_high));
        }
    }
    out
}

// ----------------------------- Realized volatility -----------------------------

/// Annualized realized volatility from an evenly-spaced price series.
///
/// Computes log returns and scales by `sqrt(samples_per_year)` assuming
/// **one sample per second**. Callers with a different cadence should
/// rescale by `sqrt(cadence_seconds)` outside this function.
pub fn realized_volatility(prices: &[Decimal]) -> Decimal {
    if prices.len() < 2 {
        return Decimal::ZERO;
    }
    let mut returns: Vec<f64> = Vec::with_capacity(prices.len() - 1);
    let mut prev = prices[0].to_f64().unwrap_or(0.0);
    for p in &prices[1..] {
        let cur = p.to_f64().unwrap_or(0.0);
        if prev <= 0.0 || cur <= 0.0 {
            return Decimal::ZERO;
        }
        returns.push((cur / prev).ln());
        prev = cur;
    }
    if returns.len() < 2 {
        return Decimal::ZERO;
    }
    let mean: f64 = returns.iter().sum::<f64>() / returns.len() as f64;
    let var: f64 = returns.iter().map(|r| (r - mean) * (r - mean)).sum::<f64>()
        / (returns.len() - 1) as f64;
    let stddev = var.sqrt();
    let annual = stddev * SECONDS_PER_YEAR.sqrt();
    Decimal::from_f64_retain(annual).unwrap_or(Decimal::ZERO)
}

// ----------------------------- Tests -----------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use std::str::FromStr;

    fn d(s: &str) -> Decimal {
        Decimal::from_str(s).unwrap()
    }

    #[test]
    fn norm_cdf_matches_known_values() {
        assert!((norm_cdf(0.0) - 0.5).abs() < 1e-6);
        assert!((norm_cdf(1.0) - 0.8413447).abs() < 1e-4);
        assert!((norm_cdf(-1.0) - 0.1586553).abs() < 1e-4);
        assert!((norm_cdf(2.0) - 0.9772499).abs() < 1e-4);
    }

    #[test]
    fn bs_atm_15min_is_near_half() {
        let p = bs_above_probability(d("100000"), d("100000"), d("0.60"), d("900"), Decimal::ZERO);
        assert!(p > d("0.45") && p < d("0.55"), "p={p}");
    }

    #[test]
    fn bs_far_otm_clips_at_floor() {
        let p = bs_above_probability(d("100000"), d("200000"), d("0.60"), d("900"), Decimal::ZERO);
        assert_eq!(p, d("0.0001"));
    }

    #[test]
    fn bs_far_itm_clips_at_ceiling() {
        let p = bs_above_probability(d("200000"), d("100000"), d("0.60"), d("900"), Decimal::ZERO);
        assert_eq!(p, d("0.9999"));
    }

    #[test]
    fn higher_strike_has_lower_probability() {
        let p_low = bs_above_probability(d("100000"), d("99000"), d("0.60"), d("900"), Decimal::ZERO);
        let p_high = bs_above_probability(d("100000"), d("101000"), d("0.60"), d("900"), Decimal::ZERO);
        assert!(p_low > p_high);
    }

    #[test]
    fn parity_deviation_positive_when_sum_exceeds_one() {
        let dev = bracket_parity_deviation([d("0.35"), d("0.35"), d("0.35")]);
        assert_eq!(dev, d("0.05"));
    }

    #[test]
    fn monotone_violations_detects_inversion() {
        let v = monotone_violations(&[
            (d("100000"), d("0.60")),
            (d("101000"), d("0.65")),
            (d("102000"), d("0.40")),
        ]);
        assert_eq!(v.len(), 1);
        assert_eq!(v[0], (d("100000"), d("0.60"), d("101000"), d("0.65")));
    }

    #[test]
    fn realized_vol_grows_with_noise() {
        let quiet: Vec<Decimal> = (0..60).map(|i| Decimal::from(100000 + i)).collect();
        let noisy: Vec<Decimal> = (0..60).map(|i| Decimal::from(100000 + i * 100)).collect();
        assert!(realized_volatility(&noisy) > realized_volatility(&quiet));
    }

    #[test]
    fn realized_vol_zero_for_constant_series() {
        let series: Vec<Decimal> = vec![d("100000"); 60];
        assert_eq!(realized_volatility(&series), Decimal::ZERO);
    }
}
