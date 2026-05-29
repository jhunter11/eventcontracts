"""Storage boundaries for raw and normalized events."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from eventcontracts.domain.events import NormalizedEvent
from eventcontracts.domain.metadata import FrozenMap, freeze_mapping
from eventcontracts.domain.models import Venue
from eventcontracts.domain.validation import (
    require_aware_datetime,
    require_non_empty,
    require_not_future_datetime,
    require_optional_aware_datetime,
)


@dataclass(frozen=True)
class EventEnvelope:
    venue: Venue | None
    source: str
    channel: str
    received_at: datetime
    exchange_ts: datetime | None
    payload: Mapping[str, Any]
    schema_version: str
    metadata: Mapping[str, Any] = field(default_factory=FrozenMap)

    def __post_init__(self) -> None:
        require_non_empty(self.source, "source")
        require_non_empty(self.channel, "channel")
        require_aware_datetime(self.received_at, "received_at")
        require_not_future_datetime(self.received_at, "received_at")
        require_optional_aware_datetime(self.exchange_ts, "exchange_ts")
        require_non_empty(self.schema_version, "schema_version")
        object.__setattr__(self, "payload", freeze_mapping(self.payload))
        object.__setattr__(self, "metadata", freeze_mapping(self.metadata))


class EventStore(Protocol):
    def append(self, event: EventEnvelope) -> None:
        """Persist one raw or normalized event."""

    def read(self, source: str) -> Iterable[EventEnvelope]:
        """Read events for a source in deterministic order."""


@dataclass(frozen=True)
class NormalizationReject:
    """Auditable record for a raw envelope that could not be normalized."""

    raw: EventEnvelope
    reasons: tuple[str, ...]
    raw_sha256: str
    normalizer_version: str = "normalizer-v1"

    def __post_init__(self) -> None:
        if not self.reasons:
            raise ValueError("normalization reject requires at least one reason")
        require_non_empty(self.raw_sha256, "raw_sha256")
        require_non_empty(self.normalizer_version, "normalizer_version")


class NormalizedEventStore(Protocol):
    """Persist strategy-facing events after normalization.

    Handoff:
    ``normalization.pipeline.NormalizationPipeline``
    writes ``NormalizedEvent`` values here.
    ``replay.engine.NormalizedReplaySource`` reads them back and presents them
    to ``runner.StrategyRunner`` through the ``EventSource`` port.
    """

    def append_normalized(self, event: NormalizedEvent) -> None:
        """Persist one normalized event."""

    def read_normalized(self) -> Iterable[NormalizedEvent]:
        """Read normalized events in deterministic replay order."""


class NormalizationRejectStore(Protocol):
    """Persist raw envelopes that failed normalization."""

    def append_normalization_reject(self, reject: NormalizationReject) -> None:
        """Persist one normalization reject."""

    def read_normalization_rejects(self, source: str = "*") -> Iterable[NormalizationReject]:
        """Read normalization rejects in deterministic order."""
