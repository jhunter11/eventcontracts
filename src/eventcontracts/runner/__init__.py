"""Strategy runner: owns the event loop, lifecycle, and integration points."""

from eventcontracts.runner.base import RunSummary, StrategyRunner
from eventcontracts.runner.inmemory import (
    AllowAllRiskGate,
    InMemoryClock,
    InMemoryContext,
    InMemoryEventSource,
    InMemoryIntentSink,
    InMemoryStateStore,
    StaticContextProvider,
    collect,
)
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
    "AllowAllRiskGate",
    "Clock",
    "ContextProvider",
    "EventSource",
    "InMemoryClock",
    "InMemoryContext",
    "InMemoryEventSource",
    "InMemoryIntentSink",
    "InMemoryStateStore",
    "IntentSink",
    "RiskDecision",
    "RiskGate",
    "RunSummary",
    "StateStore",
    "StaticContextProvider",
    "StrategyRunner",
    "collect",
]
