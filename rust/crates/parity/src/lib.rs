//! Cross-language parity harness boundary.

use eventcontracts_contracts::{FeatureVectorRecord, IntentEnvelopeRecord, NormalizedEventRecord};
use eventcontracts_runner::{StrategyContext, StrategyRuntime};
use serde::{Deserialize, Serialize};
use std::fs;
use std::path::Path;

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ParityCase {
    pub case_id: String,
    pub event: NormalizedEventRecord,
    pub feature_vector: Option<FeatureVectorRecord>,
    pub expected_decisions: Vec<IntentEnvelopeRecord>,
    pub tolerance_bps: String,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ParityResult {
    pub case_id: String,
    pub passed: bool,
    pub differences: Vec<String>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ParityError {
    MissingCase(String),
    UnsupportedSchema(String),
    Runtime(String),
}

pub trait ParityCaseLoader {
    fn load_cases(&self, uri: &str) -> Result<Vec<ParityCase>, ParityError>;
}

pub trait ParityRunner {
    /// Run a single parity case. Mutable because strategy runners maintain
    /// per-case state (last decision, rolling features, etc.).
    fn run_case(&mut self, case: &ParityCase) -> Result<ParityResult, ParityError>;
    fn run_all(&mut self, cases: &[ParityCase]) -> Result<Vec<ParityResult>, ParityError> {
        let mut results = Vec::with_capacity(cases.len());
        for case in cases {
            results.push(self.run_case(case)?);
        }
        Ok(results)
    }
}

pub struct JsonParityCaseLoader;

impl ParityCaseLoader for JsonParityCaseLoader {
    fn load_cases(&self, uri: &str) -> Result<Vec<ParityCase>, ParityError> {
        let path = Path::new(uri);
        if path.is_file() {
            return read_case(path).map(|case| vec![case]);
        }
        if !path.is_dir() {
            return Err(ParityError::MissingCase(uri.into()));
        }
        let mut cases = Vec::new();
        let entries = fs::read_dir(path).map_err(|e| ParityError::Runtime(e.to_string()))?;
        for entry in entries {
            let entry = entry.map_err(|e| ParityError::Runtime(e.to_string()))?;
            let path = entry.path();
            if path.extension().and_then(|v| v.to_str()) == Some("json") {
                cases.push(read_case(&path)?);
            }
        }
        cases.sort_by(|a, b| a.case_id.cmp(&b.case_id));
        Ok(cases)
    }
}

pub struct StrategyParityRunner<S: StrategyRuntime> {
    pub strategy: S,
    pub ctx: StrategyContext,
}

impl<S: StrategyRuntime> ParityRunner for StrategyParityRunner<S> {
    fn run_case(&mut self, case: &ParityCase) -> Result<ParityResult, ParityError> {
        run_strategy_case(&mut self.strategy, &self.ctx, case)
    }
}

pub struct DynStrategyParityRunner {
    pub strategy: Box<dyn StrategyRuntime>,
    pub ctx: StrategyContext,
}

impl ParityRunner for DynStrategyParityRunner {
    fn run_case(&mut self, case: &ParityCase) -> Result<ParityResult, ParityError> {
        run_strategy_case(self.strategy.as_mut(), &self.ctx, case)
    }
}

fn run_strategy_case(
    strategy: &mut dyn StrategyRuntime,
    ctx: &StrategyContext,
    case: &ParityCase,
) -> Result<ParityResult, ParityError> {
    let event = eventcontracts_runner::StrategyEvent::from_record(&case.event)
        .map_err(|e| ParityError::Runtime(e.to_string()))?;
    let decisions = strategy
        .on_event(&event, ctx)
        .map_err(|e| ParityError::Runtime(e.to_string()))?;
    let actual_json: Vec<String> = decisions
        .iter()
        .map(|decision| serde_json::to_string(decision).unwrap_or_default())
        .collect();
    let expected_json: Vec<String> = case
        .expected_decisions
        .iter()
        .map(|envelope| envelope.decision_json.clone())
        .collect();
    let mut differences = Vec::new();
    if actual_json != expected_json {
        differences.push(format!(
            "decisions differ: actual={actual_json:?} expected={expected_json:?}"
        ));
    }
    Ok(ParityResult {
        case_id: case.case_id.clone(),
        passed: differences.is_empty(),
        differences,
    })
}

fn read_case(path: &Path) -> Result<ParityCase, ParityError> {
    let raw = fs::read_to_string(path).map_err(|e| ParityError::Runtime(e.to_string()))?;
    serde_json::from_str(&raw).map_err(|e| ParityError::Runtime(e.to_string()))
}
