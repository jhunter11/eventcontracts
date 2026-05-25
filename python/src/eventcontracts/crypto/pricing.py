"""Crypto-market predictive helpers.

Mathematical building blocks used by the 15-min crypto strategies under
``eventcontracts.plugins.strategies.crypto_*``. The module is intentionally
narrow:

* Black-Scholes-flavored probability that a TWAP-settled binary expires
  in the money for a given strike (:func:`bs_above_probability`).
* Bracket-parity deviation across a disjoint partition of strike ranges
  (:func:`bracket_parity_deviation`).
* Monotonicity violations for the cross-strike skew (:func:`monotone_violations`).
* Realized volatility from a price history (:func:`realized_volatility`).

All inputs are :class:`~decimal.Decimal` so price math stays exact at the
boundary; transcendental work happens in float internally and the result
returns to Decimal. Use these helpers from strategies — do not re-implement
the math inline.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

# ----------------------------- Black-Scholes -----------------------------


def _norm_cdf(x: float) -> float:
    """Standard normal CDF Φ(x) via the math.erf identity."""

    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_above_probability(
    *,
    spot: Decimal,
    strike: Decimal,
    sigma_annual: Decimal,
    tau_seconds: Decimal,
    twap_window_seconds: Decimal = Decimal("0"),
) -> Decimal:
    """Probability that the underlying is at or above ``strike`` at expiry.

    Uses the lognormal/Black-Scholes assumption with zero drift, which is
    the standard short-horizon approximation for crypto event contracts.
    Settlement on a TWAP shortens the *effective* terminal variance by
    one-third of the window length, so we subtract
    ``twap_window_seconds / 3`` from ``tau_seconds`` before taking the
    square root.

    Parameters
    ----------
    spot
        Current underlying price in the same units as ``strike``.
    strike
        Strike price of the binary contract.
    sigma_annual
        Annualized implied volatility (e.g. 0.55 for 55%).
    tau_seconds
        Time to expiry in seconds. Must be positive; near-zero values
        are floored at one second so the CDF stays well defined.
    twap_window_seconds
        Length of the settlement TWAP window in seconds. Pass 0 for
        instantaneous settlement.

    Returns
    -------
    Decimal
        Probability in ``[0, 1]`` clipped to ``[0.0001, 0.9999]`` to avoid
        degenerate orders at the boundary.
    """

    if spot <= 0 or strike <= 0 or sigma_annual <= 0:
        return Decimal("0.5")

    spot_f = float(spot)
    strike_f = float(strike)
    sigma_f = float(sigma_annual)

    seconds_per_year = 365.25 * 24 * 60 * 60
    tau = max(1.0, float(tau_seconds)) / seconds_per_year
    twap_correction = max(0.0, float(twap_window_seconds)) / 3.0 / seconds_per_year
    effective_tau = max(tau - twap_correction, 1.0 / seconds_per_year)

    vol_root = sigma_f * math.sqrt(effective_tau)
    d2 = (math.log(spot_f / strike_f) - 0.5 * sigma_f * sigma_f * effective_tau) / vol_root
    prob = _norm_cdf(d2)
    return _clip_probability(Decimal(str(prob)))


def _clip_probability(p: Decimal) -> Decimal:
    return max(Decimal("0.0001"), min(Decimal("0.9999"), p))


# ----------------------------- Parity / skew -----------------------------


@dataclass(frozen=True)
class StrikeBracket:
    """One bracket in a disjoint partition of the underlying's price space.

    A bracket is one Kalshi market ("Will BTC settle in [lower, upper)?").
    The full set of brackets for a single expiry covers the real line:
    ``(-inf, K1), [K1, K2), ..., [Kn, +inf)``.

    Strikes are inclusive on the lower side. The probability mass in a
    bracket equals ``P(S_T < upper) - P(S_T < lower)`` under any pricing
    model.
    """

    market_id: str
    lower: Decimal | None  # None = unbounded below
    upper: Decimal | None  # None = unbounded above

    def width(self) -> Decimal | None:
        if self.lower is None or self.upper is None:
            return None
        return self.upper - self.lower


def bracket_parity_deviation(probs: Mapping[str, Decimal]) -> Decimal:
    """``sum(probs) - 1`` for a disjoint, exhaustive bracket partition.

    A positive deviation means the brackets are collectively overpriced
    (sell all); negative means underpriced (buy all). The framework
    expects this to be a small number — anything beyond a few bps is
    likely arbitrage minus venue fees.
    """

    total = sum(probs.values(), Decimal("0"))
    return total - Decimal("1")


def monotone_violations(
    quotes_by_strike: Sequence[tuple[Decimal, Decimal]],
) -> tuple[tuple[Decimal, Decimal, Decimal, Decimal], ...]:
    """Detect non-monotonicity in ``P(S_T >= K)`` across ascending ``K``.

    ``quotes_by_strike`` must be sorted in ascending strike order. Each
    tuple is ``(strike, p_above)``. The function returns a tuple of
    violations as ``(strike_low, p_low, strike_high, p_high)`` where
    ``p_high > p_low`` even though ``strike_high > strike_low`` — i.e.
    the higher strike claims a *larger* P(above), which is impossible
    under any arbitrage-free pricing.
    """

    violations: list[tuple[Decimal, Decimal, Decimal, Decimal]] = []
    for i in range(len(quotes_by_strike) - 1):
        strike_low, p_low = quotes_by_strike[i]
        strike_high, p_high = quotes_by_strike[i + 1]
        if p_high > p_low:
            violations.append((strike_low, p_low, strike_high, p_high))
    return tuple(violations)


# ----------------------------- Realized vol -----------------------------


def realized_volatility(prices: Sequence[Decimal]) -> Decimal:
    """Annualized realized volatility from an evenly-spaced price series.

    Computes log returns and scales by ``√(samples_per_year)`` assuming
    one sample per second. Callers with a different sampling cadence
    should rescale by ``√(cadence_seconds / 1.0)`` outside this function.

    Returns ``Decimal("0")`` when the series is too short.
    """

    if len(prices) < 2:
        return Decimal("0")

    log_returns: list[float] = []
    prev = float(prices[0])
    for price in prices[1:]:
        cur = float(price)
        if prev <= 0 or cur <= 0:
            return Decimal("0")
        log_returns.append(math.log(cur / prev))
        prev = cur

    if not log_returns:
        return Decimal("0")

    mean = sum(log_returns) / len(log_returns)
    variance = sum((r - mean) ** 2 for r in log_returns) / max(1, len(log_returns) - 1)
    stddev = math.sqrt(variance)
    seconds_per_year = 365.25 * 24 * 60 * 60
    annual = stddev * math.sqrt(seconds_per_year)
    return Decimal(str(annual))
