# Tennis XGBoost ONNX Design

This design follows `docs/deep-research-report (17).md` and keeps the Python
research implementation portable to the Rust runner.

## Model Contract

- Model family: XGBoost binary classifier.
- Target: probability that `player_1` wins the match.
- Deployment artifact: `model.onnx`.
- ONNX input name: `features`.
- Input tensor shape: `[N, 20]`.
- Input tensor dtype: `float32`.
- Feature order: exactly `contracts/examples/tennis_xgboost/feature_schema.json`.
- Python source of truth: `eventcontracts.research.tennis_xgboost`.

The Rust runner should not reproduce XGBoost internals. It should reproduce the
feature vector, load `model.onnx`, run inference, and compare parity cases
against Python probabilities.

## Feature State

All state is pre-match and temporal:

- overall Elo,
- surface Elo,
- head-to-head wins,
- recent rolling win form,
- days since previous match,
- optional bookmaker odds.

The training builder emits each match row before updating those states, so the
current match never leaks into its own features.

## Research Flow

1. Load Jeff Sackmann ATP rows into a Polars DataFrame.
2. Call `build_sackmann_training_frame(...)`.
3. Split with `temporal_train_validation_test_split(...)`.
4. Train with `train_xgboost_binary(...)`.
5. Export with `export_xgboost_onnx(model, "model.onnx")`.
6. Promote `model.onnx` plus `feature_schema.json` into an artifact bundle.

Runnable command:

```powershell
.\.venv\Scripts\python.exe -m eventcontracts.cli tennis-xgboost-train `
  --data-dir data/tennis/tennis_atp/tennis_atp-master `
  --out-root artifacts/tennis_xgboost `
  --since-year 2000 `
  --include-challengers
```

For smoke runs, add `--max-matches 1000 --num-boost-round 30`.

To score future matches from a manually curated CSV:

```powershell
.\.venv\Scripts\python.exe -m eventcontracts.cli tennis-xgboost-score `
  --model artifacts/tennis_xgboost/bundles/<bundle>/model/model.onnx `
  --input contracts/examples/tennis_xgboost/upcoming_matches_template.csv `
  --out artifacts/tennis_xgboost/upcoming_signals.jsonl
```

The scorer emits one JSONL `external` signal per match with
`source = tennis_xgboost_onnx`, `market_id`, and
`player_1_win_probability`.

## Rust Flow

1. Maintain the same state maps for Elo, surface Elo, H2H, recent form, and rest.
2. Emit the feature vector in schema order.
3. Cast to the ONNX runtime input tensor.
4. Read player-1 win probability.
5. Feed that probability into a strategy threshold or fair-value comparison.

Parity tests should pin raw snapshot input, ordered feature vector, ONNX output,
and resulting strategy decision.

The Rust implementation now has:

- `eventcontracts-feature-builder::tennis_xgboost_feature_vector(...)` for the
  20-value float32 feature row.
- `eventcontracts-model-runtime::TennisOnnxModel` for loading `model.onnx` and
  reading the `probabilities[0, 1]` player-1 win probability.
- `eventcontracts-model-runtime::TennisOnnxArtifact` for loading a promoted
  bundle and validating `feature_schema.json` before inference.
- `configs/strategies/sports-tennis-xgboost.toml` as the paper strategy spec.
- `eventcontracts-runner::TennisXgboostStrategy` registered under
  `sports_tennis_xgboost`, so the Rust live runner can instantiate the shared
  TOML spec.
- `eventcontracts-live-runner --tennis-artifact <bundle> --tennis-snapshots-jsonl
  <upcoming.jsonl>` to score manually curated upcoming matches into external
  prediction events before live Kalshi quote processing.

The remaining live-production adapter is market mapping: upcoming tennis match
snapshots must be matched to Kalshi/venue market IDs and converted into
pre-match feature vectors before the runner can paper trade future matches.
