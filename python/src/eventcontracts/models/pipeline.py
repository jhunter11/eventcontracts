"""Model lifecycle scaffolds.

Models train and iterate in Python first. Promotion requires immutable
artifacts, parity cases, checksums, and an explicit runtime format so the same
feature vectors and decisions can be checked from Rust without reinterpretation.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from eventcontracts.audit import AuditStamp
from eventcontracts.domain.features import FeatureVector, Prediction
from eventcontracts.domain.ids import ModelName, ModelVersion
from eventcontracts.domain.validation import require_aware_datetime, require_non_empty


@dataclass(frozen=True)
class TrainingDataset:
    dataset_id: str
    feature_vectors: tuple[FeatureVector, ...]
    labels: tuple[float, ...]
    created_at: datetime
    audit: AuditStamp

    def __post_init__(self) -> None:
        require_non_empty(self.dataset_id, "dataset_id")
        require_aware_datetime(self.created_at, "created_at")
        if len(self.feature_vectors) != len(self.labels):
            raise ValueError("feature_vectors and labels must have the same length")


@dataclass(frozen=True)
class TrainingRun:
    run_id: str
    model_name: ModelName
    started_at: datetime
    ended_at: datetime | None = None
    metrics: dict[str, float] = field(default_factory=dict)
    audit: AuditStamp | None = None

    def __post_init__(self) -> None:
        require_non_empty(self.run_id, "run_id")
        require_non_empty(str(self.model_name), "model_name")
        require_aware_datetime(self.started_at, "started_at")
        if self.ended_at is not None:
            require_aware_datetime(self.ended_at, "ended_at")


@dataclass(frozen=True)
class ModelArtifact:
    name: ModelName
    version: ModelVersion
    uri: str
    sha256: str
    format: str
    created_at: datetime
    audit: AuditStamp

    def __post_init__(self) -> None:
        require_non_empty(str(self.name), "model_name")
        require_non_empty(str(self.version), "model_version")
        require_non_empty(self.uri, "uri")
        require_non_empty(self.sha256, "sha256")
        require_non_empty(self.format, "format")
        require_aware_datetime(self.created_at, "created_at")


class ModelExporter:
    """Export trained models to non-JSON runtime artifacts (e.g. ONNX).

    Kept as a scaffold while the JSON artifact path in
    :mod:`eventcontracts.models.artifacts` handles the common case. An
    ONNX exporter is the next promotion step once Rust inference exists.
    """

    def export_onnx(self, run: TrainingRun) -> ModelArtifact:
        raise NotImplementedError

    def write_parity_cases(self, run: TrainingRun, vectors: Sequence[FeatureVector]) -> str:
        raise NotImplementedError


class ModelRunner:
    """Runtime inference boundary used by strategy contexts.

    Concrete implementation lives in :class:`eventcontracts.models.runner.InProcessModelRunner`.
    """

    def load(self, artifact: ModelArtifact) -> None:
        raise NotImplementedError

    def predict(self, model_name: ModelName, features: FeatureVector) -> Prediction:
        raise NotImplementedError


class ModelRegistry:
    """Track immutable model artifacts and promotion state.

    Concrete implementations live in
    :class:`eventcontracts.models.registry.InMemoryModelRegistry` and
    :class:`eventcontracts.models.registry.LocalFileModelRegistry`.
    """

    def register(self, artifact: ModelArtifact) -> None:
        raise NotImplementedError

    def get(self, name: ModelName, version: ModelVersion) -> ModelArtifact:
        raise NotImplementedError

    def promote(self, artifact: ModelArtifact, stage: str) -> None:
        raise NotImplementedError

    def current(self, name: ModelName, stage: str) -> ModelArtifact | None:
        raise NotImplementedError

    def list_versions(self, name: ModelName) -> Sequence[ModelVersion]:
        raise NotImplementedError
