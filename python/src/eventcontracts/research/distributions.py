"""Latent distributions for coherent ladder pricing.

``ladder_cdf`` prices a whole bracket ladder off one latent distribution. Its
built-in normal/logistic CDF is fine for near-Gaussian underlyings (CPI), but
the data argues for two more shapes:

* **Student-t** — crypto daily settlement has fat tails; a normal underprices
  the out-of-the-money strikes (exactly the ones that pay). A scaled Student-t
  with low-ish degrees of freedom matches the excess kurtosis while keeping a
  given mean and standard deviation. Pairs with HAR-RV volatility (Corsi 2009).
* **Discrete** — an FOMC decision is a few point masses on a 25 bp grid, not a
  continuous density. The CME-FedWatch fair value is a discrete distribution;
  pricing it with a continuous CDF is the wrong object entirely.

Every distribution exposes the same small surface so producers and pricers stay
distribution-agnostic:

    cdf(x) -> P(X <= x)
    prob_above(x) -> P(X > x)          # ">= threshold" / cumulative ladders
    prob_in(lo, hi) -> P(lo < X <= hi) # range / exclusive ladders
    mean, stddev                        # summary moments

References:
* Corsi (2009), "A Simple Approximate Long-Memory Model of Realized Volatility".
* CME Group, "Understanding the CME Group FedWatch Tool Methodology" (2023).
"""

from __future__ import annotations

import math
from abc import abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

_SQRT2 = math.sqrt(2.0)
_SQRT3 = math.sqrt(3.0)


@runtime_checkable
class Distribution(Protocol):
    """Minimal pricing surface shared by every latent distribution."""

    @property
    def mean(self) -> float: ...

    @property
    def stddev(self) -> float: ...

    def cdf(self, x: float) -> float: ...

    def prob_above(self, x: float) -> float: ...

    def prob_in(self, lo: float, hi: float) -> float: ...


class _ContinuousMixin:
    """Shared ``prob_above`` / ``prob_in`` for continuous CDFs."""

    @abstractmethod
    def cdf(self, x: float) -> float:  # overridden by every concrete subclass
        ...

    def prob_above(self, x: float) -> float:
        return 1.0 - self.cdf(x)

    def prob_in(self, lo: float, hi: float) -> float:
        if hi < lo:
            raise ValueError("hi must be >= lo")
        return max(0.0, self.cdf(hi) - self.cdf(lo))


@dataclass(frozen=True)
class Normal(_ContinuousMixin):
    mean: float
    stddev: float

    def __post_init__(self) -> None:
        if self.stddev <= 0:
            raise ValueError("stddev must be > 0")

    def cdf(self, x: float) -> float:
        return 0.5 * (1.0 + math.erf((x - self.mean) / (self.stddev * _SQRT2)))


@dataclass(frozen=True)
class Logistic(_ContinuousMixin):
    """Logistic with scale matched to ``stddev`` (s = stddev*sqrt(3)/pi)."""

    mean: float
    stddev: float

    def __post_init__(self) -> None:
        if self.stddev <= 0:
            raise ValueError("stddev must be > 0")

    def cdf(self, x: float) -> float:
        s = self.stddev * _SQRT3 / math.pi
        return 1.0 / (1.0 + math.exp(-(x - self.mean) / s))


@dataclass(frozen=True)
class StudentT(_ContinuousMixin):
    """Scaled Student-t with a given mean and standard deviation.

    The raw t with ``dof`` degrees of freedom has variance ``dof/(dof-2)``; we
    scale by ``stddev*sqrt((dof-2)/dof)`` so the result has variance
    ``stddev**2`` while keeping the heavy tails. ``dof`` must be > 2 for a finite
    variance; crypto daily returns sit around 3-6.
    """

    mean: float
    stddev: float
    dof: float = 4.0

    def __post_init__(self) -> None:
        if self.stddev <= 0:
            raise ValueError("stddev must be > 0")
        if self.dof <= 2:
            raise ValueError("dof must be > 2 for a finite variance")

    @property
    def _scale(self) -> float:
        return self.stddev * math.sqrt((self.dof - 2.0) / self.dof)

    def cdf(self, x: float) -> float:
        from scipy.stats import t as _t  # type: ignore[import-untyped]  # local import keeps scipy off cold paths

        return float(_t.cdf((x - self.mean) / self._scale, self.dof))


@dataclass(frozen=True)
class DiscreteDistribution:
    """Point masses on discrete levels (e.g. FOMC target-rate grid)."""

    masses: Mapping[float, float]

    def __post_init__(self) -> None:
        if not self.masses:
            raise ValueError("masses must not be empty")
        total = sum(self.masses.values())
        if total <= 0:
            raise ValueError("mass total must be > 0")
        if any(p < 0 for p in self.masses.values()):
            raise ValueError("masses must be non-negative")
        if abs(total - 1.0) > 1e-6:
            object.__setattr__(self, "masses", {k: v / total for k, v in self.masses.items()})

    @property
    def mean(self) -> float:
        return sum(level * p for level, p in self.masses.items())

    @property
    def stddev(self) -> float:
        mu = self.mean
        return math.sqrt(sum(p * (level - mu) ** 2 for level, p in self.masses.items()))

    def cdf(self, x: float) -> float:
        return sum(p for level, p in self.masses.items() if level <= x)

    def prob_above(self, x: float) -> float:
        return sum(p for level, p in self.masses.items() if level > x)

    def prob_in(self, lo: float, hi: float) -> float:
        return sum(p for level, p in self.masses.items() if lo < level <= hi)


def build_continuous(mean: float, stddev: float, dist: str, *, dof: float = 4.0) -> Distribution:
    """Factory mirroring ``ladder_cdf``'s ``dist`` parameter, plus ``student_t``."""

    key = dist.lower()
    if key == "normal":
        return Normal(mean, stddev)
    if key == "logistic":
        return Logistic(mean, stddev)
    if key in ("student_t", "studentt", "t"):
        return StudentT(mean, stddev, dof)
    raise ValueError(f"unknown distribution {dist!r}")
