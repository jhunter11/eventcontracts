# ML Model Pipeline (ONNX + HuggingFace)

This is the model-family-agnostic training/export/serving pipeline that any
strategy can use to promote a trained model from Python research to the Rust
runtime. It generalizes what the tennis XGBoost pipeline does so that the same
contract covers scikit-learn, XGBoost, LightGBM, and HuggingFace models.

## Why this exists

Before this, only the tennis XGBoost path could produce a portable `model.onnx`
the Rust runner could load; everything else stopped at the in-process JSON
linear/logistic models. The export, parity, and metric code was tennis-specific
and hard-coded to 20 features. This pipeline lifts that into reusable modules
so a new model is a config + a feature table, not new export/parity/Rust code.

## The contract

A promoted model is a single ONNX graph with:

- **Input** `features`: a `float32` tensor of shape `[N, len(feature_names)]`.
- **Output**: one named tensor, read by an `output_select` rule shared with the
  Rust loader — `scalar:<idx>` (one column, e.g. positive-class probability) or
  `all` (the whole row, for regression / multi-output).
- **Metadata** (`eventcontracts.*` keys embedded in the graph): feature schema
  id/version, ordered feature names, model family, task, input/output tensor
  names, and input width. The promoted `feature_schema.json` stays the source
  of truth for feature *order*; the embedded copy lets the runtime fail fast.

Feature *values* are produced upstream in the schema's order (by the offline
trainer's table, or the Rust hot-path feature builder). The runtime validates
the schema's feature *count* against the model's input width.

## Python modules

| Module | Responsibility |
| --- | --- |
| `eventcontracts.models.onnx_export` | `export_model_onnx` (sklearn/xgboost/lightgbm → ONNX), `predict_onnx`, `verify_export_parity`, `read_metadata`. |
| `eventcontracts.models.evaluation` | `evaluate_classification` (accuracy, log-loss, Brier, ROC-AUC, calibration/ECE, baseline skill), `evaluate_regression` (MSE/MAE/RMSE/R²). |
| `eventcontracts.models.parity` | `write_parity_cases` — generic export-parity fixture writer. |
| `eventcontracts.models.hf` | `HfTextClassifier`, `score_texts_to_signals`, `export_hf_text_classifier_onnx`, `resolve_hf_onnx`. |

Heavy converters (`skl2onnx`, `onnxmltools`, `onnxruntime`) are imported lazily
and are in `requirements-dev.txt`. HuggingFace extras (`transformers`, `torch`,
`optimum`, `huggingface_hub`) are optional and live in `requirements-hf.txt` —
nothing in the base framework imports them.

## Generic training CLI

`eventcontracts model-train --config <toml>` trains any tabular model family
from a labeled feature table, exports ONNX, verifies export parity, evaluates
on a held-out **temporal** test split, writes export-parity cases, and (when a
strategy spec is given) assembles a promotable artifact bundle plus a JSON
report. See the module docstring in `python/src/eventcontracts/cli/model_train.py`
for the full config schema. Families: `sklearn` (estimator registry),
`xgboost`, `lightgbm`.

## HuggingFace support

HuggingFace covers two contracts:

1. **Text/transformer models** consume tokenized text, not the single float
   tensor. They run in Python as *external-signal producers*: `HfTextClassifier`
   → `score_texts_to_signals(...)` emits `ExternalSignalEvent`-shaped JSONL with
   a `market_id` + probability, which the Rust `ExternalEdgeStrategy` already
   consumes — exactly the path the tennis scorer uses. `export_hf_text_classifier_onnx`
   (via `optimum`) is available for those who want ONNX-runtime text inference.
2. **Pre-exported single-tensor ONNX** on the HuggingFace Hub: `resolve_hf_onnx`
   downloads it and the generic `predict_onnx` / Rust `OnnxScorer` serve it like
   any local export.

## Rust runtime

`eventcontracts-model-runtime` serves promoted bundles model-agnostically:

- `OnnxScorer` / `OnnxScorerPool` — generic ONNX `Session` wrappers implementing
  the `Scorer` trait, with signature validation and `OutputSelect`.
- `OnnxArtifact::load_bundle(dir, output_select)` — loads *any* bundle: reads
  `feature_schema.json` (any feature set/width), resolves the output tensor from
  the graph, validates input width, and serves scores. Implements `Scorer`, so
  it plugs into any strategy generic over `S: Scorer`.
- `TennisOnnxArtifact` is now a thin specialization that shares the same
  bundle-location and loading helpers.

Example: `cargo run -p eventcontracts-model-runtime --example onnx_score -- <bundle_dir> <features…>`.

## Reference results (tennis ATP, pre-match winner model)

Produced by `eventcontracts tennis-xgboost-train --since-year 2010
--num-boost-round 300 --early-stopping-rounds 30` on the Jeff Sackmann ATP CSVs
in `data/tennis/`. These are the canonical "is the pipeline producing sane
numbers" reference; rerun the command to regenerate.

| Quantity | Value |
| --- | --- |
| Matches (2010→2026) | 46,837 |
| Mirrored feature rows | 93,674 |
| Train / validation / test rows | 65,534 / 14,044 / 14,096 |
| Test accuracy | 0.650 |
| Test ROC-AUC | 0.717 |
| Test log-loss | 0.617 |
| Test Brier score | 0.214 |
| ONNX export parity (max abs Δ) | 2.98e-7 |
| Python↔Rust ONNX parity (max abs Δ, spot check) | 1.19e-7 |

Interpretation: ROC-AUC ≈ 0.72 is in the expected band for a pre-match ATP
winner model from Elo/surface/form/rest/odds features; the model clears the
base-rate baseline on both log-loss and Brier. The sub-1e-6 export and
cross-language parity deltas confirm the saved ONNX graph is a faithful copy of
the trained booster and that Python and Rust inference agree.
