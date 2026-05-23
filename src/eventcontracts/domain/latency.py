"""Latency and execution-priority contracts.

Live execution will have scarce routing capacity: venue rate limits, gateway
workers, network paths, and risk checks. These types let a strategy declare
whether a decision is latency-sensitive without bypassing risk.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from eventcontracts.domain.validation import require_non_negative_int


class LatencyTier(str, Enum):
    RELAXED = "relaxed"
    STANDARD = "standard"
    FAST = "fast"
    CRITICAL = "critical"


@dataclass(frozen=True)
class ExecutionPriority:
    """Gateway scheduling hint for order-affecting decisions.

    ``max_delay_ms`` is the strategy's desired decision-to-send budget.
    ``expires_after_ms`` lets the gateway drop stale intents instead of sending
    an order after the edge has likely decayed.
    """

    tier: LatencyTier = LatencyTier.STANDARD
    max_delay_ms: int | None = None
    expires_after_ms: int | None = None
    allow_rate_limit_borrow: bool = False
    reason: str = ""

    def __post_init__(self) -> None:
        if self.max_delay_ms is not None:
            require_non_negative_int(self.max_delay_ms, "max_delay_ms")
        if self.expires_after_ms is not None:
            require_non_negative_int(self.expires_after_ms, "expires_after_ms")


DEFAULT_EXECUTION_PRIORITY = ExecutionPriority()
