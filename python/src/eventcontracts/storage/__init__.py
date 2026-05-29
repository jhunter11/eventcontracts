"""Storage interfaces and local implementations."""

from __future__ import annotations

from typing import TYPE_CHECKING

from eventcontracts.storage.inmemory import InMemoryEventStore
from eventcontracts.storage.interfaces import (
    EventEnvelope,
    EventStore,
    NormalizationReject,
    NormalizationRejectStore,
    NormalizedEventStore,
)
from eventcontracts.storage.sorting import envelope_sort_key, normalized_event_sort_key
from eventcontracts.storage.state_store import FileStateStore

if TYPE_CHECKING:
    from eventcontracts.storage.duckdb_store import DuckDbEventStore
    from eventcontracts.storage.parquet_store import ParquetEventStore


def __getattr__(name: str) -> object:
    """Lazily import optional storage backends.

    Importing `eventcontracts.storage.interfaces` should not require DuckDB or
    PyArrow. Concrete backends still fail clearly if their runtime dependency is
    missing.
    """

    if name == "DuckDbEventStore":
        from eventcontracts.storage.duckdb_store import (
            DuckDbEventStore as _DuckDbEventStore,
        )

        return _DuckDbEventStore
    if name == "ParquetEventStore":
        from eventcontracts.storage.parquet_store import (
            ParquetEventStore as _ParquetEventStore,
        )

        return _ParquetEventStore
    raise AttributeError(name)

__all__ = [
    "DuckDbEventStore",
    "EventEnvelope",
    "EventStore",
    "FileStateStore",
    "InMemoryEventStore",
    "NormalizationReject",
    "NormalizationRejectStore",
    "NormalizedEventStore",
    "ParquetEventStore",
    "envelope_sort_key",
    "normalized_event_sort_key",
]
