"""Tests for the generic classification/regression evaluation module."""

from __future__ import annotations

import math

import pytest

from eventcontracts.models.evaluation import evaluate_classification, evaluate_regression


def test_perfect_classifier_metrics() -> None:
    metrics = evaluate_classification([0, 1, 1, 0], [0.1, 0.8, 0.6, 0.4])
    assert metrics.accuracy == 1.0
    assert metrics.roc_auc == 1.0
    assert metrics.log_loss < 0.4
    assert metrics.brier_score < 0.1
    assert metrics.samples == 4
    assert metrics.positives == 2
    assert metrics.base_rate == 0.5


def test_classification_baseline_skill_is_positive_for_a_good_model() -> None:
    # A confident, correct model should beat the base-rate predictor.
    metrics = evaluate_classification([0, 0, 1, 1, 1], [0.05, 0.1, 0.9, 0.95, 0.8])
    payload = metrics.to_dict()
    assert payload["log_loss_skill_vs_baseline"] > 0
    assert payload["brier_skill_vs_baseline"] > 0


def test_calibration_error_high_for_overconfident_model() -> None:
    # Predict 0.99 for everything but only half are positive → badly calibrated.
    metrics = evaluate_classification([1, 0, 1, 0], [0.99, 0.99, 0.99, 0.99], calibration_bins=10)
    assert metrics.expected_calibration_error > 0.4


def test_roc_auc_is_nan_for_single_class() -> None:
    metrics = evaluate_classification([1, 1, 1], [0.2, 0.6, 0.9])
    assert math.isnan(metrics.roc_auc)


def test_classification_rejects_non_binary_labels() -> None:
    with pytest.raises(ValueError):
        evaluate_classification([0, 2, 1], [0.1, 0.5, 0.9])


def test_regression_metrics_and_r2() -> None:
    y_true = [1.0, 2.0, 3.0, 4.0]
    metrics = evaluate_regression(y_true, y_true)
    assert metrics.mse == 0.0
    assert metrics.r2 == 1.0
    assert metrics.rmse == 0.0

    worse = evaluate_regression(y_true, [2.5, 2.5, 2.5, 2.5])
    assert worse.mse > 0
    assert worse.r2 <= 0.0  # mean-predictor baseline is the 0-skill reference
