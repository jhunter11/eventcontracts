//! Online feature-building boundary.

use eventcontracts_contracts::{AuditStamp, FeatureVectorRecord, NormalizedEventRecord};

#[derive(Clone, Debug)]
pub struct FeatureDefinition {
    pub name: String,
    pub dtype: String,
    pub nullable: bool,
    pub default: Option<String>,
}

#[derive(Clone, Debug)]
pub struct FeatureSchema {
    pub schema_id: String,
    pub schema_version: String,
    pub features: Vec<FeatureDefinition>,
}

#[derive(Clone, Debug)]
pub struct OnlineFeatureState {
    pub schema: FeatureSchema,
    pub last_event_id: Option<String>,
    pub vector: Option<FeatureVectorRecord>,
    pub audit: Option<AuditStamp>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum FeatureError {
    MissingFeature(String),
    UnsupportedEvent(String),
    StateMismatch(String),
}

pub trait FeatureBuilder {
    fn schema(&self) -> &FeatureSchema;
    fn warmup(&self, events: &[NormalizedEventRecord]) -> Result<OnlineFeatureState, FeatureError>;
    fn update(
        &self,
        state: OnlineFeatureState,
        event: &NormalizedEventRecord,
    ) -> Result<OnlineFeatureState, FeatureError>;
}
