"""Dependency-free validation for the repository's JSON schema contracts.

The full JSON Schema standard is intentionally larger than what this project
needs at the scaffold stage. This module implements the subset currently used
under `contracts/schemas/` so validation can run in clean environments without
pulling another runtime dependency into the research loop.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from eventcontracts.domain.serialization import to_primitive

JsonObject = Mapping[str, Any]


class ContractValidationError(ValueError):
    """Raised when a document does not match a contract schema."""


def find_contracts_dir(start: Path | None = None) -> Path:
    """Locate the top-level `contracts/` directory."""

    candidates: list[Path] = []
    if start is not None:
        candidates.extend(parent / "contracts" for parent in (start, *start.parents))
    module_path = Path(__file__).resolve()
    candidates.append(module_path.parents[4] / "contracts")
    candidates.append(Path.cwd() / "contracts")

    for candidate in candidates:
        if (candidate / "schemas").is_dir():
            return candidate
    raise FileNotFoundError("contracts directory not found")


def load_json_contract(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as file:
        loaded = json.load(file)
    if not isinstance(loaded, dict):
        raise ContractValidationError(f"{path}: schema document must be an object")
    return cast(dict[str, Any], loaded)


def load_toml_contract(path: str | Path) -> dict[str, Any]:
    with Path(path).open("rb") as file:
        loaded = tomllib.load(file)
    primitive = to_primitive(loaded)
    if not isinstance(primitive, dict):
        raise ContractValidationError(f"{path}: TOML document must be an object")
    return cast(dict[str, Any], primitive)


def validate_json_contract_file(
    document_path: str | Path,
    schema_name: str,
    *,
    contracts_dir: str | Path | None = None,
) -> None:
    path = Path(document_path)
    with path.open("r", encoding="utf-8") as file:
        document = json.load(file)
    schema = _load_schema(schema_name, path, contracts_dir)
    validate_contract(document, schema, document_name=path.name)


def validate_toml_contract_file(
    document_path: str | Path,
    schema_name: str,
    *,
    contracts_dir: str | Path | None = None,
) -> dict[str, Any]:
    path = Path(document_path)
    document = load_toml_contract(path)
    schema = _load_schema(schema_name, path, contracts_dir)
    validate_contract(document, schema, document_name=path.name)
    return document


def validate_contract(
    document: Any,
    schema: JsonObject,
    *,
    document_name: str = "document",
) -> None:
    """Validate `document` against the subset used by `contracts/schemas`."""

    _validate(document, schema, schema, document_name)


def _load_schema(
    schema_name: str,
    document_path: Path,
    contracts_dir: str | Path | None,
) -> dict[str, Any]:
    root = Path(contracts_dir) if contracts_dir is not None else find_contracts_dir(document_path)
    return load_json_contract(root / "schemas" / schema_name)


def _validate(value: Any, schema: JsonObject, root: JsonObject, path: str) -> None:
    if "$ref" in schema:
        _validate(value, _resolve_ref(str(schema["$ref"]), root), root, path)
        return

    if "oneOf" in schema:
        _validate_one_of(value, schema["oneOf"], root, path)
        return

    expected_type = schema.get("type")
    if expected_type is not None:
        _validate_type(value, expected_type, path)

    if "const" in schema and value != schema["const"]:
        raise ContractValidationError(f"{path}: expected {schema['const']!r}")

    if "enum" in schema and value not in schema["enum"]:
        allowed = ", ".join(repr(item) for item in schema["enum"])
        raise ContractValidationError(f"{path}: expected one of {allowed}")

    if isinstance(value, str):
        _validate_string(value, schema, path)

    if _is_number(value):
        minimum = schema.get("minimum")
        if minimum is not None and value < minimum:
            raise ContractValidationError(f"{path}: must be >= {minimum}")

    if isinstance(value, list):
        min_items = schema.get("minItems")
        if min_items is not None and len(value) < min_items:
            raise ContractValidationError(f"{path}: expected at least {min_items} items")
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for index, item in enumerate(value):
                _validate(item, item_schema, root, f"{path}[{index}]")

    if isinstance(value, dict):
        _validate_object(value, schema, root, path)


def _validate_one_of(
    value: Any,
    options: Any,
    root: JsonObject,
    path: str,
) -> None:
    if not isinstance(options, Sequence):
        raise ContractValidationError(f"{path}: invalid oneOf schema")

    matches = 0
    messages: list[str] = []
    for option in options:
        if not isinstance(option, Mapping):
            raise ContractValidationError(f"{path}: invalid oneOf option")
        try:
            _validate(value, option, root, path)
        except ContractValidationError as exc:
            messages.append(str(exc))
        else:
            matches += 1

    if matches != 1:
        detail = "; ".join(messages[:3])
        raise ContractValidationError(
            f"{path}: expected exactly one matching schema, got {matches}"
            + (f" ({detail})" if detail else "")
        )


def _validate_type(value: Any, expected_type: Any, path: str) -> None:
    expected = (expected_type,) if isinstance(expected_type, str) else tuple(expected_type)
    if not any(_matches_type(value, item) for item in expected):
        readable = " or ".join(str(item) for item in expected)
        raise ContractValidationError(f"{path}: expected {readable}, got {type(value).__name__}")


def _matches_type(value: Any, expected_type: str) -> bool:
    match expected_type:
        case "null":
            return value is None
        case "boolean":
            return isinstance(value, bool)
        case "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        case "number":
            return _is_number(value)
        case "string":
            return isinstance(value, str)
        case "array":
            return isinstance(value, list)
        case "object":
            return isinstance(value, dict)
        case _:
            raise ContractValidationError(f"unsupported schema type: {expected_type}")


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _validate_string(value: str, schema: JsonObject, path: str) -> None:
    min_length = schema.get("minLength")
    if min_length is not None and len(value) < min_length:
        raise ContractValidationError(f"{path}: expected length >= {min_length}")

    max_length = schema.get("maxLength")
    if max_length is not None and len(value) > max_length:
        raise ContractValidationError(f"{path}: expected length <= {max_length}")

    pattern = schema.get("pattern")
    if isinstance(pattern, str) and re.fullmatch(pattern, value) is None:
        raise ContractValidationError(f"{path}: does not match pattern {pattern!r}")

    if schema.get("format") == "date-time":
        _validate_datetime(value, path)


def _validate_datetime(value: str, path: str) -> None:
    parsed_value = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(parsed_value)
    except ValueError as exc:
        raise ContractValidationError(f"{path}: invalid date-time") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractValidationError(f"{path}: date-time must include timezone")


def _validate_object(
    value: dict[str, Any],
    schema: JsonObject,
    root: JsonObject,
    path: str,
) -> None:
    required = schema.get("required", ())
    for key in required:
        if key not in value:
            raise ContractValidationError(f"{path}.{key}: missing required property")

    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        properties = {}

    additional = schema.get("additionalProperties", True)
    if additional is False:
        allowed = set(properties)
        unexpected = sorted(key for key in value if key not in allowed)
        if unexpected:
            raise ContractValidationError(
                f"{path}: unexpected propert{'y' if len(unexpected) == 1 else 'ies'} "
                + ", ".join(unexpected)
            )

    for key, child in value.items():
        child_path = f"{path}.{key}"
        if key in properties:
            _validate(child, cast(JsonObject, properties[key]), root, child_path)
        elif isinstance(additional, Mapping):
            _validate(child, additional, root, child_path)


def _resolve_ref(ref: str, root: JsonObject) -> JsonObject:
    if not ref.startswith("#/"):
        raise ContractValidationError(f"unsupported non-local ref: {ref}")
    current: Any = root
    for part in ref[2:].split("/"):
        if not isinstance(current, Mapping) or part not in current:
            raise ContractValidationError(f"unresolved schema ref: {ref}")
        current = current[part]
    if not isinstance(current, Mapping):
        raise ContractValidationError(f"schema ref does not resolve to an object: {ref}")
    return current
