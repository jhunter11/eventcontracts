"""Feature, signal, and prediction primitives.

A ``FeatureVector`` is the contract between the offline feature pipeline
(Python) and the online feature builder (eventually Rust). Both sides must
read the same ``feature_schema.json`` and emit vectors with identical
ordering and dtypes — that is what makes Python-trained models behave the
same way live.

Stored as an ordered tuple of (name, value) pairs for hashability and stable
serialization.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from math import isfinite
from typing import Any

from eventcontracts.domain.ids import FeatureSchemaId, ModelName, ModelVersion
from eventcontracts.domain.metadata import FrozenMap, freeze_mapping
from eventcontracts.domain.models import InstrumentId
from eventcontracts.domain.validation import (
    require_aware_datetime,
    require_non_empty,
    require_non_negative_int,
)


@dataclass(frozen=True)
class FeatureVector:
    schema_id: FeatureSchemaId
    schema_version: str
    instrument_id: InstrumentId | None
    timestamp: datetime
    values: tuple[tuple[str, float], ...]

    def __post_init__(self) -> None:
        require_non_empty(str(self.schema_id), "schema_id")
        require_non_empty(self.schema_version, "schema_version")
        require_aware_datetime(self.timestamp, "timestamp")
        seen: set[str] = set()
        for name, value in self.values:
            require_non_empty(name, "feature name")
            if name in seen:
                raise ValueError(f"duplicate feature name: {name}")
            if not isfinite(value):
                raise ValueError(f"feature value must be finite: {name}={value}")
            if _is_probability_semantic_feature(name) and not 0.0 <= value <= 1.0:
                raise ValueError(f"probability feature must be between 0 and 1: {name}={value}")
            seen.add(name)

    def to_dict(self) -> dict[str, float]:
        return dict(self.values)

    def get(self, name: str, default: float | None = None) -> float | None:
        for n, v in self.values:
            if n == name:
                return v
        return default


@dataclass(frozen=True)
class Signal:
    name: str
    value: float
    timestamp: datetime
    confidence: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=FrozenMap)

    def __post_init__(self) -> None:
        require_non_empty(self.name, "name")
        if not isfinite(self.value):
            raise ValueError(f"signal value must be finite: {self.value}")
        require_aware_datetime(self.timestamp, "timestamp")
        if self.confidence is not None and not 0 <= self.confidence <= 1:
            raise ValueError(f"confidence must be between 0 and 1: {self.confidence}")
        object.__setattr__(self, "metadata", freeze_mapping(self.metadata))


@dataclass(frozen=True)
class Prediction:
    model_name: ModelName
    model_version: ModelVersion
    instrument_id: InstrumentId | None
    timestamp: datetime
    horizon_seconds: int
    value: float
    confidence: float | None = None
    extras: Mapping[str, float] = field(default_factory=FrozenMap)

    def __post_init__(self) -> None:
        require_non_empty(str(self.model_name), "model_name")
        require_non_empty(str(self.model_version), "model_version")
        require_aware_datetime(self.timestamp, "timestamp")
        require_non_negative_int(self.horizon_seconds, "horizon_seconds")
        if not isfinite(self.value):
            raise ValueError(f"prediction value must be finite: {self.value}")
        if self.confidence is not None and not 0 <= self.confidence <= 1:
            raise ValueError(f"confidence must be between 0 and 1: {self.confidence}")
        for key, extra in self.extras.items():
            require_non_empty(key, "extra key")
            if not isfinite(extra):
                raise ValueError(f"extra value must be finite: {key}={extra}")
        object.__setattr__(self, "extras", freeze_mapping(self.extras))


def _is_probability_semantic_feature(name: str) -> bool:
    """Best-effort guard for feature names that explicitly carry probabilities.

    Broad model features like `implied_prob_diff` and `win_pct_diff` can be
    signed deltas, so only direct probability names are clamped.
    """

    lowered = name.lower()
    if any(token in lowered for token in ("diff", "delta", "edge", "overround")):
        return False
    return lowered in {"prob", "probability", "confidence"} or lowered.endswith(
        ("_prob", "_probability", "_win_pct")
    )
