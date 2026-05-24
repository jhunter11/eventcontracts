"""In-process model runner.

Loads :class:`ModelArtifact`s into memory and exposes ``predict`` against
a :class:`FeatureVector`. The runner is intentionally narrow — it knows
how to look a model up by name, and how to call ``predict`` on it.
Strategy code never touches the runner directly; it goes through
``StrategyContext.predict()``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from eventcontracts.domain.features import FeatureVector, Prediction
from eventcontracts.domain.ids import ModelName, ModelVersion
from eventcontracts.models.artifacts import load_artifact
from eventcontracts.models.models import TrainableModel
from eventcontracts.models.pipeline import ModelArtifact, ModelRunner


@dataclass
class _LoadedEntry:
    artifact: ModelArtifact
    model: TrainableModel
    payload: Mapping[str, Any]


@dataclass
class InProcessModelRunner(ModelRunner):
    """Holds loaded models in a dict, predicts in-process."""

    _models: dict[ModelName, _LoadedEntry] = field(default_factory=dict)

    def load(self, artifact: ModelArtifact) -> None:
        model, payload = load_artifact(artifact)
        self._models[artifact.name] = _LoadedEntry(
            artifact=artifact, model=model, payload=payload
        )

    def load_inline(
        self,
        artifact: ModelArtifact,
        model: TrainableModel,
        payload: Mapping[str, Any],
    ) -> None:
        """Register an already-instantiated model without touching disk.

        Used by the trainer when it wants to run an immediate sanity-check
        prediction against a freshly-fit model without round-tripping
        through the filesystem.
        """

        self._models[artifact.name] = _LoadedEntry(
            artifact=artifact, model=model, payload=payload
        )

    def known(self) -> tuple[ModelName, ...]:
        return tuple(self._models.keys())

    def predict(self, model_name: ModelName, features: FeatureVector) -> Prediction:
        try:
            entry = self._models[model_name]
        except KeyError as exc:
            raise KeyError(
                f"model not loaded: {model_name} (loaded: {sorted(self._models)})"
            ) from exc
        value = entry.model.predict(features.to_dict())
        horizon = int(entry.payload.get("horizon_seconds", 0))
        return Prediction(
            model_name=entry.artifact.name,
            model_version=ModelVersion(entry.artifact.version),
            instrument_id=features.instrument_id,
            timestamp=features.timestamp,
            horizon_seconds=horizon,
            value=float(value),
            confidence=None,
        )
