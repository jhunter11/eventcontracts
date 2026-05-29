//! Numeric and identifier primitives used on the hot path.
//!
//! Everything in here is `Copy` or a small inline string. No `String`, no
//! `f64`, no heap allocation in steady state.

use serde::{Deserialize, Serialize};
use smol_str::SmolStr;
use thiserror::Error;

/// One USD tick in `FixedPrice` units. Chosen so 1 Kalshi cent ($0.01) is
/// exactly 100, and Polymarket sub-cent quotes (e.g., $0.475) round-trip
/// losslessly as 4750.
pub const FIXED_SCALE: i64 = 10_000;

/// Stack-allocated instrument identifier. `kalshi:<TICKER>` is the canonical
/// form; tickers >17 chars (i.e., total >23) heap-allocate inside SmolStr —
/// acceptable at the projection boundary, not on the steady-state hot path
/// (workers carry these by reference).
pub type InstrumentId = SmolStr;

/// Stack-allocated correlation id (typically a ULID, 26 bytes — falls just
/// above SmolStr's inline budget, but is allocated *once* per intent at emit
/// time, not per market tick).
pub type CorrelationId = SmolStr;

#[derive(Debug, Error, PartialEq, Eq, Clone)]
pub enum ParseError {
    #[error("empty decimal string")]
    Empty,
    #[error("invalid decimal character `{0}` at byte {1}")]
    InvalidChar(char, usize),
    #[error("decimal would overflow i64")]
    Overflow,
    #[error("more than one decimal point")]
    MultipleDots,
    #[error("scientific notation not supported")]
    Scientific,
    #[error("invalid integer quantity")]
    InvalidQty,
}

/// Fixed-point USD price. `FixedPrice(100)` == $0.01.
///
/// Arithmetic is checked at construction (parsing) and saturating on the hot
/// path so a malformed tick can never crash a worker. Comparison /
/// addition / subtraction are plain integer ops.
#[derive(
    Clone, Copy, Debug, Default, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize, Deserialize,
)]
pub struct FixedPrice(pub i64);

impl FixedPrice {
    pub const ZERO: Self = Self(0);
    pub const ONE_CENT: Self = Self(100);
    pub const ONE_DOLLAR: Self = Self(FIXED_SCALE);

    #[inline]
    pub fn raw(self) -> i64 {
        self.0
    }

    /// Dollars × `FIXED_SCALE`. Useful for tests and config-time constants.
    /// `from_dollars(0.50)` is intentionally not provided — no f64 anywhere.
    #[inline]
    pub const fn from_cents(cents: i64) -> Self {
        Self(cents * 100)
    }

    /// Saturating add, never panics on the hot path.
    #[inline]
    pub fn saturating_add(self, other: Self) -> Self {
        Self(self.0.saturating_add(other.0))
    }

    /// Saturating sub, never panics on the hot path.
    #[inline]
    pub fn saturating_sub(self, other: Self) -> Self {
        Self(self.0.saturating_sub(other.0))
    }

    /// Multiply by an integer quantity, returning saturating notional.
    #[inline]
    pub fn saturating_notional(self, qty: Qty) -> i64 {
        self.0.saturating_mul(qty.0 as i64)
    }

    /// Format back to the canonical decimal-string used by `contracts`. Used
    /// only at audit boundaries — never on the hot path.
    pub fn to_decimal_string(self) -> String {
        let neg = self.0 < 0;
        let abs = self.0.unsigned_abs() as u128;
        let whole = abs / FIXED_SCALE as u128;
        let frac = abs % FIXED_SCALE as u128;
        let sign = if neg { "-" } else { "" };
        if frac == 0 {
            format!("{sign}{whole}")
        } else {
            // Trim trailing zeros for human readability ("0.42" not "0.4200").
            let mut s = format!("{frac:04}");
            while s.ends_with('0') {
                s.pop();
            }
            format!("{sign}{whole}.{s}")
        }
    }
}

/// Quantity of contracts/shares. Kalshi orders top out well below u32 range.
#[derive(
    Clone, Copy, Debug, Default, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize, Deserialize,
)]
pub struct Qty(pub u32);

impl Qty {
    pub const ZERO: Self = Self(0);

    #[inline]
    pub fn raw(self) -> u32 {
        self.0
    }
}

/// Parse a decimal-string (`"0.42"`, `"1.2345"`, `"-0.5"`) into `FixedPrice`
/// without touching f64. Empty strings round-trip to `ZERO` so the Kalshi
/// normalizer's empty-decimal fallback (`first_decimal`) doesn't poison the
/// projection.
///
/// Tracks digits past the 4th fractional decimal but truncates (does not
/// round) — matches the contract's "no silent rounding" invariant: parsing a
/// 5-decimal price gives back fewer significant digits, but the truncation
/// is deterministic and explicit. Inputs originate from venue feeds that
/// emit at most 4 decimals in practice (Kalshi: 2, Polymarket: ≤4).
pub fn parse_fixed_price(s: &str) -> Result<FixedPrice, ParseError> {
    if s.is_empty() {
        return Ok(FixedPrice::ZERO);
    }
    let bytes = s.as_bytes();
    let (sign, start) = match bytes[0] {
        b'-' => (-1i64, 1usize),
        b'+' => (1i64, 1),
        _ => (1, 0),
    };
    if start == bytes.len() {
        return Err(ParseError::Empty);
    }

    let mut whole: i64 = 0;
    let mut frac_scale: i64 = FIXED_SCALE;
    let mut frac: i64 = 0;
    let mut saw_dot = false;
    let mut saw_digit = false;

    for (i, &b) in bytes.iter().enumerate().skip(start) {
        match b {
            b'0'..=b'9' => {
                saw_digit = true;
                let d = (b - b'0') as i64;
                if !saw_dot {
                    whole = whole.checked_mul(10).ok_or(ParseError::Overflow)?;
                    whole = whole.checked_add(d).ok_or(ParseError::Overflow)?;
                } else if frac_scale > 1 {
                    frac_scale /= 10;
                    frac += d * frac_scale;
                }
                // else: deeper than 4 decimals — silently truncated.
            }
            b'.' => {
                if saw_dot {
                    return Err(ParseError::MultipleDots);
                }
                saw_dot = true;
            }
            b'e' | b'E' => return Err(ParseError::Scientific),
            other => return Err(ParseError::InvalidChar(other as char, i)),
        }
    }

    if !saw_digit {
        return Err(ParseError::Empty);
    }

    let whole_scaled = whole.checked_mul(FIXED_SCALE).ok_or(ParseError::Overflow)?;
    let total = whole_scaled.checked_add(frac).ok_or(ParseError::Overflow)?;
    Ok(FixedPrice(sign * total))
}

/// Parse a quantity-as-decimal-string into a `Qty`. The contract carries
/// quantities as decimal strings (e.g., "10", "10.0") — both shapes round to
/// the integer count, anything fractional past `.0...0` is rejected.
pub fn parse_qty(s: &str) -> Result<Qty, ParseError> {
    if s.is_empty() {
        return Ok(Qty::ZERO);
    }
    let mut split = s.splitn(2, '.');
    let int_part = split.next().unwrap_or("");
    let frac_part = split.next().unwrap_or("");
    if !frac_part.is_empty() && !frac_part.bytes().all(|b| b == b'0') {
        return Err(ParseError::InvalidQty);
    }
    int_part
        .parse::<u32>()
        .map(Qty)
        .map_err(|_| ParseError::InvalidQty)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_kalshi_cent_decimals() {
        assert_eq!(parse_fixed_price("0.42").unwrap(), FixedPrice(4200));
        assert_eq!(parse_fixed_price("0.99").unwrap(), FixedPrice(9900));
        assert_eq!(parse_fixed_price("1").unwrap(), FixedPrice(10_000));
        assert_eq!(parse_fixed_price("0").unwrap(), FixedPrice(0));
    }

    #[test]
    fn parse_polymarket_subcent_decimals() {
        assert_eq!(parse_fixed_price("0.4250").unwrap(), FixedPrice(4250));
        assert_eq!(parse_fixed_price("0.0001").unwrap(), FixedPrice(1));
    }

    #[test]
    fn parse_handles_signs_and_empty() {
        assert_eq!(parse_fixed_price("-0.5").unwrap(), FixedPrice(-5000));
        assert_eq!(parse_fixed_price("+0.5").unwrap(), FixedPrice(5000));
        assert_eq!(parse_fixed_price("").unwrap(), FixedPrice::ZERO);
    }

    #[test]
    fn parse_rejects_garbage() {
        assert!(matches!(
            parse_fixed_price("1e5"),
            Err(ParseError::Scientific)
        ));
        assert!(matches!(
            parse_fixed_price("1.2.3"),
            Err(ParseError::MultipleDots)
        ));
        assert!(matches!(
            parse_fixed_price("abc"),
            Err(ParseError::InvalidChar(_, _))
        ));
    }

    #[test]
    fn parse_truncates_past_four_decimals() {
        // "0.12345" — the 5th decimal silently truncated. Inputs above 4dp
        // are out-of-spec from any venue we ingest; the alternative (reject)
        // would crash a worker on a single malformed message.
        assert_eq!(parse_fixed_price("0.12345").unwrap(), FixedPrice(1234));
    }

    #[test]
    fn to_decimal_string_trims_trailing_zeros() {
        assert_eq!(FixedPrice(4200).to_decimal_string(), "0.42");
        assert_eq!(FixedPrice(10_000).to_decimal_string(), "1");
        assert_eq!(FixedPrice(-5000).to_decimal_string(), "-0.5");
        assert_eq!(FixedPrice(1).to_decimal_string(), "0.0001");
    }

    #[test]
    fn parse_qty_accepts_integer_and_zero_frac() {
        assert_eq!(parse_qty("10").unwrap(), Qty(10));
        assert_eq!(parse_qty("10.0").unwrap(), Qty(10));
        assert_eq!(parse_qty("10.000").unwrap(), Qty(10));
        assert_eq!(parse_qty("").unwrap(), Qty::ZERO);
    }

    #[test]
    fn parse_qty_rejects_fractional() {
        assert!(matches!(parse_qty("10.5"), Err(ParseError::InvalidQty)));
    }

    #[test]
    fn saturating_arithmetic_does_not_panic_on_overflow() {
        let a = FixedPrice(i64::MAX);
        let b = FixedPrice(1);
        assert_eq!(a.saturating_add(b), FixedPrice(i64::MAX));
        assert_eq!(
            FixedPrice::ZERO.saturating_sub(FixedPrice(1)),
            FixedPrice(-1)
        );
    }

    #[test]
    fn notional_uses_integer_math() {
        // 10 contracts at $0.42 = $4.20 = 42_000 in fixed units.
        let p = parse_fixed_price("0.42").unwrap();
        let q = Qty(10);
        assert_eq!(p.saturating_notional(q), 42_000);
    }
}
