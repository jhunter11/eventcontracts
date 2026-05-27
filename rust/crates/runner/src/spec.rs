//! `StrategySpec` artifact loader. Mirrors the Python TOML schema at
//! `contracts/schemas/strategy_spec.schema.json` so the **same TOML file**
//! drives both research (Python) and execution (Rust).
//!
//! The Rust loader is intentionally tolerant: unknown TOML keys are kept in
//! `parameters` rather than rejected, matching how the Python framework reads
//! the file. Strict schema validation happens at the bundle-promotion gate.

use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::path::Path;
use thiserror::Error;

#[derive(Debug, Error)]
pub enum SpecError {
    #[error("failed to read spec file `{path}`: {source}")]
    Io {
        path: String,
        #[source]
        source: std::io::Error,
    },
    #[error("toml parse failed: {0}")]
    Toml(#[from] toml::de::Error),
    #[error("missing required field `{0}`")]
    MissingField(&'static str),
    #[error("unknown strategy `{name}@{version}` (no factory registered)")]
    UnknownStrategy { name: String, version: String },
    #[error("strategy factory failed: {0}")]
    Factory(String),
    #[error("parameter `{0}` has invalid value: {1}")]
    InvalidParameter(String, String),
}

/// Wire form of `[parameters]` — TOML scalar values that are accepted by the
/// JSON-schema `oneOf` (string/number/boolean).
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(untagged)]
pub enum ParamValue {
    Str(String),
    Int(i64),
    Float(f64),
    Bool(bool),
}

impl ParamValue {
    /// Coerce to string. Numbers come back as their canonical decimal form.
    pub fn as_str(&self) -> String {
        match self {
            ParamValue::Str(s) => s.clone(),
            ParamValue::Int(i) => i.to_string(),
            ParamValue::Float(f) => format!("{f}"),
            ParamValue::Bool(b) => b.to_string(),
        }
    }

    /// Parse as f64 — accepts any of the three numeric/string forms. Strings
    /// must already be a decimal literal.
    pub fn as_f64(&self) -> Result<f64, String> {
        match self {
            ParamValue::Float(f) => Ok(*f),
            ParamValue::Int(i) => Ok(*i as f64),
            ParamValue::Str(s) => s.parse::<f64>().map_err(|e| e.to_string()),
            ParamValue::Bool(_) => Err("bool not coercible to f64".into()),
        }
    }
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Subscription {
    #[serde(default)]
    pub venues: Vec<String>,
    #[serde(default)]
    pub instrument_patterns: Vec<String>,
    #[serde(default)]
    pub event_kinds: Vec<String>,
    #[serde(default)]
    pub external_sources: Vec<String>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct ExecutionPriority {
    #[serde(default = "default_tier")]
    pub tier: String,
    #[serde(default)]
    pub max_delay_ms: Option<u64>,
    #[serde(default)]
    pub expires_after_ms: Option<u64>,
    #[serde(default)]
    pub reason: Option<String>,
}

fn default_tier() -> String {
    "standard".to_string()
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct StrategySpecArtifact {
    pub strategy_id: String,
    pub name: String,
    pub version: String,
    #[serde(default)]
    pub description: String,
    #[serde(default)]
    pub feature_schema_id: Option<String>,
    #[serde(default)]
    pub subscription: Subscription,
    #[serde(default)]
    pub default_execution_priority: Option<ExecutionPriority>,
    #[serde(default)]
    pub parameters: BTreeMap<String, ParamValue>,
    #[serde(default)]
    pub tags: BTreeMap<String, String>,
}

impl StrategySpecArtifact {
    pub fn from_toml_str(s: &str) -> Result<Self, SpecError> {
        let spec: StrategySpecArtifact = toml::from_str(s)?;
        spec.validate()?;
        Ok(spec)
    }

    pub fn load(path: impl AsRef<Path>) -> Result<Self, SpecError> {
        let p = path.as_ref();
        let text = std::fs::read_to_string(p).map_err(|e| SpecError::Io {
            path: p.display().to_string(),
            source: e,
        })?;
        Self::from_toml_str(&text)
    }

    fn validate(&self) -> Result<(), SpecError> {
        if self.strategy_id.is_empty() {
            return Err(SpecError::MissingField("strategy_id"));
        }
        if self.name.is_empty() {
            return Err(SpecError::MissingField("name"));
        }
        if self.version.is_empty() {
            return Err(SpecError::MissingField("version"));
        }
        Ok(())
    }

    /// Required parameter as f64 — surfaces a typed error pointing at the
    /// failing field rather than a panic.
    pub fn param_f64(&self, key: &str) -> Result<f64, SpecError> {
        let v = self
            .parameters
            .get(key)
            .ok_or_else(|| SpecError::InvalidParameter(key.to_string(), "missing".into()))?;
        v.as_f64()
            .map_err(|e| SpecError::InvalidParameter(key.to_string(), e))
    }

    /// Optional parameter as f64 with a default.
    pub fn param_f64_or(&self, key: &str, default: f64) -> Result<f64, SpecError> {
        match self.parameters.get(key) {
            None => Ok(default),
            Some(v) => v
                .as_f64()
                .map_err(|e| SpecError::InvalidParameter(key.to_string(), e)),
        }
    }

    /// Optional parameter as decimal-string (canonical form, no rounding).
    pub fn param_str_or(&self, key: &str, default: &str) -> String {
        self.parameters
            .get(key)
            .map(|v| v.as_str())
            .unwrap_or_else(|| default.to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const SAMPLE_WEATHER_SPEC: &str = r#"
strategy_id = "weather-threshold-v1"
name = "weather_threshold"
version = "0.1.0"
description = "Example bundle: threshold trade on Kalshi weather contracts."
feature_schema_id = "weather_threshold_features"

[subscription]
venues = ["kalshi"]
instrument_patterns = ["WEATHER-*"]
event_kinds = ["trade", "quote", "external"]

[default_execution_priority]
tier = "standard"

[parameters]
buy_below = "0.40"
sell_above = "0.60"
size = "10"

[tags]
family = "weather"
mode = "paper"
"#;

    #[test]
    fn parses_python_authored_weather_threshold_spec() {
        let spec = StrategySpecArtifact::from_toml_str(SAMPLE_WEATHER_SPEC).unwrap();
        assert_eq!(spec.strategy_id, "weather-threshold-v1");
        assert_eq!(spec.name, "weather_threshold");
        assert_eq!(spec.version, "0.1.0");
        assert_eq!(spec.subscription.venues, vec!["kalshi"]);
        assert_eq!(spec.param_f64("buy_below").unwrap(), 0.40);
        assert_eq!(spec.param_f64("sell_above").unwrap(), 0.60);
        assert_eq!(spec.param_str_or("size", "1"), "10");
        assert_eq!(spec.tags.get("family").map(String::as_str), Some("weather"));
    }

    #[test]
    fn missing_strategy_id_rejected() {
        let toml = r#"name = "x"
version = "1"
strategy_id = ""
"#;
        let err = StrategySpecArtifact::from_toml_str(toml).unwrap_err();
        assert!(matches!(err, SpecError::MissingField("strategy_id")));
    }

    #[test]
    fn parameters_accept_string_number_and_bool() {
        let toml = r#"
strategy_id = "s"
name = "n"
version = "v"
[parameters]
a = "0.5"
b = 0.5
c = 5
d = true
"#;
        let spec = StrategySpecArtifact::from_toml_str(toml).unwrap();
        assert_eq!(spec.param_f64("a").unwrap(), 0.5);
        assert_eq!(spec.param_f64("b").unwrap(), 0.5);
        assert_eq!(spec.param_f64("c").unwrap(), 5.0);
        assert!(spec.param_f64("d").is_err());
    }
}
