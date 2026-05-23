"""Replay clock primitives."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class ReplayWindow:
    starts_at: datetime
    ends_at: datetime

    def __post_init__(self) -> None:
        if self.ends_at <= self.starts_at:
            raise ValueError("replay window must end after it starts")


@dataclass
class ReplayClock:
    window: ReplayWindow
    step: timedelta

    def ticks(self) -> list[datetime]:
        current = self.window.starts_at
        ticks: list[datetime] = []
        while current <= self.window.ends_at:
            ticks.append(current)
            current += self.step
        return ticks
