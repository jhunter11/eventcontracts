"""Ingestion pipeline boundaries."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from eventcontracts.domain.models import Venue
from eventcontracts.domain.validation import require_non_empty, require_optional_aware_datetime
from eventcontracts.storage.interfaces import EventEnvelope, EventStore


@dataclass(frozen=True)
class IngestionJob:
    name: str
    venue: Venue | None
    source: str
    starts_at: datetime | None = None
    ends_at: datetime | None = None

    def __post_init__(self) -> None:
        require_non_empty(self.name, "name")
        require_non_empty(self.source, "source")
        require_optional_aware_datetime(self.starts_at, "starts_at")
        require_optional_aware_datetime(self.ends_at, "ends_at")
        if self.starts_at is not None and self.ends_at is not None and self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at")


class CaptureSource(Protocol):
    """Venue or external adapter that emits raw envelopes.

    Handoff:
    adapter/client code yields ``EventEnvelope`` values.
    ``IngestionPipeline`` persists them to an ``EventStore`` before any
    normalization happens.
    """

    def capture(self, job: IngestionJob) -> Iterable[EventEnvelope]:
        """Yield raw events for a capture job."""


@dataclass(frozen=True)
class IterableCaptureSource:
    """Test/local capture source backed by prebuilt raw envelopes."""

    events: tuple[EventEnvelope, ...]

    def capture(self, job: IngestionJob) -> Iterable[EventEnvelope]:
        return (
            event
            for event in self.events
            if (job.venue is None or event.venue == job.venue)
            and event.source == job.source
        )


class IngestionPipeline:
    """Runs raw capture jobs and writes raw event envelopes."""

    def __init__(
        self,
        store: EventStore,
        sources: Mapping[str, CaptureSource],
    ) -> None:
        self.store = store
        self.sources = sources

    def run(self, job: IngestionJob) -> int:
        """Capture raw data and persist it unchanged.

        Returns the number of raw envelopes written. This is intentionally
        simple: retries, checkpoints, and backpressure belong in concrete
        capture services once venue APIs are implemented.
        """

        if job.source not in self.sources:
            raise KeyError(f"capture source not registered: {job.source}")

        written = 0
        for event in self.sources[job.source].capture(job):
            self.store.append(event)
            written += 1
        return written
