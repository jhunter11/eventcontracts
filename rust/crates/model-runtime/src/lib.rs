//! ONNX model-serving boundary for promoted research artifacts.
//!
//! Two layers:
//!
//! - [`OnnxScorer`] is a generic, venue/model-agnostic wrapper around an ONNX
//!   `Session`. It implements [`Scorer`](eventcontracts_feature_builder::Scorer)
//!   so hot-path strategies can take a `&mut dyn Scorer` without depending on
//!   `ort` themselves.
//! - [`TennisOnnxArtifact`] / [`TennisOnnxModel`] keep their existing tennis
//!   API stable; internally they're now thin specializations on top of
//!   `OnnxScorer`. The Python-side feature contract is unchanged.

use eventcontracts_feature_builder::{
    tennis_v2_feature_vector, tennis_xgboost_feature_vector, Scorer, ScorerError,
    TennisMatchSnapshot, TennisV2Snapshot, TENNIS_V2_FEATURE_NAMES, TENNIS_XGBOOST_FEATURE_NAMES,
};
use ndarray::Array2;
use ort::{
    session::Session,
    value::{TensorRef, ValueType},
};
use parking_lot::Mutex;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use thiserror::Error;

#[derive(Debug, Error)]
pub enum ModelRuntimeError {
    #[error("expected {expected} tennis features, received {actual}")]
    FeatureWidth { expected: usize, actual: usize },
    #[error("onnx model did not expose `probabilities` output")]
    MissingProbabilityOutput,
    #[error("onnx model returned no player-1 probability")]
    MissingPlayerOneProbability,
    #[error("invalid input tensor shape: {0}")]
    Shape(#[from] ndarray::ShapeError),
    #[error("onnx runtime error: {0}")]
    Ort(#[from] ort::Error),
    #[error("onnx session builder error: {0}")]
    SessionBuilder(String),
    #[error("artifact missing model at {0}")]
    MissingModel(String),
    #[error("artifact missing feature schema at {0}")]
    MissingFeatureSchema(String),
    #[error("feature schema mismatch at index {index}: expected `{expected}`, got `{actual}`")]
    FeatureSchemaMismatch {
        index: usize,
        expected: &'static str,
        actual: String,
    },
    #[error("feature schema JSON error: {0}")]
    FeatureSchemaJson(#[from] serde_json::Error),
    #[error("feature schema IO error: {0}")]
    FeatureSchemaIo(#[from] std::io::Error),
    #[error("model signature mismatch: {0}")]
    Signature(String),
    #[error("invalid probability output {value}: {reason}")]
    InvalidProbability { value: f32, reason: &'static str },
}

// ---------- generic onnx scorer ----------

/// Generic ONNX scorer. Holds one `Session` plus the input / output tensor
/// names and an expected input width. Implements [`Scorer`].
///
/// `output_select` picks which element of the output tensor to return:
/// - `OutputSelect::All` returns the whole flat output (useful for vector
///   regression outputs).
/// - `OutputSelect::ScalarAt(index)` returns a 1-element vec — used by
///   classifiers where you want, e.g., the probability of class 1.
pub struct OnnxScorer {
    /// `ort::Session::run` requires `&mut self` in ort 2.x, so we wrap in a
    /// `parking_lot::Mutex`. The lock is held only for the duration of one
    /// inference (typically <1ms for our models) — fine for the current
    /// single-worker setup. Switch to `Arc<OnnxScorer>` with per-worker
    /// session pools if contention shows up in profiling.
    session: Mutex<Session>,
    pub input_name: String,
    pub output_name: String,
    input_width: usize,
    output_select: OutputSelect,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum OutputSelect {
    /// Return every f32 in the named output tensor, flattened in C order.
    All,
    /// Return a single-element `Vec<f32>` holding the value at `[0, index]`.
    ScalarAt(usize),
}

impl OnnxScorer {
    pub fn load(
        path: impl AsRef<Path>,
        input_name: impl Into<String>,
        output_name: impl Into<String>,
        input_width: usize,
        output_select: OutputSelect,
    ) -> Result<Self, ModelRuntimeError> {
        let input_name = input_name.into();
        let output_name = output_name.into();
        let session = Session::builder()?
            .with_intra_threads(1)
            .map_err(|e| ModelRuntimeError::SessionBuilder(e.to_string()))?
            .with_intra_op_spinning(false)
            .map_err(|e| ModelRuntimeError::SessionBuilder(e.to_string()))?
            .commit_from_file(path)?;
        validate_session_signature(
            &session,
            &input_name,
            &output_name,
            input_width,
            output_select,
        )?;
        Ok(Self {
            session: Mutex::new(session),
            input_name,
            output_name,
            input_width,
            output_select,
        })
    }

    fn run_inner(&self, features: &[f32]) -> Result<Vec<f32>, ModelRuntimeError> {
        if features.len() != self.input_width {
            return Err(ModelRuntimeError::FeatureWidth {
                expected: self.input_width,
                actual: features.len(),
            });
        }
        let input = Array2::from_shape_vec((1, features.len()), features.to_vec())?;
        // parking_lot::Mutex — never poisons, faster than std::sync::Mutex.
        let mut session = self.session.lock();
        let outputs = session
            .run(ort::inputs![self.input_name.as_str() => TensorRef::from_array_view(&input)?])?;
        let array = outputs
            .get(self.output_name.as_str())
            .ok_or(ModelRuntimeError::MissingProbabilityOutput)?
            .try_extract_array::<f32>()?;
        match self.output_select {
            OutputSelect::All => Ok(array.iter().copied().collect()),
            OutputSelect::ScalarAt(idx) => {
                let value = array
                    .get([0, idx])
                    .copied()
                    .ok_or(ModelRuntimeError::MissingPlayerOneProbability)?;
                Ok(vec![value])
            }
        }
    }
}

pub struct OnnxScorerPool {
    scorers: Vec<OnnxScorer>,
    next: AtomicUsize,
}

impl OnnxScorerPool {
    pub fn load(
        path: impl AsRef<Path>,
        input_name: impl Into<String>,
        output_name: impl Into<String>,
        input_width: usize,
        output_select: OutputSelect,
        pool_size: usize,
    ) -> Result<Self, ModelRuntimeError> {
        let path = path.as_ref();
        let input_name = input_name.into();
        let output_name = output_name.into();
        let pool_size = pool_size.max(1);
        let mut scorers = Vec::with_capacity(pool_size);
        for _ in 0..pool_size {
            scorers.push(OnnxScorer::load(
                path,
                input_name.clone(),
                output_name.clone(),
                input_width,
                output_select,
            )?);
        }
        Ok(Self {
            scorers,
            next: AtomicUsize::new(0),
        })
    }
}

impl Scorer for OnnxScorerPool {
    fn input_width(&self) -> usize {
        self.scorers[0].input_width()
    }

    fn predict(&self, features: &[f32]) -> Result<Vec<f32>, ScorerError> {
        let index = self.next.fetch_add(1, Ordering::Relaxed) % self.scorers.len();
        self.scorers[index].predict(features)
    }
}

impl Scorer for OnnxScorer {
    fn input_width(&self) -> usize {
        self.input_width
    }
    fn predict(&self, features: &[f32]) -> Result<Vec<f32>, ScorerError> {
        if features.len() != self.input_width {
            return Err(ScorerError::InputWidth {
                expected: self.input_width,
                actual: features.len(),
            });
        }
        let out = self
            .run_inner(features)
            .map_err(|e| ScorerError::Backend(e.to_string()))?;
        validate_scorer_outputs(
            &out,
            matches!(self.output_select, OutputSelect::ScalarAt(_)),
        )?;
        Ok(out)
    }
}

fn validate_scorer_outputs(values: &[f32], probability_mode: bool) -> Result<(), ScorerError> {
    for (index, value) in values.iter().copied().enumerate() {
        if !value.is_finite() {
            return Err(ScorerError::InvalidOutput {
                index,
                value,
                reason: "not finite",
            });
        }
        if probability_mode && !(0.0..=1.0).contains(&value) {
            return Err(ScorerError::InvalidOutput {
                index,
                value,
                reason: "outside [0, 1]",
            });
        }
    }
    Ok(())
}

fn validate_probability(value: f32) -> Result<f32, ModelRuntimeError> {
    if !value.is_finite() {
        return Err(ModelRuntimeError::InvalidProbability {
            value,
            reason: "not finite",
        });
    }
    if !(0.0..=1.0).contains(&value) {
        return Err(ModelRuntimeError::InvalidProbability {
            value,
            reason: "outside [0, 1]",
        });
    }
    Ok(value)
}

fn validate_session_signature(
    session: &Session,
    input_name: &str,
    output_name: &str,
    input_width: usize,
    output_select: OutputSelect,
) -> Result<(), ModelRuntimeError> {
    let input = session
        .inputs()
        .iter()
        .find(|input| input.name() == input_name)
        .ok_or_else(|| ModelRuntimeError::Signature(format!("missing input `{input_name}`")))?;
    validate_tensor_width("input", input.dtype(), input_width)?;

    let output = session
        .outputs()
        .iter()
        .find(|output| output.name() == output_name)
        .ok_or_else(|| ModelRuntimeError::Signature(format!("missing output `{output_name}`")))?;
    if let OutputSelect::ScalarAt(index) = output_select {
        validate_output_index(output.dtype(), index)?;
    }
    Ok(())
}

fn validate_tensor_width(
    label: &'static str,
    dtype: &ValueType,
    expected_width: usize,
) -> Result<(), ModelRuntimeError> {
    let ValueType::Tensor { shape, .. } = dtype else {
        return Err(ModelRuntimeError::Signature(format!(
            "{label} is not a tensor"
        )));
    };
    if shape.len() >= 2 {
        let width = shape[1];
        if width >= 0 && width as usize != expected_width {
            return Err(ModelRuntimeError::Signature(format!(
                "{label} width {} does not match expected {}",
                width, expected_width
            )));
        }
    }
    Ok(())
}

fn validate_output_index(dtype: &ValueType, index: usize) -> Result<(), ModelRuntimeError> {
    let ValueType::Tensor { shape, .. } = dtype else {
        return Err(ModelRuntimeError::Signature(
            "output is not a tensor".into(),
        ));
    };
    if shape.len() >= 2 {
        let width = shape[1];
        if width >= 0 && index >= width as usize {
            return Err(ModelRuntimeError::Signature(format!(
                "output_index {index} is outside output width {width}"
            )));
        }
    }
    Ok(())
}

// ---------- generic promoted ONNX bundle ----------

/// A promoted ONNX artifact bundle for *any* model family.
///
/// This is the model-agnostic counterpart to [`TennisOnnxArtifact`]: it reads
/// the bundle's `feature_schema.json` (any feature set, any width), resolves
/// the model's input/output tensors, and serves scores through a pooled
/// [`OnnxScorer`]. The Rust runtime never needs per-model code to load a
/// promoted bundle — the Python `model-train` / tennis pipelines write a
/// schema + `model.onnx` that this loader understands.
///
/// The feature *values* are still produced upstream (by the hot-path feature
/// builder, in the schema's order); this loader validates the schema's feature
/// *count* against the model's input width and fails fast on a mismatch.
pub struct OnnxArtifact {
    scorer: OnnxScorerPool,
    pub bundle_dir: PathBuf,
    pub model_path: PathBuf,
    pub feature_schema_path: PathBuf,
    pub feature_names: Vec<String>,
    pub output_select: OutputSelect,
}

impl OnnxArtifact {
    /// Load a bundle directory. `output_select` chooses the model output the
    /// scorer returns — `ScalarAt(1)` for a binary classifier's positive-class
    /// probability, `All` for regression / multi-output models.
    pub fn load_bundle(
        bundle_dir: impl AsRef<Path>,
        output_select: OutputSelect,
    ) -> Result<Self, ModelRuntimeError> {
        let bundle_dir = bundle_dir.as_ref().to_path_buf();
        let model_path = find_bundle_model(&bundle_dir)?;
        let feature_schema_path = find_bundle_feature_schema(&bundle_dir)?;
        let feature_names = read_feature_schema_names(&feature_schema_path)?;
        let input_width = feature_names.len();
        let output_name = resolve_output_name(&model_path, output_select)?;
        let scorer = OnnxScorerPool::load(
            &model_path,
            "features",
            output_name,
            input_width,
            output_select,
            default_onnx_pool_size(),
        )?;
        Ok(Self {
            scorer,
            bundle_dir,
            model_path,
            feature_schema_path,
            feature_names,
            output_select,
        })
    }

    /// Number of features the model expects, i.e. the feature-schema width.
    pub fn input_width(&self) -> usize {
        self.feature_names.len()
    }

    /// Score one feature row (already in schema order). Returns the value(s)
    /// selected by `output_select`.
    pub fn predict_features(&self, features: &[f32]) -> Result<Vec<f32>, ModelRuntimeError> {
        self.scorer
            .predict(features)
            .map_err(|e| ModelRuntimeError::Signature(e.to_string()))
    }

    /// Convenience for binary classifiers loaded with `ScalarAt`: the single
    /// selected probability, validated to be a finite value in `[0, 1]`.
    pub fn predict_probability(&self, features: &[f32]) -> Result<f32, ModelRuntimeError> {
        let out = self.predict_features(features)?;
        let value = out
            .first()
            .copied()
            .ok_or(ModelRuntimeError::MissingPlayerOneProbability)?;
        validate_probability(value)
    }
}

impl Scorer for OnnxArtifact {
    fn input_width(&self) -> usize {
        self.feature_names.len()
    }

    fn predict(&self, features: &[f32]) -> Result<Vec<f32>, ScorerError> {
        self.scorer.predict(features)
    }
}

/// Read the ordered feature names from a bundle `feature_schema.json`.
pub fn read_feature_schema_names(path: impl AsRef<Path>) -> Result<Vec<String>, ModelRuntimeError> {
    let raw = fs::read_to_string(path)?;
    let parsed: serde_json::Value = serde_json::from_str(&raw)?;
    let features = parsed
        .get("features")
        .and_then(|value| value.as_array())
        .ok_or_else(|| {
            serde_json::Error::io(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                "feature schema missing features array",
            ))
        })?;
    if features.is_empty() {
        return Err(ModelRuntimeError::FeatureWidth {
            expected: 1,
            actual: 0,
        });
    }
    Ok(features
        .iter()
        .map(|value| {
            value
                .get("name")
                .and_then(|name| name.as_str())
                .unwrap_or("")
                .to_string()
        })
        .collect())
}

/// Open the model once to discover the output tensor name. For a classifier
/// (`ScalarAt`) prefer an output whose name contains `probab`; otherwise take
/// the first output (the regression value).
fn resolve_output_name(
    path: impl AsRef<Path>,
    output_select: OutputSelect,
) -> Result<String, ModelRuntimeError> {
    let session = Session::builder()?
        .with_intra_threads(1)
        .map_err(|e| ModelRuntimeError::SessionBuilder(e.to_string()))?
        .commit_from_file(path)?;
    let outputs = session.outputs();
    if outputs.is_empty() {
        return Err(ModelRuntimeError::MissingProbabilityOutput);
    }
    if matches!(output_select, OutputSelect::ScalarAt(_)) {
        if let Some(output) = outputs
            .iter()
            .find(|output| output.name().to_ascii_lowercase().contains("probab"))
        {
            return Ok(output.name().to_string());
        }
        return Ok(outputs[outputs.len() - 1].name().to_string());
    }
    Ok(outputs[0].name().to_string())
}

fn find_bundle_model(bundle_dir: &Path) -> Result<PathBuf, ModelRuntimeError> {
    first_existing(&[
        bundle_dir.join("model").join("model.onnx"),
        bundle_dir.join("model.onnx"),
    ])
    .ok_or_else(|| {
        ModelRuntimeError::MissingModel(bundle_dir.join("model/model.onnx").display().to_string())
    })
}

fn find_bundle_feature_schema(bundle_dir: &Path) -> Result<PathBuf, ModelRuntimeError> {
    first_existing(&[
        bundle_dir.join("feature_schema.json"),
        bundle_dir.join("model").join("feature_schema.json"),
        bundle_dir.join("contracts").join("feature_schema.json"),
    ])
    .ok_or_else(|| {
        ModelRuntimeError::MissingFeatureSchema(
            bundle_dir.join("feature_schema.json").display().to_string(),
        )
    })
}

// ---------- tennis specialization ----------

/// Tennis ONNX classifier. Now a thin specialization on top of
/// [`OnnxScorer`] — the generic engine handles the session, input/output
/// dispatch, and validation. This crate only adds the snapshot →
/// 20-feature-vector conversion.
pub struct TennisOnnxModel {
    scorer: OnnxScorerPool,
}

pub struct TennisOnnxArtifact {
    model: TennisOnnxModel,
    pub bundle_dir: PathBuf,
    pub model_path: PathBuf,
    pub feature_schema_path: PathBuf,
}

impl TennisOnnxArtifact {
    pub fn load_bundle(bundle_dir: impl AsRef<Path>) -> Result<Self, ModelRuntimeError> {
        let bundle_dir = bundle_dir.as_ref().to_path_buf();
        let model_path = find_bundle_model(&bundle_dir)?;
        let feature_schema_path = find_bundle_feature_schema(&bundle_dir)?;
        validate_tennis_feature_schema(&feature_schema_path)?;
        Ok(Self {
            model: TennisOnnxModel::load(&model_path)?,
            bundle_dir,
            model_path,
            feature_schema_path,
        })
    }

    pub fn predict_snapshot(
        &mut self,
        snapshot: &TennisMatchSnapshot,
    ) -> Result<f32, ModelRuntimeError> {
        self.model.predict_snapshot(snapshot)
    }
}

pub fn validate_tennis_feature_schema(path: impl AsRef<Path>) -> Result<(), ModelRuntimeError> {
    let raw = fs::read_to_string(path)?;
    let parsed: serde_json::Value = serde_json::from_str(&raw)?;
    let features = parsed
        .get("features")
        .and_then(|value| value.as_array())
        .ok_or_else(|| {
            serde_json::Error::io(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                "feature schema missing features array",
            ))
        })?;
    for (index, expected) in TENNIS_XGBOOST_FEATURE_NAMES.iter().enumerate() {
        let actual = features
            .get(index)
            .and_then(|value| value.get("name"))
            .and_then(|value| value.as_str())
            .unwrap_or("")
            .to_string();
        if actual != *expected {
            return Err(ModelRuntimeError::FeatureSchemaMismatch {
                index,
                expected,
                actual,
            });
        }
    }
    if features.len() != TENNIS_XGBOOST_FEATURE_NAMES.len() {
        return Err(ModelRuntimeError::FeatureWidth {
            expected: TENNIS_XGBOOST_FEATURE_NAMES.len(),
            actual: features.len(),
        });
    }
    Ok(())
}

/// Read the `schema_version` string from a bundle `feature_schema.json`.
/// Lets a caller pick the right tennis feature builder (v1 = 20 features,
/// v2 = 34) from the promoted bundle instead of a hard-coded assumption.
pub fn read_feature_schema_version(path: impl AsRef<Path>) -> Result<String, ModelRuntimeError> {
    let raw = fs::read_to_string(path)?;
    let parsed: serde_json::Value = serde_json::from_str(&raw)?;
    Ok(parsed
        .get("schema_version")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string())
}

/// Resolve a bundle's `feature_schema.json` and read its `schema_version`.
/// The live-runner uses this to choose the v1 (20-feature) or v2 (34-feature)
/// tennis scorer from the promoted bundle itself.
pub fn bundle_feature_schema_version(
    bundle_dir: impl AsRef<Path>,
) -> Result<String, ModelRuntimeError> {
    let schema = find_bundle_feature_schema(bundle_dir.as_ref())?;
    read_feature_schema_version(schema)
}

// ---------- tennis v2 specialization (34 features) ----------

/// Tennis v2 ONNX classifier — same shape as [`TennisOnnxModel`] but bound to
/// the 34-feature v2 contract and `tennis_v2_feature_vector`.
pub struct TennisV2OnnxModel {
    scorer: OnnxScorerPool,
}

impl TennisV2OnnxModel {
    pub fn load(path: impl AsRef<Path>) -> Result<Self, ModelRuntimeError> {
        let scorer = OnnxScorerPool::load(
            path,
            "features",
            "probabilities",
            TENNIS_V2_FEATURE_NAMES.len(),
            OutputSelect::ScalarAt(1),
            default_onnx_pool_size(),
        )?;
        Ok(Self { scorer })
    }

    pub fn predict_snapshot(
        &mut self,
        snapshot: &TennisV2Snapshot,
    ) -> Result<f32, ModelRuntimeError> {
        self.predict_features(&tennis_v2_feature_vector(snapshot))
    }

    pub fn predict_features(&mut self, features: &[f32]) -> Result<f32, ModelRuntimeError> {
        let out = self
            .scorer
            .predict(features)
            .map_err(|e| ModelRuntimeError::Signature(e.to_string()))?;
        let value = out
            .first()
            .copied()
            .ok_or(ModelRuntimeError::MissingPlayerOneProbability)?;
        validate_probability(value)
    }
}

pub struct TennisV2OnnxArtifact {
    model: TennisV2OnnxModel,
    pub bundle_dir: PathBuf,
    pub model_path: PathBuf,
    pub feature_schema_path: PathBuf,
}

impl TennisV2OnnxArtifact {
    pub fn load_bundle(bundle_dir: impl AsRef<Path>) -> Result<Self, ModelRuntimeError> {
        let bundle_dir = bundle_dir.as_ref().to_path_buf();
        let model_path = find_bundle_model(&bundle_dir)?;
        let feature_schema_path = find_bundle_feature_schema(&bundle_dir)?;
        validate_tennis_v2_feature_schema(&feature_schema_path)?;
        Ok(Self {
            model: TennisV2OnnxModel::load(&model_path)?,
            bundle_dir,
            model_path,
            feature_schema_path,
        })
    }

    pub fn predict_snapshot(
        &mut self,
        snapshot: &TennisV2Snapshot,
    ) -> Result<f32, ModelRuntimeError> {
        self.model.predict_snapshot(snapshot)
    }
}

/// Validate that a bundle `feature_schema.json` matches the v2 feature
/// contract in name and order before any inference.
pub fn validate_tennis_v2_feature_schema(path: impl AsRef<Path>) -> Result<(), ModelRuntimeError> {
    let raw = fs::read_to_string(path)?;
    let parsed: serde_json::Value = serde_json::from_str(&raw)?;
    let features = parsed
        .get("features")
        .and_then(|value| value.as_array())
        .ok_or_else(|| {
            serde_json::Error::io(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                "feature schema missing features array",
            ))
        })?;
    for (index, expected) in TENNIS_V2_FEATURE_NAMES.iter().enumerate() {
        let actual = features
            .get(index)
            .and_then(|value| value.get("name"))
            .and_then(|value| value.as_str())
            .unwrap_or("")
            .to_string();
        if actual != *expected {
            return Err(ModelRuntimeError::FeatureSchemaMismatch {
                index,
                expected,
                actual,
            });
        }
    }
    if features.len() != TENNIS_V2_FEATURE_NAMES.len() {
        return Err(ModelRuntimeError::FeatureWidth {
            expected: TENNIS_V2_FEATURE_NAMES.len(),
            actual: features.len(),
        });
    }
    Ok(())
}

fn first_existing(candidates: &[PathBuf]) -> Option<PathBuf> {
    candidates.iter().find(|path| path.exists()).cloned()
}

impl TennisOnnxModel {
    pub fn load(path: impl AsRef<Path>) -> Result<Self, ModelRuntimeError> {
        let scorer = OnnxScorerPool::load(
            path,
            "features",
            "probabilities",
            TENNIS_XGBOOST_FEATURE_NAMES.len(),
            OutputSelect::ScalarAt(1),
            default_onnx_pool_size(),
        )?;
        Ok(Self { scorer })
    }

    pub fn predict_snapshot(
        &mut self,
        snapshot: &TennisMatchSnapshot,
    ) -> Result<f32, ModelRuntimeError> {
        self.predict_features(&tennis_xgboost_feature_vector(snapshot))
    }

    pub fn predict_features(&mut self, features: &[f32]) -> Result<f32, ModelRuntimeError> {
        let out = self
            .scorer
            .predict(features)
            .map_err(|e| ModelRuntimeError::Signature(e.to_string()))?;
        let value = out
            .first()
            .copied()
            .ok_or(ModelRuntimeError::MissingPlayerOneProbability)?;
        validate_probability(value)
    }
}

fn default_onnx_pool_size() -> usize {
    std::env::var("EVENTCONTRACTS_ONNX_POOL_SIZE")
        .ok()
        .and_then(|raw| raw.parse::<usize>().ok())
        .filter(|size| *size > 0)
        .unwrap_or_else(|| {
            std::thread::available_parallelism()
                .map(usize::from)
                .unwrap_or(1)
                .clamp(1, 4)
        })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    #[test]
    fn validates_expected_feature_schema_order() {
        let path = temp_schema_path("valid");
        let features: Vec<_> = TENNIS_XGBOOST_FEATURE_NAMES
            .iter()
            .map(|name| serde_json::json!({"name": name, "dtype": "float32"}))
            .collect();
        fs::write(&path, serde_json::json!({"features": features}).to_string()).unwrap();

        validate_tennis_feature_schema(&path).unwrap();
        let _ = fs::remove_file(path);
    }

    #[test]
    fn rejects_feature_schema_order_mismatch() {
        let path = temp_schema_path("invalid");
        let mut names = TENNIS_XGBOOST_FEATURE_NAMES;
        names.swap(0, 1);
        let features: Vec<_> = names
            .iter()
            .map(|name| serde_json::json!({"name": name, "dtype": "float32"}))
            .collect();
        fs::write(&path, serde_json::json!({"features": features}).to_string()).unwrap();

        let err = validate_tennis_feature_schema(&path).unwrap_err();
        assert!(matches!(
            err,
            ModelRuntimeError::FeatureSchemaMismatch { index: 0, .. }
        ));
        let _ = fs::remove_file(path);
    }

    #[test]
    fn reads_generic_feature_schema_names_of_any_shape() {
        let path = temp_schema_path("generic");
        let schema = serde_json::json!({
            "schema_id": "macro_cpi_features",
            "schema_version": "1",
            "features": [
                {"name": "surprise_z", "dtype": "float32"},
                {"name": "trend_3m", "dtype": "float32"},
                {"name": "regime_flag", "dtype": "float32"},
            ],
        });
        fs::write(&path, schema.to_string()).unwrap();

        let names = read_feature_schema_names(&path).unwrap();
        assert_eq!(names, vec!["surprise_z", "trend_3m", "regime_flag"]);
        let _ = fs::remove_file(path);
    }

    #[test]
    fn rejects_empty_generic_feature_schema() {
        let path = temp_schema_path("empty");
        fs::write(&path, serde_json::json!({"features": []}).to_string()).unwrap();
        let err = read_feature_schema_names(&path).unwrap_err();
        assert!(matches!(
            err,
            ModelRuntimeError::FeatureWidth { actual: 0, .. }
        ));
        let _ = fs::remove_file(path);
    }

    #[test]
    fn validates_expected_v2_feature_schema_order_and_reads_version() {
        let path = temp_schema_path("v2-valid");
        let features: Vec<_> = TENNIS_V2_FEATURE_NAMES
            .iter()
            .map(|name| serde_json::json!({"name": name, "dtype": "float32"}))
            .collect();
        fs::write(
            &path,
            serde_json::json!({"schema_version": "2", "features": features}).to_string(),
        )
        .unwrap();

        validate_tennis_v2_feature_schema(&path).unwrap();
        assert_eq!(read_feature_schema_version(&path).unwrap(), "2");
        let _ = fs::remove_file(path);
    }

    #[test]
    fn rejects_v2_schema_with_v1_feature_set() {
        // A v1 (20-feature) schema must be rejected by the v2 validator so a
        // bundle/feature-builder mismatch can't reach live inference.
        let path = temp_schema_path("v2-wrong");
        let features: Vec<_> = TENNIS_XGBOOST_FEATURE_NAMES
            .iter()
            .map(|name| serde_json::json!({"name": name, "dtype": "float32"}))
            .collect();
        fs::write(&path, serde_json::json!({"features": features}).to_string()).unwrap();

        let err = validate_tennis_v2_feature_schema(&path).unwrap_err();
        // First divergent name is at index 2 (v1 rank_log_advantage vs v2 elo_blend_diff).
        assert!(matches!(
            err,
            ModelRuntimeError::FeatureSchemaMismatch { .. }
        ));
        let _ = fs::remove_file(path);
    }

    /// End-to-end ONNX inference on a real promoted v2 bundle. Skipped unless
    /// `EVENTCONTRACTS_TENNIS_V2_BUNDLE` points at a bundle dir (so CI without
    /// the artifact stays green); run locally after exporting the v2 model to
    /// prove the Rust `ort` path loads + scores it and respects the monotone
    /// Elo constraint. Closes the last seam beyond Python export-parity.
    #[test]
    fn v2_bundle_scores_and_respects_elo_monotonicity_when_present() {
        let Ok(dir) = std::env::var("EVENTCONTRACTS_TENNIS_V2_BUNDLE") else {
            return;
        };
        let mut artifact = TennisV2OnnxArtifact::load_bundle(&dir).expect("load v2 bundle");
        let neutral = TennisV2Snapshot::default();
        let p_neutral = artifact.predict_snapshot(&neutral).unwrap();
        assert!(
            p_neutral > 0.0 && p_neutral < 1.0,
            "neutral probability out of range: {p_neutral}"
        );
        let strong = TennisV2Snapshot {
            p1_elo: 1950.0,
            p2_elo: 1500.0,
            p1_surface_elo: 1950.0,
            p2_surface_elo: 1500.0,
            p1_elo_blend: 1950.0,
            p2_elo_blend: 1500.0,
            ..Default::default()
        };
        let p_strong = artifact.predict_snapshot(&strong).unwrap();
        assert!(
            p_strong >= p_neutral,
            "elo-favored p1 must not score lower (monotone): strong={p_strong} neutral={p_neutral}"
        );
    }

    fn temp_schema_path(label: &str) -> PathBuf {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        std::env::temp_dir().join(format!("tennis-schema-{label}-{nonce}.json"))
    }
}
