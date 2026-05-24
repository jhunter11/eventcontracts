"""Artifact bundle scaffolds."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from eventcontracts.audit import AuditStamp
from eventcontracts.domain.spec import StrategySpec
from eventcontracts.domain.validation import require_aware_datetime, require_non_empty
from eventcontracts.features.pipeline import FeatureSchema
from eventcontracts.models.pipeline import ModelArtifact


@dataclass(frozen=True)
class BundleFile:
    path: str
    sha256: str
    size_bytes: int

    def __post_init__(self) -> None:
        require_non_empty(self.path, "path")
        require_non_empty(self.sha256, "sha256")
        if self.size_bytes < 0:
            raise ValueError("size_bytes must be non-negative")


@dataclass(frozen=True)
class ArtifactBundle:
    bundle_id: str
    strategy: StrategySpec
    feature_schema: FeatureSchema | None
    model: ModelArtifact | None
    files: tuple[BundleFile, ...]
    created_at: datetime
    manifest_path: str
    audit: AuditStamp

    def __post_init__(self) -> None:
        require_non_empty(self.bundle_id, "bundle_id")
        require_non_empty(self.manifest_path, "manifest_path")
        require_aware_datetime(self.created_at, "created_at")
        object.__setattr__(self, "files", tuple(self.files))


class ArtifactBundleWriter:
    """Write immutable strategy/model bundles."""

    def write_manifest(self, bundle: ArtifactBundle) -> BundleFile:
        raise NotImplementedError

    def write_strategy_spec(self, spec: StrategySpec) -> BundleFile:
        raise NotImplementedError

    def write_feature_schema(self, schema: FeatureSchema) -> BundleFile:
        raise NotImplementedError

    def write_model(self, artifact: ModelArtifact) -> BundleFile:
        raise NotImplementedError


class ArtifactBundleLoader:
    """Load immutable bundles for replay, paper, or live sleeves."""

    def load(self, uri: str) -> ArtifactBundle:
        raise NotImplementedError

    def list_versions(self, strategy_name: str) -> Sequence[str]:
        raise NotImplementedError


class ArtifactBundleValidator:
    """Validate bundle integrity and runtime compatibility."""

    def validate_checksums(self, bundle: ArtifactBundle) -> None:
        raise NotImplementedError

    def validate_strategy_registered(self, bundle: ArtifactBundle) -> None:
        raise NotImplementedError

    def validate_parity_cases(self, bundle: ArtifactBundle) -> None:
        raise NotImplementedError


class PromotionRegistry:
    """Mutable promotion pointer outside immutable bundle contents."""

    def promote(self, bundle: ArtifactBundle, stage: str) -> None:
        raise NotImplementedError

    def current(self, strategy_name: str, stage: str) -> ArtifactBundle | None:
        raise NotImplementedError
