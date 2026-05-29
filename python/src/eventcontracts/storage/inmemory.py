"""In-memory stores for local vertical-slice tests."""

from __future__ import annotations

from dataclasses import dataclass, field

from eventcontracts.domain.events import NormalizedEvent
from eventcontracts.storage.interfaces import EventEnvelope
from eventcontracts.storage.sorting import envelope_sort_key, normalized_event_sort_key


@dataclass
class InMemoryEventStore:
    """A local store implementing raw and normalized persistence ports."""

    raw_events: list[EventEnvelope] = field(default_factory=list)
    normalized_events: list[NormalizedEvent] = field(default_factory=list)

    def append(self, event: EventEnvelope) -> None:
        self.raw_events.append(event)

    def read(self, source: str) -> tuple[EventEnvelope, ...]:
        events = (
            event
            for event in self.raw_events
            if source == "*" or event.source == source
        )
        return tuple(sorted(events, key=envelope_sort_key))

    def append_normalized(self, event: NormalizedEvent) -> None:
        self.normalized_events.append(event)

    def read_normalized(self) -> tuple[NormalizedEvent, ...]:
        return tuple(sorted(self.normalized_events, key=normalized_event_sort_key))
