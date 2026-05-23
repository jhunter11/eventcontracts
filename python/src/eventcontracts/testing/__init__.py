"""Test doubles for in-memory wiring of the strategy runner.

Production code must not import from this package. Tests and local
backtest experiments use the in-memory ports here to build an end-to-end
loop without I/O.
"""

from eventcontracts.testing.doubles import (
    AllowAllRiskGate,
    InMemoryClock,
    InMemoryContext,
    InMemoryEventSource,
    InMemoryIntentSink,
    InMemoryStateStore,
    StaticContextProvider,
    collect,
)

__all__ = [
    "AllowAllRiskGate",
    "InMemoryClock",
    "InMemoryContext",
    "InMemoryEventSource",
    "InMemoryIntentSink",
    "InMemoryStateStore",
    "StaticContextProvider",
    "collect",
]
