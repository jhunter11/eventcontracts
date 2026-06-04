"""CPI nowcast -> release distribution for KXCPI ladder pricing.

The edge on a pre-release CPI ladder is a better view of the print than the
consensus the market anchors to. The Cleveland Fed publishes a free daily
inflation nowcast (FRED); that is the distribution **mean**. The **sigma** is the
historical nowcast error (how far the print has landed from the nowcast), and the
**delta vs consensus** is the directional signal (a hotter-than-consensus nowcast
shifts mass up the ladder). Fed to ``ladder_cdf`` as ``{mean, sigma, dist}``.

This is the producer the ``macro_cpi_cdf`` sleeve was missing; pulling the live
Cleveland nowcast + consensus is separate plumbing.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from eventcontracts.research.distributions import Distribution, build_continuous


@dataclass(frozen=True)
class CpiNowcast:
    """A CPI (YoY or MoM) nowcast with its surprise dispersion."""

    nowcast: float
    consensus: float
    surprise_sigma: float
    dist: str = "normal"
    dof: float = 5.0

    def __post_init__(self) -> None:
        if self.surprise_sigma <= 0:
            raise ValueError("surprise_sigma must be > 0")

    @property
    def delta(self) -> float:
        """Nowcast minus consensus -- positive = expect a hotter print than the crowd."""

        return self.nowcast - self.consensus

    def distribution(self) -> Distribution:
        return build_continuous(self.nowcast, self.surprise_sigma, self.dist, dof=self.dof)

    def ladder_signal_payload(self) -> dict[str, Any]:
        """The ``{mean, sigma, dist}`` payload ``ladder_cdf`` consumes."""

        return {"mean": self.nowcast, "sigma": self.surprise_sigma, "dist": self.dist}


def surprise_sigma_from_history(nowcasts: Sequence[float], actuals: Sequence[float]) -> float:
    """Std of realized nowcast errors ``actual - nowcast`` (the surprise scale)."""

    if len(nowcasts) != len(actuals) or len(nowcasts) < 2:
        raise ValueError("need >= 2 aligned (nowcast, actual) pairs")
    errors = [a - n for n, a in zip(nowcasts, actuals, strict=True)]
    return statistics.pstdev(errors)
