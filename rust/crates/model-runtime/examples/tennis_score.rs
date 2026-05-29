use eventcontracts_feature_builder::TENNIS_XGBOOST_FEATURE_NAMES;
use eventcontracts_model_runtime::TennisOnnxModel;
use std::env;
use std::error::Error;

fn main() -> Result<(), Box<dyn Error>> {
    let mut args = env::args().skip(1);
    let model_path = args
        .next()
        .ok_or("usage: tennis_score <model.onnx> <20 float32 feature values>")?;
    let features: Vec<f32> = args
        .map(|value| value.parse::<f32>())
        .collect::<Result<Vec<_>, _>>()?;
    if features.len() != TENNIS_XGBOOST_FEATURE_NAMES.len() {
        return Err(format!(
            "expected {} feature values, received {}",
            TENNIS_XGBOOST_FEATURE_NAMES.len(),
            features.len()
        )
        .into());
    }
    let probability = TennisOnnxModel::load(model_path)?.predict_features(&features)?;
    println!("{probability:.9}");
    Ok(())
}
