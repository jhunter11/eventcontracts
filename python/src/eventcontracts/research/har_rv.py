"""HAR-RV realized-volatility forecasting (Corsi 2009) for crypto settlement.

Realized variance over a day is the sum of squared high-frequency returns.
Corsi's Heterogeneous Autoregressive model forecasts next-day RV from three
horizons -- daily, weekly (5d), monthly (22d) -- a parsimonious proxy for the
long-memory persistence of volatility:

    RV_{t+1} = b0 + b_d * RV^d_t + b_w * RV^w_t + b_m * RV^m_t + e

Crypto RV is highly non-normal with extreme spikes, so we fit in **log space**
by default (Corsi's Log-HAR-RV, the more robust variant in the crypto
literature), then exponentiate. The 1-day forecast scales to a multi-day
settlement horizon under a random walk (Var_H = H * RV_next), feeding a
fat-tailed terminal distribution in the pricer.

References: Corsi (2009), "A Simple Approximate Long-Memory Model of Realized
Volatility", J. Financial Econometrics; crypto Log-HAR / HAR-RV-J extensions
(e.g. arXiv:2507.22409, arXiv:2508.15922).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np


def log_returns(prices: Sequence[float]) -> list[float]:
    """Close-to-close log returns from a price path."""

    out: list[float] = []
    for prev, cur in zip(prices, prices[1:], strict=False):
        if prev > 0 and cur > 0:
            out.append(math.log(cur / prev))
    return out


def realized_variance(returns: Sequence[float]) -> float:
    """Sum of squared returns -- the realized variance estimator."""

    return float(sum(r * r for r in returns))


def realized_semivariance(returns: Sequence[float]) -> tuple[float, float]:
    """Signed realized variance: ``(RS_plus, RS_minus)`` (Barndorff-Nielsen 2008).

    Downside (``RS_minus``) and upside (``RS_plus``) variance forecast future
    volatility asymmetrically -- the leverage effect. ``RS_plus + RS_minus == RV``.
    """

    pos = float(sum(r * r for r in returns if r > 0))
    neg = float(sum(r * r for r in returns if r < 0))
    return pos, neg


def bipower_variation(returns: Sequence[float]) -> float:
    """Realized bipower variation -- a jump-robust estimator of the *continuous*
    variation (Barndorff-Nielsen & Shephard 2004). RV - BPV estimates the jump.
    """

    n = len(returns)
    if n < 2:
        return 0.0
    s = sum(abs(returns[i - 1]) * abs(returns[i]) for i in range(1, n))
    return (math.pi / 2.0) * (n / (n - 1.0)) * float(s)


def continuous_jump(returns: Sequence[float]) -> tuple[float, float]:
    """Split RV into ``(continuous, jump)`` via bipower variation."""

    rv = realized_variance(returns)
    jump = max(rv - bipower_variation(returns), 0.0)
    return rv - jump, jump


@dataclass(frozen=True)
class HARRVFit:
    """Fitted HAR-RV coefficients plus the config used to build features."""

    beta0: float
    beta_d: float
    beta_w: float
    beta_m: float
    weekly: int
    monthly: int
    log_space: bool
    r_squared: float
    n_obs: int

    def forecast(self, rv_recent: Sequence[float]) -> float:
        """Forecast next-day realized variance from the tail of an RV series."""

        if len(rv_recent) < self.monthly:
            raise ValueError(f"need >= {self.monthly} RV points to forecast")
        d, w, m = _components(rv_recent, len(rv_recent) - 1, self.weekly, self.monthly)
        if self.log_space:
            d, w, m = math.log(max(d, 1e-18)), math.log(max(w, 1e-18)), math.log(max(m, 1e-18))
        pred = self.beta0 + self.beta_d * d + self.beta_w * w + self.beta_m * m
        return math.exp(pred) if self.log_space else max(pred, 1e-18)

    def horizon_sigma(self, rv_recent: Sequence[float], horizon_days: float) -> float:
        """Forecast standard deviation of cumulative return over ``horizon_days``.

        Random-walk scaling of the 1-day RV forecast: Var_H = H * RV_next.
        """

        if horizon_days < 0:
            raise ValueError("horizon_days must be >= 0")
        return math.sqrt(self.forecast(rv_recent) * horizon_days)


def _components(rv: Sequence[float], i: int, weekly: int, monthly: int) -> tuple[float, float, float]:
    daily = rv[i]
    week = sum(rv[i - weekly + 1 : i + 1]) / weekly
    month = sum(rv[i - monthly + 1 : i + 1]) / monthly
    return daily, week, month


class HARRV:
    """Heterogeneous Autoregressive model of realized volatility."""

    def __init__(self, *, weekly: int = 5, monthly: int = 22, log_space: bool = True) -> None:
        if not 1 < weekly < monthly:
            raise ValueError("require 1 < weekly < monthly")
        self.weekly = weekly
        self.monthly = monthly
        self.log_space = log_space

    def fit(self, rv: Sequence[float]) -> HARRVFit:
        if len(rv) < self.monthly + 2:
            raise ValueError(f"need >= {self.monthly + 2} RV observations to fit, got {len(rv)}")
        series = [math.log(max(v, 1e-18)) for v in rv] if self.log_space else list(rv)
        rows: list[list[float]] = []
        targets: list[float] = []
        for i in range(self.monthly - 1, len(rv) - 1):
            d, w, m = _components(rv, i, self.weekly, self.monthly)
            if self.log_space:
                d, w, m = math.log(max(d, 1e-18)), math.log(max(w, 1e-18)), math.log(max(m, 1e-18))
            rows.append([1.0, d, w, m])
            targets.append(series[i + 1])
        x = np.asarray(rows, dtype=float)
        y = np.asarray(targets, dtype=float)
        beta, *_ = np.linalg.lstsq(x, y, rcond=None)
        resid = y - x @ beta
        ss_res = float(resid @ resid)
        ss_tot = float(((y - y.mean()) ** 2).sum())
        r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
        return HARRVFit(
            beta0=float(beta[0]),
            beta_d=float(beta[1]),
            beta_w=float(beta[2]),
            beta_m=float(beta[3]),
            weekly=self.weekly,
            monthly=self.monthly,
            log_space=self.log_space,
            r_squared=r2,
            n_obs=len(targets),
        )


# --- HAR family with realized-semivariance / jump components -----------------
#
# Liu, Fu & Hong (2025, arXiv:2503.00851) Table 4: on equities, decomposing RV
# lifts the next-day RV regression Adj R^2 from ~0.41 (plain HAR) to ~0.58
# (HAR-RS, signed semivariance) and ~0.50 (HAR-CJ, continuous+jump). Crypto has
# the same leverage asymmetry and jumpiness, so these variants are the upgrade.
# Each consumes *intraday returns per day* (needed for the signed/jump split)
# rather than a pre-reduced RV series.

_VARIANT_COMPONENTS: dict[str, tuple[str, ...]] = {
    "har": ("rv",),
    "har_rs": ("rs_pos", "rs_neg"),
    "har_cj": ("cv", "jump"),
}


def _day_components(returns: Sequence[float]) -> dict[str, float]:
    rs_pos, rs_neg = realized_semivariance(returns)
    cv, jump = continuous_jump(returns)
    return {"rv": realized_variance(returns), "rs_pos": rs_pos, "rs_neg": rs_neg, "cv": cv, "jump": jump}


def _components_by_key(daily_returns: Sequence[Sequence[float]]) -> dict[str, list[float]]:
    days = [_day_components(r) for r in daily_returns]
    return {key: [d[key] for d in days] for key in ("rv", "rs_pos", "rs_neg", "cv", "jump")}


def _feature_row(variant: str, by_key: dict[str, list[float]], i: int, weekly: int, monthly: int) -> list[float]:
    row = [1.0]
    for key in _VARIANT_COMPONENTS[variant]:
        series = by_key[key]
        row.append(series[i])
        row.append(sum(series[i - weekly + 1 : i + 1]) / weekly)
        row.append(sum(series[i - monthly + 1 : i + 1]) / monthly)
    return row


@dataclass(frozen=True)
class HARFamilyFit:
    """A fitted HAR-family model forecasting next-day realized variance (level)."""

    variant: str
    coefficients: tuple[float, ...]
    feature_names: tuple[str, ...]
    weekly: int
    monthly: int
    r_squared: float
    n_obs: int

    def forecast(self, daily_returns: Sequence[Sequence[float]]) -> float:
        if len(daily_returns) < self.monthly:
            raise ValueError(f"need >= {self.monthly} days of returns to forecast")
        by_key = _components_by_key(daily_returns)
        row = _feature_row(self.variant, by_key, len(daily_returns) - 1, self.weekly, self.monthly)
        pred = sum(c * x for c, x in zip(self.coefficients, row, strict=True))
        return max(pred, 1e-18)

    def horizon_sigma(self, daily_returns: Sequence[Sequence[float]], horizon_days: float) -> float:
        if horizon_days < 0:
            raise ValueError("horizon_days must be >= 0")
        return math.sqrt(self.forecast(daily_returns) * horizon_days)


class HARFamily:
    """HAR with optional semivariance (``har_rs``) or jump (``har_cj``) split."""

    def __init__(self, *, weekly: int = 5, monthly: int = 22, variant: str = "har_rs", ridge: float = 0.0) -> None:
        if variant not in _VARIANT_COMPONENTS:
            raise ValueError(f"variant must be one of {sorted(_VARIANT_COMPONENTS)}")
        if not 1 < weekly < monthly:
            raise ValueError("require 1 < weekly < monthly")
        if ridge < 0:
            raise ValueError("ridge must be >= 0")
        self.weekly = weekly
        self.monthly = monthly
        self.variant = variant
        self.ridge = ridge

    def fit(self, daily_returns: Sequence[Sequence[float]]) -> HARFamilyFit:
        if len(daily_returns) < self.monthly + 2:
            raise ValueError(f"need >= {self.monthly + 2} days, got {len(daily_returns)}")
        by_key = _components_by_key(daily_returns)
        rv = by_key["rv"]
        rows: list[list[float]] = []
        targets: list[float] = []
        for i in range(self.monthly - 1, len(daily_returns) - 1):
            rows.append(_feature_row(self.variant, by_key, i, self.weekly, self.monthly))
            targets.append(rv[i + 1])
        full = np.asarray(rows, dtype=float)  # column 0 is the intercept
        y = np.asarray(targets, dtype=float)
        # Ridge on standardized slope features (intercept unpenalized). The signed
        # semivariance components are collinear, so unregularized OLS extrapolates
        # to degenerate (negative) forecasts on short crypto samples; ridge is the
        # L2 analogue of the LASSO-HAR the literature uses to control this.
        xr = full[:, 1:]
        mu = xr.mean(axis=0)
        sd = xr.std(axis=0)
        sd[sd == 0.0] = 1.0
        z = (xr - mu) / sd
        ybar = float(y.mean())
        p = z.shape[1]
        b = np.linalg.solve(z.T @ z + self.ridge * np.eye(p), z.T @ (y - ybar))
        raw_slopes = b / sd
        raw_intercept = ybar - float((b * mu / sd).sum())
        coef = np.concatenate(([raw_intercept], raw_slopes))
        resid = y - full @ coef
        ss_res = float(resid @ resid)
        ss_tot = float(((y - ybar) ** 2).sum())
        r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
        names = ["const"]
        for key in _VARIANT_COMPONENTS[self.variant]:
            names += [f"{key}_d", f"{key}_w", f"{key}_m"]
        return HARFamilyFit(
            variant=self.variant,
            coefficients=tuple(float(c) for c in coef),
            feature_names=tuple(names),
            weekly=self.weekly,
            monthly=self.monthly,
            r_squared=r2,
            n_obs=len(targets),
        )
