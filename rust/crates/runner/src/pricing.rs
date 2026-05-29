//! Deterministic price discretisation helpers (Rust parity for the Python
//! `eventcontracts.strategy.pricing` module).
//!
//! Prices are fixed-point ticks where `PRICE_ONE` (1_000_000) == $1.00. This is
//! the **runner's** 6-decimal `FixedPrice` scale (`lib.rs::PRICE_SCALE` is an
//! alias of `PRICE_ONE`), not the 4-decimal bus-boundary scale — keeping the two
//! in lockstep is what lets these helpers be dropped into the runner without a
//! scale mismatch silently clamping every price to ~$0.01. The venue tick
//! (Kalshi: 1 cent) is `CENT_TICK` (10_000) in these units. The edge-preserving
//! rule is the same as Python: a BUY limit floors to the tick grid (never pay
//! above fair) and a SELL limit ceils (never sell below fair), so the rounding
//! step itself cannot turn a positive model edge negative.

/// Fixed-point representation of $1.00 (6-decimal). Single source of truth for
/// the runner's `PRICE_SCALE`.
pub const PRICE_ONE: i64 = 1_000_000;
/// Default venue tick: 1 cent, in `PRICE_ONE` units.
pub const CENT_TICK: i64 = 10_000;

/// Largest tick-multiple `<= price_ticks`. `tick` must be positive.
pub fn floor_to_tick(price_ticks: i64, tick: i64) -> i64 {
    debug_assert!(tick > 0, "tick must be positive");
    price_ticks.div_euclid(tick).saturating_mul(tick)
}

/// Smallest tick-multiple `>= price_ticks`. `tick` must be positive.
pub fn ceil_to_tick(price_ticks: i64, tick: i64) -> i64 {
    debug_assert!(tick > 0, "tick must be positive");
    let rem = price_ticks.rem_euclid(tick);
    if rem == 0 {
        price_ticks
    } else {
        price_ticks.saturating_add(tick - rem)
    }
}

/// Clamp into the tradable band `[tick, PRICE_ONE - tick]` for a binary market.
pub fn clamp_price(price_ticks: i64, tick: i64) -> i64 {
    let low = tick;
    let high = PRICE_ONE - tick;
    price_ticks.clamp(low, high)
}

/// Edge-preserving BUY limit: floor the fair value, clamped to the band.
pub fn buy_limit_from_fair(fair_ticks: i64, tick: i64) -> i64 {
    clamp_price(floor_to_tick(fair_ticks, tick), tick)
}

/// Edge-preserving SELL limit: ceil the fair value, clamped to the band.
pub fn sell_limit_from_fair(fair_ticks: i64, tick: i64) -> i64 {
    clamp_price(ceil_to_tick(fair_ticks, tick), tick)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn floor_and_ceil_match_python_semantics() {
        // 0.556 -> floor 0.55, ceil 0.56 (ticks: 556_000 -> 550_000 / 560_000).
        assert_eq!(floor_to_tick(556_000, CENT_TICK), 550_000);
        assert_eq!(ceil_to_tick(556_000, CENT_TICK), 560_000);
        // Exact tick is unchanged either way.
        assert_eq!(floor_to_tick(550_000, CENT_TICK), 550_000);
        assert_eq!(ceil_to_tick(550_000, CENT_TICK), 550_000);
        // Half-cent mid (the odd-tick-sum case the parity fixtures must cover):
        // 0.595 -> buy floors to 0.59, the No complement 0.405 -> 0.40.
        assert_eq!(floor_to_tick(595_000, CENT_TICK), 590_000);
        assert_eq!(floor_to_tick(405_000, CENT_TICK), 400_000);
    }

    #[test]
    fn buy_floors_and_sell_ceils_to_preserve_edge() {
        assert_eq!(buy_limit_from_fair(556_000, CENT_TICK), 550_000);
        assert_eq!(sell_limit_from_fair(556_000, CENT_TICK), 560_000);
        assert!(buy_limit_from_fair(556_000, CENT_TICK) <= 556_000);
        assert!(sell_limit_from_fair(556_000, CENT_TICK) >= 556_000);
    }

    #[test]
    fn clamp_keeps_prices_in_band() {
        assert_eq!(clamp_price(4_000, CENT_TICK), CENT_TICK); // 0.004 -> 0.01
        assert_eq!(clamp_price(999_000, CENT_TICK), PRICE_ONE - CENT_TICK); // 0.999 -> 0.99
        assert_eq!(buy_limit_from_fair(3_000, CENT_TICK), CENT_TICK); // 0.003 floors to 0, clamps to 0.01
        assert_eq!(
            sell_limit_from_fair(999_000, CENT_TICK),
            PRICE_ONE - CENT_TICK
        );
    }
}
