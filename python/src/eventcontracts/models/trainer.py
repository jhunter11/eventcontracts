"""Train a model from a list of `TrainingExample`s.

Splits the examples **chronologically** into train/validate halves (never
shuffles across time), fits the requested model, computes metrics on
both splits, and returns a `TrainingResult` carrying the model, the
metrics, an audited `TrainingRun`, and the canonical artifact payload.

Designed so the same call drives both the in-process trainer (used in
tests and the `eventcontracts train` CLI) and any future hosted trainer
that wants to produce the same artifact shape.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np

from eventcontracts.audit import AuditStamp, audit_stamp_for
from eventcontracts.domain.ids import FeatureSchemaId, ModelName
from eventcontracts.models.dataset import TrainingExample
from eventcontracts.models.models import (
    LinearRegressionModel,
    LogisticRegressionModel,
    ModelKind,
    TrainableModel,
    TrainingMetrics,
)
from eventcontracts.models.pipeline import TrainingRun


@dataclass(frozen=True)
class TrainingResult:
    """Everything one training call produces."""

    model: TrainableModel
    metrics: TrainingMetrics
    run: TrainingRun
    artifact_payload: dict[str, Any]


class ChronologicalSplit:
    """Split a list of `TrainingExample`s into (train, validate) by time."""

    def __init__(self, validate_fraction: float = 0.2) -> None:
        if not 0.0 < validate_fraction < 1.0:
            raise ValueError("validate_fraction must be in (0, 1)")
        self.validate_fraction = validate_fraction

    def split(
        self, examples: Sequence[TrainingExample]
    ) -> tuple[Sequence[TrainingExample], Sequence[TrainingExample]]:
        if len(examples) < 2:
            return tuple(examples), ()
        ordered = sorted(examples, key=lambda ex: ex.features.timestamp)
        cutoff = int(len(ordered) * (1.0 - self.validate_fraction))
        cutoff = max(1, min(cutoff, len(ordered) - 1))
        return ordered[:cutoff], ordered[cutoff:]


def _examples_to_matrix(
    examples: Sequence[TrainingExample], feature_names: Sequence[str]
) -> tuple[np.ndarray, np.ndarray]:
    if not examples:
        return np.empty((0, len(feature_names))), np.empty((0,))
    rows = []
    labels = []
    for ex in examples:
        vector = ex.features.to_dict()
        rows.append([float(vector.get(name, 0.0)) for name in feature_names])
        labels.append(float(ex.label))
    return np.asarray(rows, dtype=np.float64), np.asarray(labels, dtype=np.float64)


@dataclass(frozen=True)
class TrainerConfig:
    model_name: ModelName
    model_version: str
    kind: ModelKind
    feature_schema_id: FeatureSchemaId
    feature_schema_version: str
    horizon_seconds: int
    learning_rate: float = 0.1
    iterations: int = 500
    l2: float = 0.001
    seed: int = 0
    validate_fraction: float = 0.2


class ModelTrainer:
    """Fit a `TrainableModel` from a list of `TrainingExample`s."""

    def __init__(self, config: TrainerConfig) -> None:
        self.config = config

    def train(
        self,
        examples: Sequence[TrainingExample],
        *,
        now: datetime,
        producer: str = "eventcontracts.models.trainer",
    ) -> TrainingResult:
        if not examples:
            raise ValueError("cannot train on zero examples")
        feature_names = tuple(name for name, _ in examples[0].features.values)
        for ex in examples:
            actual = tuple(name for name, _ in ex.features.values)
            if actual != feature_names:
                raise ValueError(
                    f"feature ordering mismatch in dataset: {actual} != {feature_names}"
                )
        split = ChronologicalSplit(self.config.validate_fraction)
        train_examples, validate_examples = split.split(examples)
        x_train, y_train = _examples_to_matrix(train_examples, feature_names)
        x_validate, y_validate = _examples_to_matrix(validate_examples, feature_names)

        if self.config.kind is ModelKind.LINEAR_REGRESSION:
            model: TrainableModel = LinearRegressionModel.fit(
                feature_names, x_train, y_train, l2=self.config.l2
            )
        elif self.config.kind is ModelKind.LOGISTIC_REGRESSION:
            model = LogisticRegressionModel.fit(
                feature_names,
                x_train,
                y_train,
                learning_rate=self.config.learning_rate,
                iterations=self.config.iterations,
                l2=self.config.l2,
                seed=self.config.seed,
            )
        else:  # pragma: no cover — guarded by ModelKind enum
            raise ValueError(f"unsupported model kind: {self.config.kind}")

        metrics = _compute_metrics(
            model, x_train, y_train, x_validate, y_validate, kind=self.config.kind
        )

        artifact_payload = {
            "model_name": str(self.config.model_name),
            "model_version": self.config.model_version,
            "kind": self.config.kind.value,
            "feature_schema_id": str(self.config.feature_schema_id),
            "feature_schema_version": self.config.feature_schema_version,
            "horizon_seconds": int(self.config.horizon_seconds),
            "created_at": now.isoformat(),
            "training_metrics": metrics.to_dict(),
            "training_seed": self.config.seed,
            "training_iterations": self.config.iterations,
            "training_learning_rate": self.config.learning_rate,
            "training_l2": self.config.l2,
            "validate_fraction": self.config.validate_fraction,
            "train_samples": len(train_examples),
            "validate_samples": len(validate_examples),
            **model.to_payload(),
        }

        run_audit: AuditStamp = audit_stamp_for(
            artifact_payload,
            object_id=f"training-run:{self.config.model_name}:{self.config.model_version}",
            object_kind="training_run",
            schema_version="training-run-v1",
            produced_at=now,
            producer=producer,
        )
        run = TrainingRun(
            run_id=f"{self.config.model_name}-{self.config.model_version}-{now.isoformat()}",
            model_name=self.config.model_name,
            started_at=now,
            ended_at=now,
            metrics={k: float(v) for k, v in metrics.to_dict().items() if v is not None},
            audit=run_audit,
        )
        return TrainingResult(
            model=model,
            metrics=metrics,
            run=run,
            artifact_payload=artifact_payload,
        )


def _compute_metrics(
    model: TrainableModel,
    x_train: np.ndarray,
    y_train: np.ndarray,
    x_validate: np.ndarray,
    y_validate: np.ndarray,
    *,
    kind: ModelKind,
) -> TrainingMetrics:
    feature_names = model.feature_names

    def _predict(x: np.ndarray) -> np.ndarray:
        if x.shape[0] == 0:
            return np.empty((0,), dtype=np.float64)
        rows = [
            {name: float(x[row_i, col_i]) for col_i, name in enumerate(feature_names)}
            for row_i in range(x.shape[0])
        ]
        return np.asarray([model.predict(row) for row in rows], dtype=np.float64)

    x_eval = x_validate if x_validate.shape[0] > 0 else x_train
    y_eval = y_validate if y_validate.shape[0] > 0 else y_train
    preds = _predict(x_eval)
    mse = float(np.mean((preds - y_eval) ** 2)) if y_eval.size else 0.0
    mae = float(np.mean(np.abs(preds - y_eval))) if y_eval.size else 0.0

    accuracy: float | None = None
    auc: float | None = None
    log_loss: float | None = None
    if kind is ModelKind.LOGISTIC_REGRESSION and y_eval.size:
        cls = (preds >= 0.5).astype(np.float64)
        accuracy = float(np.mean(cls == y_eval))
        clipped = np.clip(preds, 1e-7, 1.0 - 1e-7)
        log_loss = float(
            -np.mean(y_eval * np.log(clipped) + (1 - y_eval) * np.log(1 - clipped))
        )
        auc = _safe_auc(y_eval, preds)

    return TrainingMetrics(
        mse=mse,
        mae=mae,
        accuracy=accuracy,
        auc=auc,
        log_loss=log_loss,
        train_samples=int(x_train.shape[0]),
        validate_samples=int(x_validate.shape[0]),
        feature_count=int(len(feature_names)),
    )


def _safe_auc(y: np.ndarray, scores: np.ndarray) -> float | None:
    """Mann-Whitney AUC, returns None when only one class is present."""

    pos = scores[y > 0.5]
    neg = scores[y <= 0.5]
    if pos.size == 0 or neg.size == 0:
        return None
    # Rank-based AUC.
    combined = np.concatenate([pos, neg])
    order = np.argsort(combined, kind="mergesort")
    ranks = np.empty_like(order, dtype=np.float64)
    ranks[order] = np.arange(1, combined.size + 1)
    pos_rank_sum = float(np.sum(ranks[: pos.size]))
    auc = (pos_rank_sum - pos.size * (pos.size + 1) / 2) / (pos.size * neg.size)
    if not math.isfinite(auc):
        return None
    return float(auc)
