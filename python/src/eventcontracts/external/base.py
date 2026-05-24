"""External data provider scaffolds."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from eventcontracts.audit import AuditStamp
from eventcontracts.domain.metadata import FrozenMap, freeze_mapping
from eventcontracts.domain.validation import require_aware_datetime, require_non_empty
from eventcontracts.storage.interfaces import EventEnvelope


@dataclass(frozen=True)
class ExternalObservation:
    source: str
    observed_at: datetime
    received_at: datetime
    payload: Mapping[str, Any]
    schema_version: str
    audit: AuditStamp
    metadata: Mapping[str, Any] = field(default_factory=FrozenMap)

    def __post_init__(self) -> None:
        require_non_empty(self.source, "source")
        require_aware_datetime(self.observed_at, "observed_at")
        require_aware_datetime(self.received_at, "received_at")
        require_non_empty(self.schema_version, "schema_version")
        object.__setattr__(self, "payload", freeze_mapping(self.payload))
        object.__setattr__(self, "metadata", freeze_mapping(self.metadata))


class ExternalDataClient:
    """Provider client that yields point-in-time external observations."""

    def stream(self) -> Iterator[ExternalObservation]:
        raise NotImplementedError

    def backfill(self, start: datetime, end: datetime) -> Iterator[ExternalObservation]:
        raise NotImplementedError


class ExternalEnvelopeMapper:
    """Map external observations into raw envelopes for storage."""

    def to_envelope(self, observation: ExternalObservation) -> EventEnvelope:
        raise NotImplementedError
