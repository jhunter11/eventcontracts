//! Score a feature row against *any* promoted ONNX bundle via the
//! model-agnostic [`OnnxArtifact`] loader.
//!
//! ```text
//! cargo run -p eventcontracts-model-runtime --example onnx_score -- <bundle_dir> <feature floats...>
//! ```
//!
//! The number of feature floats must equal the bundle's feature-schema width.
//! Unlike `tennis_score`, this example hard-codes nothing about the model: it
//! reads the width from `feature_schema.json` and the output tensor from the
//! ONNX graph.

use eventcontracts_model_runtime::{OnnxArtifact, OutputSelect};
use std::env;
use std::error::Error;

fn main() -> Result<(), Box<dyn Error>> {
    let mut args = env::args().skip(1);
    let bundle_dir = args
        .next()
        .ok_or("usage: onnx_score <bundle_dir> <feature floats...>")?;
    let features: Vec<f32> = args
        .map(|value| value.parse::<f32>())
        .collect::<Result<Vec<_>, _>>()?;

    let artifact = OnnxArtifact::load_bundle(&bundle_dir, OutputSelect::ScalarAt(1))?;
    if features.len() != artifact.input_width() {
        return Err(format!(
            "bundle expects {} features ({:?}), received {}",
            artifact.input_width(),
            artifact.feature_names,
            features.len()
        )
        .into());
    }
    let probability = artifact.predict_probability(&features)?;
    println!("probability={probability:.9}");
    println!("feature_width={}", artifact.input_width());
    Ok(())
}
