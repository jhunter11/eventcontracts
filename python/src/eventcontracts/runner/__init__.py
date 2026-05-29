"""Strategy runner: owns the event loop, lifecycle, and integration points."""

from __future__ import annotations

import warnings
from typing import Any

from eventcontracts.runner.base import RunSummary, StepResult, StrategyRunner
from eventcontracts.runner.context import StatefulContext, StatefulContextProvider
from eventcontracts.runner.ports import (
    Clock,
    ContextProvider,
    EventSource,
    IntentSink,
    RiskDecision,
    RiskGate,
    StateStore,
)

__all__ = [
    "Clock",
    "ContextProvider",
    "EventSource",
    "IntentSink",
    "RiskDecision",
    "RiskGate",
    "RunSummary",
    "StepResult",
    "StateStore",
    "StatefulContext",
    "StatefulContextProvider",
    "StrategyRunner",
]

_TEST_DOUBLE_NAMES = frozenset(
    {
        "AllowAllRiskGate",
        "InMemoryClock",
        "InMemoryContext",
        "InMemoryEventSource",
        "InMemoryIntentSink",
        "InMemoryStateStore",
        "StaticContextProvider",
        "collect",
    }
)


def __getattr__(name: str) -> Any:
    if name in _TEST_DOUBLE_NAMES:
        warnings.warn(
            f"eventcontracts.runner.{name} moved to eventcontracts.testing."
            f" Import from eventcontracts.testing instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        from eventcontracts import testing

        return getattr(testing, name)
    raise AttributeError(f"module 'eventcontracts.runner' has no attribute {name!r}")
