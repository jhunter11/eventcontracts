"""In-memory stores for local vertical-slice tests."""

from __future__ import annotations

from dataclasses import dataclass, field

from eventcontracts.domain.events import NormalizedEvent
from eventcontracts.storage.interfaces import EventEnvelope


def envelope_sort_key(event: EventEnvelope) -> tuple[str, str, str, str]:
    """Deterministic raw-event ordering for replay.

    Exchange time is preferred, receipt time is the fallback, then source,
    channel, and source sequence metadata provide stable tie-breakers.
    """

    event_time = event.exchange_ts or event.received_at
    sequence = str(event.metadata.get("source_sequence", ""))
    return (event_time.isoformat(), event.source, event.channel, sequence)


def normalized_event_sort_key(event: NormalizedEvent) -> tuple[str, str, str, str]:
    """Deterministic normalized-event ordering for replay."""

    provenance = event.provenance
    sequence = provenance.source_sequence or ""
    payload_time = getattr(
        getattr(event, "trade", None)
        or getattr(event, "quote", None)
        or getattr(event, "book", None)
        or getattr(event, "lifecycle", None),
        "exchange_ts",
        None,
    )
    received_at = getattr(
        getattr(event, "trade", None)
        or getattr(event, "quote", None)
        or getattr(event, "book", None)
        or getattr(event, "lifecycle", None),
        "received_at",
        None,
    )
    event_time = payload_time or received_at or getattr(event, "timestamp", None)
    return (
        event_time.isoformat() if event_time is not None else "",
        provenance.source,
        provenance.channel,
        sequence,
    )


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
