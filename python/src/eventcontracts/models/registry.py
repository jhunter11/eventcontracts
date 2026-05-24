"""Local model registries.

Two implementations:

* :class:`InMemoryModelRegistry` — process-local; useful for tests.
* :class:`LocalFileModelRegistry` — backed by a directory on disk where
  each artifact lives at ``<root>/<name>/<version>.json``. The registry
  also tracks promotion pointers per stage in ``<root>/_promotions.json``.

Both share the :class:`ModelRegistry` interface from
:mod:`eventcontracts.models.pipeline`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from eventcontracts.domain.ids import ModelName, ModelVersion
from eventcontracts.models.artifacts import write_artifact
from eventcontracts.models.pipeline import ModelArtifact, ModelRegistry


@dataclass
class InMemoryModelRegistry(ModelRegistry):
    """Hold artifacts in a dict for tests and short-lived processes."""

    _artifacts: dict[tuple[ModelName, ModelVersion], ModelArtifact] = field(
        default_factory=dict
    )
    _promotions: dict[tuple[ModelName, str], ModelVersion] = field(default_factory=dict)

    def register(self, artifact: ModelArtifact) -> None:
        key = (artifact.name, artifact.version)
        existing = self._artifacts.get(key)
        if existing is not None and existing.sha256 != artifact.sha256:
            raise ValueError(
                f"conflicting registration for {artifact.name}:{artifact.version}"
            )
        self._artifacts[key] = artifact

    def get(self, name: ModelName, version: ModelVersion) -> ModelArtifact:
        try:
            return self._artifacts[(name, version)]
        except KeyError as exc:
            raise KeyError(f"model not registered: {name}:{version}") from exc

    def promote(self, artifact: ModelArtifact, stage: str) -> None:
        if not stage:
            raise ValueError("stage must be non-empty")
        self.register(artifact)
        self._promotions[(artifact.name, stage)] = artifact.version

    def current(self, name: ModelName, stage: str) -> ModelArtifact | None:
        version = self._promotions.get((name, stage))
        if version is None:
            return None
        return self._artifacts.get((name, version))

    def list_versions(self, name: ModelName) -> Sequence[ModelVersion]:
        return tuple(sorted({v for n, v in self._artifacts if n == name}))


class LocalFileModelRegistry(ModelRegistry):
    """File-backed registry: ``<root>/<name>/<version>.json`` per artifact."""

    PROMOTIONS_FILE = "_promotions.json"

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def register(self, artifact: ModelArtifact) -> None:
        self._index_artifact(artifact)

    def get(self, name: ModelName, version: ModelVersion) -> ModelArtifact:
        path = self.root / str(name) / f"{version}.json"
        if not path.exists():
            raise KeyError(f"model not registered: {name}:{version}")
        return self._artifact_from_indexed(path)

    def promote(self, artifact: ModelArtifact, stage: str) -> None:
        if not stage:
            raise ValueError("stage must be non-empty")
        self._index_artifact(artifact)
        promotions = self._load_promotions()
        promotions.setdefault(str(artifact.name), {})[stage] = str(artifact.version)
        self._write_promotions(promotions)

    def current(self, name: ModelName, stage: str) -> ModelArtifact | None:
        promotions = self._load_promotions()
        version = promotions.get(str(name), {}).get(stage)
        if version is None:
            return None
        path = self.root / str(name) / f"{version}.json"
        if not path.exists():
            return None
        return self._artifact_from_indexed(path)

    def list_versions(self, name: ModelName) -> Sequence[ModelVersion]:
        dir_ = self.root / str(name)
        if not dir_.exists():
            return ()
        return tuple(
            sorted(ModelVersion(p.stem) for p in dir_.glob("*.json"))
        )

    def write_from_payload(
        self,
        payload: Mapping[str, Any],
        *,
        producer: str = "eventcontracts.models",
    ) -> ModelArtifact:
        """Convenience: persist a training payload and index it in one call."""

        path = self.root / str(payload["model_name"]) / f"{payload['model_version']}.json"
        artifact = write_artifact(payload, path=path, producer=producer)
        self._index_artifact(artifact)
        return artifact

    def _index_artifact(self, artifact: ModelArtifact) -> None:
        path = Path(artifact.uri)
        if not path.exists():
            raise FileNotFoundError(f"artifact file missing: {path}")
        target = self.root / str(artifact.name) / f"{artifact.version}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        # If the file already lives where the registry expects, no copy needed.
        if path.resolve() != target.resolve():
            target.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")

    def _artifact_from_indexed(self, path: Path) -> ModelArtifact:
        from hashlib import sha256

        from eventcontracts.models.artifacts import load_artifact  # noqa: F401 — ensure importable

        raw = path.read_text(encoding="utf-8").strip()
        digest = sha256(raw.encode("utf-8")).hexdigest()
        payload = json.loads(raw)
        from datetime import datetime

        created_at = datetime.fromisoformat(
            str(payload["created_at"]).replace("Z", "+00:00")
        )
        from eventcontracts.audit import audit_stamp_for

        return ModelArtifact(
            name=ModelName(str(payload["model_name"])),
            version=ModelVersion(str(payload["model_version"])),
            uri=str(path.resolve()),
            sha256=digest,
            format=str(payload["kind"]),
            created_at=created_at,
            audit=audit_stamp_for(
                payload,
                object_id=f"model-artifact:{payload['model_name']}:{payload['model_version']}",
                object_kind="model_artifact",
                schema_version="model-artifact-v1",
                produced_at=created_at,
                producer="LocalFileModelRegistry",
            ),
        )

    def _load_promotions(self) -> dict[str, dict[str, str]]:
        path = self.root / self.PROMOTIONS_FILE
        if not path.exists():
            return {}
        from typing import cast

        return cast(
            "dict[str, dict[str, str]]", json.loads(path.read_text(encoding="utf-8"))
        )

    def _write_promotions(self, promotions: Mapping[str, Mapping[str, str]]) -> None:
        path = self.root / self.PROMOTIONS_FILE
        path.write_text(
            json.dumps(promotions, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
