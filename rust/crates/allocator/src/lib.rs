//! Capital allocator boundary.

use eventcontracts_contracts::AuditStamp;

#[derive(Clone, Debug)]
pub struct CapitalSnapshot {
    pub as_of: String,
    pub total_capital: String,
    pub allocated_capital: String,
    pub currency: String,
    pub audit: AuditStamp,
}

#[derive(Clone, Debug)]
pub struct SleeveSpecRecord {
    pub sleeve_id: String,
    pub strategy_id: String,
    pub venue: String,
    pub capital_allocation: String,
    pub currency: String,
}

#[derive(Clone, Debug)]
pub struct AllocationDecision {
    pub sleeve_id: String,
    pub target_capital: String,
    pub reason: String,
    pub audit: AuditStamp,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum AllocationError {
    InvalidCapital(String),
    UnknownSleeve(String),
    Policy(String),
}

pub trait Allocator {
    fn snapshot(&self) -> Result<CapitalSnapshot, AllocationError>;
    fn propose(
        &self,
        sleeves: &[SleeveSpecRecord],
    ) -> Result<Vec<AllocationDecision>, AllocationError>;
    fn apply(&mut self, decisions: &[AllocationDecision]) -> Result<(), AllocationError>;
}
