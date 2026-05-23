"""Deterministic replay engine boundary."""

from __future__ import annotations

from collections.abc import Iterator

from eventcontracts.storage.interfaces import EventEnvelope


class ReplayEngine:
    """Reads persisted events and yields them in replay order."""

    def replay(self) -> Iterator[EventEnvelope]:
        raise NotImplementedError
