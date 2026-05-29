"""Offline and online feature-building scaffolds."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from eventcontracts.audit import AuditStamp
from eventcontracts.domain.events import NormalizedEvent
from eventcontracts.domain.features import FeatureVector
from eventcontracts.domain.ids import FeatureSchemaId
from eventcontracts.domain.metadata import FrozenMap, freeze_mapping
from eventcontracts.domain.models import InstrumentId
from eventcontracts.domain.validation import require_aware_datetime, require_non_empty


class FeatureDType(str, Enum):
    FLOAT32 = "float32"
    FLOAT64 = "float64"
    INT32 = "int32"
    INT64 = "int64"
    BOOL = "bool"
    STRING = "string"


@dataclass(frozen=True)
class FeatureDefinition:
    name: str
    dtype: FeatureDType
    nullable: bool = False
    default: str | int | float | bool | None = None
    description: str = ""

    def __post_init__(self) -> None:
        require_non_empty(self.name, "feature name")


@dataclass(frozen=True)
class FeatureSchema:
    schema_id: FeatureSchemaId
    schema_version: str
    features: tuple[FeatureDefinition, ...]
    target_name: str | None = None
    target_horizon_seconds: int | None = None

    def __post_init__(self) -> None:
        require_non_empty(str(self.schema_id), "schema_id")
        require_non_empty(self.schema_version, "schema_version")
        if not self.features:
            raise ValueError("feature schema must contain at least one feature")
        object.__setattr__(self, "features", tuple(self.features))


@dataclass(frozen=True)
class OnlineFeatureState:
    instrument_id: InstrumentId | None
    as_of: datetime
    last_event: NormalizedEvent | None = None
    vector: FeatureVector | None = None
    builder_state: Mapping[str, Any] = field(default_factory=FrozenMap)
    audit: AuditStamp | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        require_aware_datetime(self.as_of, "as_of")
        object.__setattr__(self, "builder_state", freeze_mapping(self.builder_state))
        object.__setattr__(self, "notes", tuple(self.notes))


class FeatureBuilder:
    """Build feature vectors from normalized event streams."""

    def schema(self) -> FeatureSchema:
        raise NotImplementedError

    def warmup(self, events: Sequence[NormalizedEvent]) -> OnlineFeatureState:
        raise NotImplementedError

    def update(
        self, state: OnlineFeatureState, event: NormalizedEvent
    ) -> OnlineFeatureState:
        raise NotImplementedError

    def build_offline(self, events: Sequence[NormalizedEvent]) -> Sequence[FeatureVector]:
        raise NotImplementedError


class FeatureStore:
    """Point-in-time feature storage boundary."""

    def write_vector(self, vector: FeatureVector, audit: AuditStamp) -> None:
        raise NotImplementedError

    def latest(
        self, schema_id: FeatureSchemaId, instrument_id: InstrumentId | None
    ) -> FeatureVector | None:
        raise NotImplementedError

    def history(
        self,
        schema_id: FeatureSchemaId,
        instrument_id: InstrumentId | None,
        start: datetime,
        end: datetime,
    ) -> Sequence[FeatureVector]:
        raise NotImplementedError


class InMemoryFeatureStore(FeatureStore):
    """Point-in-time feature store for tests, notebooks, and paper runs."""

    def __init__(self) -> None:
        self._vectors: list[tuple[FeatureVector, AuditStamp]] = []

    def write_vector(self, vector: FeatureVector, audit: AuditStamp) -> None:
        self._vectors.append((vector, audit))
        self._vectors.sort(
            key=lambda item: (
                str(item[0].schema_id),
                _instrument_key(item[0].instrument_id),
                item[0].timestamp,
            )
        )

    def latest(
        self, schema_id: FeatureSchemaId, instrument_id: InstrumentId | None
    ) -> FeatureVector | None:
        matches = [
            vector
            for vector, _audit in self._vectors
            if vector.schema_id == schema_id and vector.instrument_id == instrument_id
        ]
        return matches[-1] if matches else None

    def history(
        self,
        schema_id: FeatureSchemaId,
        instrument_id: InstrumentId | None,
        start: datetime,
        end: datetime,
    ) -> Sequence[FeatureVector]:
        require_aware_datetime(start, "start")
        require_aware_datetime(end, "end")
        if end < start:
            raise ValueError("end must be on or after start")
        return tuple(
            vector
            for vector, _audit in self._vectors
            if vector.schema_id == schema_id
            and vector.instrument_id == instrument_id
            and start <= vector.timestamp <= end
        )

    def audit_for(self, vector: FeatureVector) -> AuditStamp | None:
        for candidate, audit in self._vectors:
            if candidate == vector:
                return audit
        return None


def _instrument_key(instrument_id: InstrumentId | None) -> str:
    if instrument_id is None:
        return ""
    return (
        f"{instrument_id.venue.value}:"
        f"{instrument_id.market_id}:"
        f"{instrument_id.outcome_id or ''}"
    )
