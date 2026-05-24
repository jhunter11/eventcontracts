"""Storage interfaces and local implementations."""

from eventcontracts.storage.duckdb_store import DuckDbEventStore
from eventcontracts.storage.inmemory import (
    InMemoryEventStore,
    envelope_sort_key,
    normalized_event_sort_key,
)
from eventcontracts.storage.interfaces import (
    EventEnvelope,
    EventStore,
    NormalizedEventStore,
)
from eventcontracts.storage.parquet_store import ParquetEventStore

__all__ = [
    "DuckDbEventStore",
    "EventEnvelope",
    "EventStore",
    "InMemoryEventStore",
    "NormalizedEventStore",
    "ParquetEventStore",
    "envelope_sort_key",
    "normalized_event_sort_key",
]
