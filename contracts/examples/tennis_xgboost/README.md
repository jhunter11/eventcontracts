# Tennis XGBoost ONNX Contract

This example pins the feature order for the ATP pre-match XGBoost model.

The Python research path should train an XGBoost binary classifier and export
`model.onnx` with `eventcontracts.research.export_xgboost_onnx`. The Rust runner
only needs to reproduce the feature vector in the order declared by
`feature_schema.json`, supply it as a float32 ONNX tensor named `features`, and
read the player-1 win probability output.

Parity cases should include:

- raw pre-match snapshot inputs,
- the ordered feature vector,
- the ONNX probability output,
- the expected strategy decision, if a trading threshold is attached.
