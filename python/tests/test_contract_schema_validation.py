"""Contract schema validation tests."""

from __future__ import annotations

import pytest

from eventcontracts.contracts import (
    ContractValidationError,
    load_json_contract,
    validate_contract,
    validate_json_contract_file,
    validate_toml_contract_file,
)
from eventcontracts.domain.events import EVENT_KINDS
from eventcontracts.features import FeatureDType
from tests.conftest import REPO_ROOT

CONTRACTS = REPO_ROOT / "contracts"


def test_weather_threshold_bundle_files_match_contract_schemas() -> None:
    bundle = CONTRACTS / "examples/weather_threshold"

    validate_toml_contract_file(bundle / "manifest.toml", "manifest.schema.json")
    validate_toml_contract_file(bundle / "strategy_spec.toml", "strategy_spec.schema.json")
    validate_toml_contract_file(bundle / "sleeve_spec.toml", "sleeve_spec.schema.json")
    validate_json_contract_file(bundle / "feature_schema.json", "feature_schema.schema.json")


def test_strategy_spec_schema_event_kinds_match_domain_contract() -> None:
    schema = load_json_contract(CONTRACTS / "schemas/strategy_spec.schema.json")
    event_kind_schema = schema["$defs"]["event_subscription"]["properties"]["event_kinds"][
        "items"
    ]

    assert tuple(event_kind_schema["enum"]) == EVENT_KINDS


def test_feature_schema_dtype_enum_matches_python_contract() -> None:
    schema = load_json_contract(CONTRACTS / "schemas/feature_schema.schema.json")
    dtype_schema = schema["$defs"]["feature"]["properties"]["dtype"]

    assert tuple(dtype_schema["enum"]) == tuple(dtype.value for dtype in FeatureDType)


def test_feature_schema_rejects_unknown_dtype() -> None:
    schema = load_json_contract(CONTRACTS / "schemas/feature_schema.schema.json")
    document = {
        "schema_id": "bad_features",
        "schema_version": "1",
        "features": [{"name": "bad", "dtype": "float128"}],
    }

    with pytest.raises(ContractValidationError, match="expected one of"):
        validate_contract(document, schema)
