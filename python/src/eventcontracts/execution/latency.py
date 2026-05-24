"""Latency models for paper execution.

A latency model returns a draw representing how long it takes for an
intent to reach the venue and how long until acknowledgement arrives
back. The base unit is :class:`~datetime.timedelta`. Implementations
are seeded for determinism so replay runs reproduce identical fills.
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Protocol


class LatencyModel(Protocol):
    """Draw a latency for the next intent."""

    def draw(self, kind: str = "submit") -> timedelta: ...


@dataclass(frozen=True)
class ConstantLatency:
    """Returns a fixed latency for every draw.

    Useful for tests and for a sleeve that has a measured median round trip
    and does not care about jitter.
    """

    submit_ms: float = 50.0
    cancel_ms: float = 50.0
    replace_ms: float = 50.0

    def draw(self, kind: str = "submit") -> timedelta:
        match kind:
            case "submit":
                return timedelta(milliseconds=self.submit_ms)
            case "cancel":
                return timedelta(milliseconds=self.cancel_ms)
            case "replace":
                return timedelta(milliseconds=self.replace_ms)
            case _:
                return timedelta(milliseconds=self.submit_ms)


@dataclass
class LognormalLatency:
    """Lognormally-distributed latency seeded for replay determinism.

    The lognormal has thicker tails than a Gaussian, which matches what
    real venues exhibit: most submits complete near the median but a
    small fraction experience multi-second delays. Pass ``seed`` to
    pin the random stream.
    """

    median_ms: float = 80.0
    sigma: float = 0.4
    floor_ms: float = 5.0
    ceiling_ms: float = 5_000.0
    seed: int = 0
    _rng: random.Random = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    def draw(self, kind: str = "submit") -> timedelta:
        mu = math.log(self.median_ms)
        sample = self._rng.lognormvariate(mu, self.sigma)
        clipped = max(self.floor_ms, min(self.ceiling_ms, sample))
        return timedelta(milliseconds=clipped)


@dataclass
class LookupLatency:
    """Latency by kind, with a default for unknown keys.

    Useful when submit/cancel/replace round trips differ — e.g. a venue
    may acknowledge cancels faster than new orders.
    """

    by_kind: Mapping[str, float]
    default_ms: float = 50.0

    def draw(self, kind: str = "submit") -> timedelta:
        ms = self.by_kind.get(kind, self.default_ms)
        return timedelta(milliseconds=ms)
