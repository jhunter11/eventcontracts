"""BTC daily/terminal settlement pricer (KXBTCD family).

``btc_settlement`` prices the *within-window* 60 s average for 15-minute
contracts. The daily ``KXBTCD`` strikes settle on the terminal index level at a
fixed time, so the right object is the **terminal-price distribution** over a
multi-hour horizon, not the Asian-average kernel.

Model: the cumulative log return ``X = ln(S_T / S_0)`` over the remaining
horizon is fat-tailed (crypto excess kurtosis), so ``X ~ StudentT(mu, sigma,
dof)`` with ``sigma`` from an HAR-RV forecast (see :mod:`har_rv`) scaled to the
horizon, and a martingale drift ``mu = -0.5 sigma**2`` so ``E[S_T] = S_0``.
Then ``P(S_T >= K) = P(X >= ln(K/S_0))``. A normal tail (``dof -> inf``) is the
special case; low ``dof`` lifts the out-of-the-money strikes that a normal
underprices -- exactly where the ladder edge tends to hide.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from eventcontracts.research.distributions import Normal, StudentT

_SECS_PER_DAY = 86400.0
_DAYS_PER_YEAR = 365.0


@dataclass(frozen=True)
class BtcTerminalModel:
    """Terminal-price distribution for a fixed-time crypto settlement."""

    spot: float
    horizon_sigma: float  # stddev of cumulative log-return to settlement
    dof: float = 4.0
    extra_drift_logret: float = 0.0

    def __post_init__(self) -> None:
        if self.spot <= 0:
            raise ValueError("spot must be > 0")
        if self.horizon_sigma <= 0:
            raise ValueError("horizon_sigma must be > 0")

    @property
    def _mu(self) -> float:
        # Martingale correction so E[S_T] = S_0 under the (approx) lognormal.
        return self.extra_drift_logret - 0.5 * self.horizon_sigma**2

    def _logret_dist(self) -> StudentT | Normal:
        if self.dof == math.inf:
            return Normal(self._mu, self.horizon_sigma)
        return StudentT(self._mu, self.horizon_sigma, self.dof)

    def prob_above(self, strike: float) -> float:
        if strike <= 0:
            return 1.0
        return self._logret_dist().prob_above(math.log(strike / self.spot))

    def prob_below(self, strike: float) -> float:
        return 1.0 - self.prob_above(strike)

    def prob_in(self, low_strike: float, high_strike: float) -> float:
        if high_strike < low_strike:
            raise ValueError("high_strike must be >= low_strike")
        lo = math.log(max(low_strike, 1e-9) / self.spot)
        hi = math.log(max(high_strike, 1e-9) / self.spot)
        return self._logret_dist().prob_in(lo, hi)


def horizon_sigma_from_daily_vol(daily_vol: float, seconds_to_settle: float) -> float:
    """Scale a 1-day return stddev to the settlement horizon (random walk)."""

    if daily_vol < 0 or seconds_to_settle < 0:
        raise ValueError("inputs must be >= 0")
    return daily_vol * math.sqrt(seconds_to_settle / _SECS_PER_DAY)


def horizon_sigma_from_annual_vol(annual_vol: float, seconds_to_settle: float) -> float:
    """Scale an annualized vol to the settlement horizon (random walk)."""

    if annual_vol < 0 or seconds_to_settle < 0:
        raise ValueError("inputs must be >= 0")
    return annual_vol * math.sqrt(seconds_to_settle / (_DAYS_PER_YEAR * _SECS_PER_DAY))
