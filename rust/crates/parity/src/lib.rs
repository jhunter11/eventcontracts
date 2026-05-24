//! Cross-language parity harness boundary.

use eventcontracts_contracts::{FeatureVectorRecord, IntentEnvelopeRecord, NormalizedEventRecord};

#[derive(Clone, Debug)]
pub struct ParityCase {
    pub case_id: String,
    pub event: NormalizedEventRecord,
    pub feature_vector: Option<FeatureVectorRecord>,
    pub expected_decisions: Vec<IntentEnvelopeRecord>,
    pub tolerance_bps: String,
}

#[derive(Clone, Debug)]
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
    fn run_case(&self, case: &ParityCase) -> Result<ParityResult, ParityError>;
    fn run_all(&self, cases: &[ParityCase]) -> Result<Vec<ParityResult>, ParityError> {
        let mut results = Vec::with_capacity(cases.len());
        for case in cases {
            results.push(self.run_case(case)?);
        }
        Ok(results)
    }
}
