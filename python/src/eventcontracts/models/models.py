"""Concrete trainable models.

Two shapes are provided, both pure-numpy so they have no scikit-learn /
torch dependency:

* :class:`LinearRegressionModel` — closed-form OLS on a feature design
  matrix with bias. Predicts an unbounded continuous value.
* :class:`LogisticRegressionModel` — gradient-descent logistic regression
  with L2 regularization. Predicts a probability in ``[0, 1]``.

Both models share a stable serialization shape (``to_payload`` /
``from_payload``) so the artifact writer can persist them as JSON. The
runner code in :mod:`eventcontracts.models.runner` doesn't know which
model kind it loaded — every model exposes ``predict(features)``.

Determinism: training is seedable; the same inputs in the same order
must produce the same coefficients.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import numpy as np


class ModelKind(str, Enum):
    LINEAR_REGRESSION = "linear_regression"
    LOGISTIC_REGRESSION = "logistic_regression"


@dataclass(frozen=True)
class TrainingMetrics:
    """Summary statistics emitted at the end of every training run."""

    mse: float
    mae: float
    accuracy: float | None = None
    auc: float | None = None
    log_loss: float | None = None
    train_samples: int = 0
    validate_samples: int = 0
    feature_count: int = 0
    extras: Mapping[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, float | int | None]:
        out: dict[str, float | int | None] = {
            "mse": self.mse,
            "mae": self.mae,
            "accuracy": self.accuracy,
            "auc": self.auc,
            "log_loss": self.log_loss,
            "train_samples": self.train_samples,
            "validate_samples": self.validate_samples,
            "feature_count": self.feature_count,
        }
        for key, value in self.extras.items():
            out[f"extra_{key}"] = value
        return out


@dataclass
class LinearRegressionModel:
    """Closed-form OLS linear regression with an explicit bias term."""

    feature_names: tuple[str, ...]
    coefficients: tuple[float, ...]
    intercept: float
    kind: ModelKind = ModelKind.LINEAR_REGRESSION

    def predict(self, features: Mapping[str, float]) -> float:
        x = np.array(
            [float(features.get(name, 0.0)) for name in self.feature_names],
            dtype=np.float64,
        )
        return float(self.intercept + x @ np.array(self.coefficients, dtype=np.float64))

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "feature_names": list(self.feature_names),
            "coefficients": list(self.coefficients),
            "intercept": self.intercept,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> LinearRegressionModel:
        return cls(
            feature_names=tuple(str(n) for n in payload["feature_names"]),
            coefficients=tuple(float(c) for c in payload["coefficients"]),
            intercept=float(payload["intercept"]),
        )

    @classmethod
    def fit(
        cls,
        feature_names: Sequence[str],
        x: np.ndarray,
        y: np.ndarray,
        *,
        l2: float = 0.0,
    ) -> LinearRegressionModel:
        """Closed-form ridge regression. ``l2 = 0`` reduces to plain OLS."""

        if x.shape[0] == 0:
            raise ValueError("cannot fit on zero samples")
        n_features = x.shape[1]
        design = np.hstack([np.ones((x.shape[0], 1), dtype=np.float64), x])
        reg = l2 * np.eye(n_features + 1, dtype=np.float64)
        reg[0, 0] = 0.0  # don't regularize the bias term
        gram = design.T @ design + reg
        rhs = design.T @ y
        try:
            beta = np.linalg.solve(gram, rhs)
        except np.linalg.LinAlgError:
            # Fall back to pseudo-inverse when the gram matrix is singular.
            beta = np.linalg.pinv(gram) @ rhs
        return cls(
            feature_names=tuple(feature_names),
            intercept=float(beta[0]),
            coefficients=tuple(float(v) for v in beta[1:]),
        )


@dataclass
class LogisticRegressionModel:
    """Logistic regression for binary classification (output in [0, 1])."""

    feature_names: tuple[str, ...]
    coefficients: tuple[float, ...]
    intercept: float
    kind: ModelKind = ModelKind.LOGISTIC_REGRESSION

    def predict(self, features: Mapping[str, float]) -> float:
        x = np.array(
            [float(features.get(name, 0.0)) for name in self.feature_names],
            dtype=np.float64,
        )
        z = float(self.intercept + x @ np.array(self.coefficients, dtype=np.float64))
        return _sigmoid_scalar(z)

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "feature_names": list(self.feature_names),
            "coefficients": list(self.coefficients),
            "intercept": self.intercept,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> LogisticRegressionModel:
        return cls(
            feature_names=tuple(str(n) for n in payload["feature_names"]),
            coefficients=tuple(float(c) for c in payload["coefficients"]),
            intercept=float(payload["intercept"]),
        )

    @classmethod
    def fit(
        cls,
        feature_names: Sequence[str],
        x: np.ndarray,
        y: np.ndarray,
        *,
        learning_rate: float = 0.1,
        iterations: int = 500,
        l2: float = 0.001,
        seed: int = 0,
    ) -> LogisticRegressionModel:
        """Gradient-descent logistic regression with L2 regularization."""

        if x.shape[0] == 0:
            raise ValueError("cannot fit on zero samples")
        if not set(np.unique(y)).issubset({0.0, 1.0}):
            raise ValueError(
                "LogisticRegressionModel requires binary labels in {0.0, 1.0}"
            )
        rng = np.random.default_rng(seed)
        n_features = x.shape[1]
        weights = np.asarray(rng.standard_normal(n_features), dtype=np.float64) * 0.01
        bias: float = 0.0
        n = float(x.shape[0])
        for _ in range(iterations):
            z = bias + x @ weights
            p = _sigmoid_array(z)
            error = p - y
            grad_w = (x.T @ error) / n + l2 * weights
            grad_b = float(np.mean(error))
            weights = weights - learning_rate * grad_w
            bias = bias - learning_rate * grad_b
        return cls(
            feature_names=tuple(feature_names),
            intercept=float(bias),
            coefficients=tuple(float(v) for v in np.asarray(weights, dtype=np.float64)),
        )


def _sigmoid_array(z: np.ndarray) -> np.ndarray:
    """Vectorized numerically stable sigmoid."""

    out = np.empty_like(z, dtype=np.float64)
    positive = z >= 0
    out[positive] = 1.0 / (1.0 + np.exp(-z[positive]))
    ez = np.exp(z[~positive])
    out[~positive] = ez / (1.0 + ez)
    return out


def _sigmoid_scalar(z: float) -> float:
    """Scalar numerically stable sigmoid."""

    if z >= 0:
        return 1.0 / (1.0 + float(np.exp(-z)))
    ez = float(np.exp(z))
    return ez / (1.0 + ez)


TrainableModel = LinearRegressionModel | LogisticRegressionModel


def load_model(payload: Mapping[str, Any]) -> TrainableModel:
    """Construct the right concrete model from a serialized payload."""

    kind = ModelKind(payload["kind"])
    if kind is ModelKind.LINEAR_REGRESSION:
        return LinearRegressionModel.from_payload(payload)
    if kind is ModelKind.LOGISTIC_REGRESSION:
        return LogisticRegressionModel.from_payload(payload)
    raise ValueError(f"unknown model kind: {kind}")
