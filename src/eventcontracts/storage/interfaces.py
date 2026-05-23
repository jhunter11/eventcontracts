"""Storage boundaries for raw and normalized events."""

from __future__ import annotations

from collections.abc import Iterable
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from eventcontracts.domain.metadata import FrozenMap, freeze_mapping
from eventcontracts.domain.models import Venue
from eventcontracts.domain.validation import (
    require_aware_datetime,
    require_non_empty,
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
        require_optional_aware_datetime(self.exchange_ts, "exchange_ts")
        require_non_empty(self.schema_version, "schema_version")
        object.__setattr__(self, "payload", freeze_mapping(self.payload))
        object.__setattr__(self, "metadata", freeze_mapping(self.metadata))


class EventStore(Protocol):
    def append(self, event: EventEnvelope) -> None:
        """Persist one raw or normalized event."""

    def read(self, source: str) -> Iterable[EventEnvelope]:
        """Read events for a source in deterministic order."""
