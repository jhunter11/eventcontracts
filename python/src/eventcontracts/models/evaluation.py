"""Model evaluation: classification + regression metrics with baselines.

The researcher guide (``docs/ml-strategy-researcher-guide.md``) requires every
model to report calibrated, baseline-relative metrics before promotion. This
module is the single, dependency-light (numpy-only) place those numbers come
from, so a backtest, a training CLI, and a notebook all compute them the same
way and the resulting reference reports are comparable across runs.

Classification reports accuracy, log-loss, Brier score, ROC-AUC, and a
reliability/calibration summary (Expected Calibration Error). Regression
reports MSE, MAE, RMSE, and R². Both report the trivial baseline (predict the
base rate / the mean) so a metric is never read in a vacuum.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np

_EPS = 1e-15


@dataclass(frozen=True)
class CalibrationBin:
    lower: float
    upper: float
    count: int
    mean_predicted: float
    mean_observed: float


@dataclass(frozen=True)
class ClassificationMetrics:
    samples: int
    positives: int
    base_rate: float
    accuracy: float
    log_loss: float
    brier_score: float
    roc_auc: float
    expected_calibration_error: float
    baseline_log_loss: float
    baseline_brier_score: float
    calibration_bins: tuple[CalibrationBin, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, float | int]:
        return {
            "samples": self.samples,
            "positives": self.positives,
            "base_rate": self.base_rate,
            "accuracy": self.accuracy,
            "log_loss": self.log_loss,
            "brier_score": self.brier_score,
            "roc_auc": self.roc_auc,
            "expected_calibration_error": self.expected_calibration_error,
            "baseline_log_loss": self.baseline_log_loss,
            "baseline_brier_score": self.baseline_brier_score,
            "log_loss_skill_vs_baseline": _skill(self.log_loss, self.baseline_log_loss),
            "brier_skill_vs_baseline": _skill(self.brier_score, self.baseline_brier_score),
        }


@dataclass(frozen=True)
class RegressionMetrics:
    samples: int
    mse: float
    mae: float
    rmse: float
    r2: float
    baseline_mse: float

    def to_dict(self) -> dict[str, float | int]:
        return {
            "samples": self.samples,
            "mse": self.mse,
            "mae": self.mae,
            "rmse": self.rmse,
            "r2": self.r2,
            "baseline_mse": self.baseline_mse,
            "mse_skill_vs_baseline": _skill(self.mse, self.baseline_mse),
        }


def evaluate_classification(
    y_true: Sequence[int] | np.ndarray,
    y_probability: Sequence[float] | np.ndarray,
    *,
    threshold: float = 0.5,
    calibration_bins: int = 10,
) -> ClassificationMetrics:
    """Binary classification metrics for positive-class probabilities."""

    labels = np.asarray(y_true, dtype=np.float64).ravel()
    probs = np.asarray(y_probability, dtype=np.float64).ravel()
    if labels.shape != probs.shape:
        raise ValueError(f"y_true and y_probability differ in shape: {labels.shape} != {probs.shape}")
    if labels.size == 0:
        raise ValueError("at least one sample is required")
    if not np.isin(labels, (0.0, 1.0)).all():
        raise ValueError("y_true must contain only 0/1 labels")

    n = int(labels.size)
    positives = int(labels.sum())
    base_rate = positives / n
    clipped = np.clip(probs, _EPS, 1.0 - _EPS)
    predictions = (probs >= threshold).astype(np.float64)

    accuracy = float(np.mean(predictions == labels))
    log_loss = float(-np.mean(labels * np.log(clipped) + (1 - labels) * np.log(1 - clipped)))
    brier = float(np.mean((probs - labels) ** 2))
    roc_auc = _roc_auc(labels, probs)
    bins = _calibration_bins(labels, probs, calibration_bins)
    ece = _expected_calibration_error(bins, n)

    base = min(max(base_rate, _EPS), 1.0 - _EPS)
    baseline_log_loss = float(-(base_rate * math.log(base) + (1 - base_rate) * math.log(1 - base)))
    baseline_brier = float(base_rate * (1 - base_rate))

    return ClassificationMetrics(
        samples=n,
        positives=positives,
        base_rate=base_rate,
        accuracy=accuracy,
        log_loss=log_loss,
        brier_score=brier,
        roc_auc=roc_auc,
        expected_calibration_error=ece,
        baseline_log_loss=baseline_log_loss,
        baseline_brier_score=baseline_brier,
        calibration_bins=bins,
    )


def evaluate_regression(
    y_true: Sequence[float] | np.ndarray,
    y_pred: Sequence[float] | np.ndarray,
) -> RegressionMetrics:
    """MSE/MAE/RMSE/R² for continuous targets, vs the mean-predictor baseline."""

    actual = np.asarray(y_true, dtype=np.float64).ravel()
    predicted = np.asarray(y_pred, dtype=np.float64).ravel()
    if actual.shape != predicted.shape:
        raise ValueError(f"y_true and y_pred differ in shape: {actual.shape} != {predicted.shape}")
    if actual.size == 0:
        raise ValueError("at least one sample is required")

    errors = predicted - actual
    mse = float(np.mean(errors**2))
    mae = float(np.mean(np.abs(errors)))
    rmse = float(math.sqrt(mse))
    mean = float(np.mean(actual))
    baseline_mse = float(np.mean((actual - mean) ** 2))
    r2 = 1.0 - (mse / baseline_mse) if baseline_mse > 0 else 0.0

    return RegressionMetrics(
        samples=int(actual.size),
        mse=mse,
        mae=mae,
        rmse=rmse,
        r2=float(r2),
        baseline_mse=baseline_mse,
    )


def _roc_auc(labels: np.ndarray, scores: np.ndarray) -> float:
    """Mann-Whitney rank AUC with tie handling. NaN when one class is absent."""

    n_pos = int(np.sum(labels == 1))
    n_neg = int(np.sum(labels == 0))
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    order = np.argsort(scores, kind="mergesort")
    sorted_scores = scores[order]
    ranks = np.empty(scores.size, dtype=np.float64)
    cursor = 0
    while cursor < sorted_scores.size:
        end = cursor + 1
        while end < sorted_scores.size and sorted_scores[end] == sorted_scores[cursor]:
            end += 1
        average_rank = (cursor + 1 + end) / 2.0
        ranks[order[cursor:end]] = average_rank
        cursor = end
    pos_rank_sum = float(np.sum(ranks[labels == 1]))
    return (pos_rank_sum - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


def _calibration_bins(labels: np.ndarray, probs: np.ndarray, bin_count: int) -> tuple[CalibrationBin, ...]:
    if bin_count <= 0:
        return ()
    edges = np.linspace(0.0, 1.0, bin_count + 1)
    out: list[CalibrationBin] = []
    for i in range(bin_count):
        lower, upper = float(edges[i]), float(edges[i + 1])
        # The final bin is closed on the right so probability 1.0 lands somewhere.
        in_upper = (probs <= upper) if i == bin_count - 1 else (probs < upper)
        mask = (probs >= lower) & in_upper
        count = int(np.sum(mask))
        if count == 0:
            out.append(CalibrationBin(lower, upper, 0, 0.0, 0.0))
            continue
        out.append(
            CalibrationBin(
                lower=lower,
                upper=upper,
                count=count,
                mean_predicted=float(np.mean(probs[mask])),
                mean_observed=float(np.mean(labels[mask])),
            )
        )
    return tuple(out)


def _expected_calibration_error(bins: Sequence[CalibrationBin], total: int) -> float:
    if total == 0:
        return 0.0
    return float(
        sum(
            (bin_.count / total) * abs(bin_.mean_predicted - bin_.mean_observed)
            for bin_ in bins
            if bin_.count > 0
        )
    )


def _skill(metric: float, baseline: float) -> float:
    """Skill score in [−inf, 1]: 1 perfect, 0 == baseline, <0 worse than baseline."""

    if baseline <= 0:
        return 0.0
    return float(1.0 - (metric / baseline))
