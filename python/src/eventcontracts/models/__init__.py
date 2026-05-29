"""Model inference, training, export, and registry contracts."""

from eventcontracts.models.artifacts import (
    canonical_dumps,
    load_artifact,
    sha256_of_payload,
    write_artifact,
)
from eventcontracts.models.dataset import TrainingExample, TrainingExampleBuilder
from eventcontracts.models.evaluation import (
    CalibrationBin,
    ClassificationMetrics,
    RegressionMetrics,
    evaluate_classification,
    evaluate_regression,
)
from eventcontracts.models.labelers import (
    BinaryProfitableAfterFeesLabeler,
    Labeler,
    NextMidChangeBpsLabeler,
    SettlementProbabilityLabeler,
)
from eventcontracts.models.models import (
    LinearRegressionModel,
    LogisticRegressionModel,
    ModelKind,
    TrainableModel,
    TrainingMetrics,
    load_model,
)
from eventcontracts.models.onnx_export import (
    ModelFamily,
    ModelTask,
    OnnxExport,
    OnnxExportParityError,
    export_model_onnx,
    predict_onnx,
    read_metadata,
    verify_export_parity,
)
from eventcontracts.models.parity import write_parity_cases
from eventcontracts.models.pipeline import (
    ModelArtifact,
    ModelExporter,
    ModelRegistry,
    ModelRunner,
    TrainingDataset,
    TrainingRun,
)
from eventcontracts.models.registry import (
    InMemoryModelRegistry,
    LocalFileModelRegistry,
)
from eventcontracts.models.runner import InProcessModelRunner
from eventcontracts.models.trainer import (
    ChronologicalSplit,
    ModelTrainer,
    TrainerConfig,
    TrainingResult,
)

__all__ = [
    "BinaryProfitableAfterFeesLabeler",
    "CalibrationBin",
    "ChronologicalSplit",
    "ClassificationMetrics",
    "InMemoryModelRegistry",
    "InProcessModelRunner",
    "Labeler",
    "LinearRegressionModel",
    "LocalFileModelRegistry",
    "LogisticRegressionModel",
    "ModelArtifact",
    "ModelExporter",
    "ModelFamily",
    "ModelKind",
    "ModelRegistry",
    "ModelRunner",
    "ModelTask",
    "ModelTrainer",
    "NextMidChangeBpsLabeler",
    "OnnxExport",
    "OnnxExportParityError",
    "RegressionMetrics",
    "SettlementProbabilityLabeler",
    "TrainableModel",
    "TrainerConfig",
    "TrainingDataset",
    "TrainingExample",
    "TrainingExampleBuilder",
    "TrainingMetrics",
    "TrainingResult",
    "TrainingRun",
    "canonical_dumps",
    "evaluate_classification",
    "evaluate_regression",
    "export_model_onnx",
    "load_artifact",
    "load_model",
    "predict_onnx",
    "read_metadata",
    "sha256_of_payload",
    "verify_export_parity",
    "write_artifact",
    "write_parity_cases",
]
