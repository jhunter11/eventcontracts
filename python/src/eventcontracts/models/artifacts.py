"""Serialize and verify model artifacts on disk.

Format: a canonical JSON payload with the model's coefficients plus the
training metadata produced by :class:`ModelTrainer`. The artifact file's
SHA-256 is recorded in the returned :class:`ModelArtifact` so the
runner can verify a loaded artifact has not drifted from what was
trained.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from eventcontracts.audit import audit_stamp_for
from eventcontracts.domain.ids import ModelName, ModelVersion
from eventcontracts.models.models import TrainableModel, load_model
from eventcontracts.models.pipeline import ModelArtifact


def canonical_dumps(payload: Mapping[str, Any]) -> str:
    """Stable JSON: sorted keys, no extra whitespace, no ASCII escaping."""

    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_of_payload(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_dumps(payload).encode("utf-8")).hexdigest()


def write_artifact(
    payload: Mapping[str, Any], *, path: Path, producer: str = "eventcontracts.models"
) -> ModelArtifact:
    """Write `payload` to disk as canonical JSON and return the artifact."""

    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = canonical_dumps(payload)
    path.write_text(rendered + "\n", encoding="utf-8")
    digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    name = ModelName(str(payload["model_name"]))
    version = ModelVersion(str(payload["model_version"]))
    created_at = datetime.fromisoformat(str(payload["created_at"]).replace("Z", "+00:00"))
    return ModelArtifact(
        name=name,
        version=version,
        uri=str(path.resolve()),
        sha256=digest,
        format=str(payload["kind"]),
        created_at=created_at,
        audit=audit_stamp_for(
            payload,
            object_id=f"model-artifact:{name}:{version}",
            object_kind="model_artifact",
            schema_version="model-artifact-v1",
            produced_at=created_at,
            producer=producer,
        ),
    )


def load_artifact(artifact: ModelArtifact) -> tuple[TrainableModel, dict[str, Any]]:
    """Load a model from disk, verifying its sha256 against the artifact."""

    raw = Path(artifact.uri).read_text(encoding="utf-8").strip()
    actual = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    if actual != artifact.sha256:
        raise ValueError(
            f"sha256 mismatch for {artifact.uri}: expected {artifact.sha256}, got {actual}"
        )
    payload = json.loads(raw)
    return load_model(payload), payload
