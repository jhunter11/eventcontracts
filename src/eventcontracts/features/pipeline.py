"""Offline and online feature-building scaffolds."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from eventcontracts.domain.events import NormalizedEvent
from eventcontracts.domain.features import FeatureVector
from eventcontracts.domain.ids import FeatureSchemaId
from eventcontracts.domain.models import InstrumentId
from eventcontracts.domain.validation import require_aware_datetime, require_non_empty


class FeatureDType(str, Enum):
    FLOAT32 = "float32"
    FLOAT64 = "float64"
    INT64 = "int64"
    BOOL = "bool"


@dataclass(frozen=True)
class FeatureDefinition:
    name: str
    dtype: FeatureDType
    nullable: bool = False
    default: float | None = None
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


@dataclass(frozen=True)
class OnlineFeatureState:
    instrument_id: InstrumentId | None
    as_of: datetime
    last_event: NormalizedEvent | None = None
    vector: FeatureVector | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        require_aware_datetime(self.as_of, "as_of")


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

    def write_vector(self, vector: FeatureVector) -> None:
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
