//! Sleeve runner boundary.

use eventcontracts_contracts::{IntentEnvelopeRecord, NormalizedEventRecord};

#[derive(Clone, Debug)]
pub struct RunSummary {
    pub sleeve_id: String,
    pub strategy_id: String,
    pub events_processed: u64,
    pub decisions_emitted: u64,
    pub intents_dispatched: u64,
    pub intents_rejected: u64,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum RunnerError {
    EventSource(String),
    Strategy(String),
    Risk(String),
    Sink(String),
}

pub trait EventSource {
    fn next_event(&mut self) -> Result<Option<NormalizedEventRecord>, RunnerError>;
}

pub trait IntentSink {
    fn emit(&mut self, envelope: IntentEnvelopeRecord) -> Result<(), RunnerError>;
}

pub trait StrategyRuntime {
    fn on_event(
        &mut self,
        event: &NormalizedEventRecord,
    ) -> Result<Vec<IntentEnvelopeRecord>, RunnerError>;
}

pub trait RiskGate {
    fn evaluate(&self, envelope: &IntentEnvelopeRecord) -> Result<(), RunnerError>;
}

pub trait SleeveRunner {
    fn run(&mut self) -> Result<RunSummary, RunnerError>;
}
