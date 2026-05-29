/// Kalshi taker fee in dollars * 10_000 ticks.
///
/// Kalshi's published fill fee is:
///
/// `ceil(100 * rate * p * (1 - p) * contracts)` cents
///
/// with price `p` in dollars. `price_ticks` uses the repo standard
/// 1e-4-dollar scale, `qty_contracts` is whole contracts, and `rate_bps`
/// defaults to 700 for the 7% public curve.
pub fn kalshi_taker_fee_ticks(price_ticks: i64, qty_contracts: i64, rate_bps: u32) -> i64 {
    if qty_contracts <= 0 || price_ticks <= 0 || price_ticks >= 10_000 || rate_bps == 0 {
        return 0;
    }
    let p = i128::from(price_ticks);
    let one_minus_p = i128::from(10_000_i64.saturating_sub(price_ticks));
    let qty = i128::from(qty_contracts);
    let numerator = i128::from(rate_bps)
        .saturating_mul(p)
        .saturating_mul(one_minus_p)
        .saturating_mul(qty);
    let cents = div_ceil_i128(numerator, 10_000_000_000_i128);
    i64::try_from(cents.saturating_mul(100)).unwrap_or(i64::MAX)
}

fn div_ceil_i128(numerator: i128, denominator: i128) -> i128 {
    if numerator <= 0 {
        return 0;
    }
    numerator
        .saturating_add(denominator.saturating_sub(1))
        .saturating_div(denominator)
}

#[cfg(test)]
mod tests {
    use super::kalshi_taker_fee_ticks;

    #[test]
    fn kalshi_fee_matches_python_at_known_points() {
        assert_eq!(kalshi_taker_fee_ticks(5_000, 1, 700), 200);
        assert_eq!(kalshi_taker_fee_ticks(1_000, 1, 700), 100);
        assert_eq!(kalshi_taker_fee_ticks(9_000, 1, 700), 100);
        assert_eq!(kalshi_taker_fee_ticks(5_000, 10, 700), 1_800);
    }

    #[test]
    fn kalshi_fee_is_zero_for_maker_equivalent_inputs() {
        assert_eq!(kalshi_taker_fee_ticks(5_000, 0, 700), 0);
        assert_eq!(kalshi_taker_fee_ticks(0, 10, 700), 0);
        assert_eq!(kalshi_taker_fee_ticks(10_000, 10, 700), 0);
        assert_eq!(kalshi_taker_fee_ticks(5_000, 10, 0), 0);
    }
}
