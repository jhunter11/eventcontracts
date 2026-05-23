"""Deterministic replay engines and runner event sources."""

from __future__ import annotations

from collections.abc import Iterator

from eventcontracts.domain.events import NormalizedEvent
from eventcontracts.runner.ports import EventSource
from eventcontracts.storage.interfaces import (
    EventEnvelope,
    EventStore,
    NormalizedEventStore,
)


class ReplayEngine:
    """Reads persisted events and yields them in replay order."""

    def replay(self) -> Iterator[EventEnvelope]:
        raise NotImplementedError


class RawReplayEngine(ReplayEngine):
    """Replay raw envelopes from an ``EventStore``.

    Handoff:
    ``storage.EventEnvelope`` values enter from storage and remain raw. This is
    useful for normalizer parity tests and debugging capture issues before
    strategy-facing events are produced.
    """

    def __init__(self, store: EventStore, *, source: str = "*") -> None:
        self.store = store
        self.source = source

    def replay(self) -> Iterator[EventEnvelope]:
        yield from self.store.read(self.source)


class NormalizedReplaySource(EventSource):
    """Expose normalized persisted events through the runner ``EventSource`` port.

    Handoff:
    ``storage.NormalizedEventStore`` -> ``runner.EventSource`` ->
    ``StrategyRunner.run`` -> ``Strategy.on_event``.
    """

    def __init__(self, store: NormalizedEventStore) -> None:
        self.store = store

    def stream(self) -> Iterator[NormalizedEvent]:
        yield from self.store.read_normalized()
