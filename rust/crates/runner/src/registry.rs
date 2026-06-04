//! Strategy registry — maps `strategy.name` (from the TOML spec) to a Rust
//! factory that produces a `Box<dyn StrategyRuntime>`.
//!
//! ## Simple promotion contract
//!
//! 1. Researcher writes/edits a TOML spec in `configs/strategies/<name>.toml`
//!    (same file both Python and Rust read).
//! 2. Engineer adds a Rust implementation under `runner/src/strategies/` that
//!    implements `FromSpec` and `StrategyRuntime`.
//! 3. Register the factory with one line in `default_registry()` below.
//!
//! The TOML's `name` field is the lookup key. `version` is informational at
//! this layer — promotion gates (bundle validation, parity cases) enforce
//! version pinning separately.

use crate::spec::{SpecError, StrategySpecArtifact};
use crate::StrategyRuntime;
use std::collections::HashMap;

pub trait FromSpec: Sized {
    fn from_spec(spec: &StrategySpecArtifact) -> Result<Self, SpecError>;
}

type Factory =
    Box<dyn Fn(&StrategySpecArtifact) -> Result<Box<dyn StrategyRuntime>, SpecError> + Send + Sync>;

pub struct StrategyRegistry {
    factories: HashMap<String, Factory>,
}

impl StrategyRegistry {
    pub fn new() -> Self {
        Self {
            factories: HashMap::new(),
        }
    }

    /// Register a factory under a strategy `name`. Re-registering the same
    /// name overwrites the previous entry.
    pub fn register<F>(&mut self, name: impl Into<String>, factory: F)
    where
        F: Fn(&StrategySpecArtifact) -> Result<Box<dyn StrategyRuntime>, SpecError>
            + Send
            + Sync
            + 'static,
    {
        self.factories.insert(name.into(), Box::new(factory));
    }

    pub fn instantiate(
        &self,
        spec: &StrategySpecArtifact,
    ) -> Result<Box<dyn StrategyRuntime>, SpecError> {
        if let Some(factory) = self.factories.get(spec.name.as_str()) {
            return factory(spec);
        }
        let archetype = spec
            .tags
            .get("archetype")
            .or_else(|| spec.tags.get("strategy_archetype"))
            .map(String::as_str);
        if archetype == Some("external_edge")
            || spec.param_str_or("archetype", "") == "external_edge"
        {
            return Ok(Box::new(crate::ExternalEdgeStrategy::from_spec(spec)?));
        }
        Err(SpecError::UnknownStrategy {
            name: spec.name.clone(),
            version: spec.version.clone(),
        })
    }

    pub fn registered_names(&self) -> Vec<&str> {
        let mut names: Vec<&str> = self.factories.keys().map(String::as_str).collect();
        names.sort_unstable();
        names
    }
}

impl Default for StrategyRegistry {
    fn default() -> Self {
        Self::new()
    }
}

/// Built-in registry. Every strategy listed here ships with both a Python
/// implementation (in `python/src/eventcontracts/plugins/strategies/`) and a
/// Rust implementation. Their TOML specs are interchangeable.
pub fn default_registry() -> StrategyRegistry {
    let mut r = StrategyRegistry::new();
    // `weather_threshold` is the example bundle distributed in
    // `contracts/examples/weather_threshold/`. The Rust impl is ThresholdStrategy
    // with params (buy_below, sell_above, size) read from the spec.
    r.register("weather_threshold", |spec| {
        Ok(Box::new(crate::ThresholdStrategy::from_spec(spec)?))
    });
    // `example_threshold` is the Python toy strategy in
    // `python/src/eventcontracts/plugins/strategies/example_threshold.py`.
    // Same parameters, same Rust impl.
    r.register("example_threshold", |spec| {
        Ok(Box::new(crate::ThresholdStrategy::from_spec(spec)?))
    });
    r.register("sports_tennis_xgboost", |spec| {
        Ok(Box::new(crate::TennisXgboostStrategy::from_spec(spec)?))
    });
    r.register("weather_temperature_arbitrage", |spec| {
        Ok(Box::new(
            crate::WeatherTemperatureArbitrageStrategy::from_spec(spec)?,
        ))
    });
    r.register("entertainment_box_office", |spec| {
        Ok(Box::new(crate::EntertainmentBoxOfficeStrategy::from_spec(
            spec,
        )?))
    });
    r
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::spec::StrategySpecArtifact;

    fn weather_spec() -> StrategySpecArtifact {
        StrategySpecArtifact::from_toml_str(
            r#"
strategy_id = "weather-threshold-v1"
name = "weather_threshold"
version = "0.1.0"
[parameters]
buy_below = "0.40"
sell_above = "0.60"
size = "10"
"#,
        )
        .unwrap()
    }

    #[test]
    fn default_registry_has_documented_names() {
        let r = default_registry();
        let names = r.registered_names();
        assert!(names.contains(&"weather_threshold"));
        assert!(names.contains(&"example_threshold"));
        assert!(names.contains(&"sports_tennis_xgboost"));
        assert!(names.contains(&"weather_temperature_arbitrage"));
    }

    #[test]
    fn instantiate_returns_runtime_for_known_name() {
        let r = default_registry();
        let strat = r.instantiate(&weather_spec()).unwrap();
        assert_eq!(strat.strategy_id(), "weather-threshold-v1");
    }

    #[test]
    fn instantiate_returns_runtime_for_tennis_strategy() {
        let r = default_registry();
        let spec = StrategySpecArtifact::from_toml_str(
            r#"
strategy_id = "sports-tennis-xgboost-v1"
name = "sports_tennis_xgboost"
version = "1.0.0"
[parameters]
prediction_source = "tennis_xgboost_onnx"
min_edge_bps = "150"
size = "5"
"#,
        )
        .unwrap();
        let strat = r.instantiate(&spec).unwrap();
        assert_eq!(strat.strategy_id(), "sports-tennis-xgboost-v1");
    }

    #[test]
    fn instantiate_returns_runtime_for_weather_temperature_arbitrage() {
        let r = default_registry();
        let spec = StrategySpecArtifact::from_toml_str(
            r#"
strategy_id = "weather-temperature-arbitrage-live-v1"
name = "weather_temperature_arbitrage"
version = "1.0.0"
[parameters]
signal_source = "open-meteo"
min_edge_bps = "150"
size = "1"
"#,
        )
        .unwrap();
        let strat = r.instantiate(&spec).unwrap();
        assert_eq!(strat.strategy_id(), "weather-temperature-arbitrage-live-v1");
    }

    #[test]
    fn unknown_external_edge_archetype_instantiates_config_only_runtime() {
        let r = default_registry();
        let spec = StrategySpecArtifact::from_toml_str(
            r#"
strategy_id = "flu-hospitalization-surge-v1"
name = "flu_hospitalization_surge"
version = "1.0.0"
[parameters]
signal_source = "public-health-nowcast"
min_edge_bps = "150"
size = "3"
[tags]
archetype = "external_edge"
"#,
        )
        .unwrap();
        let strat = r.instantiate(&spec).unwrap();
        assert_eq!(strat.strategy_id(), "flu-hospitalization-surge-v1");
    }

    #[test]
    fn unknown_name_returns_typed_error() {
        let r = default_registry();
        let mut spec = weather_spec();
        spec.name = "not_a_real_strategy".into();
        match r.instantiate(&spec) {
            Err(SpecError::UnknownStrategy { .. }) => {}
            Err(e) => panic!("wrong error variant: {e}"),
            Ok(_) => panic!("expected UnknownStrategy"),
        }
    }
}
