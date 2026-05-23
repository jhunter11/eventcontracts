"""DuckDB-backed storage placeholder."""

from __future__ import annotations

from pathlib import Path

from eventcontracts.storage.interfaces import EventEnvelope


class DuckDbEventStore:
    """Future local analytical store for raw and normalized event data."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, event: EventEnvelope) -> None:
        raise NotImplementedError
