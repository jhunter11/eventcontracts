"""`eventcontracts validate-bundle` — check a bundle against contracts/.

A valid bundle directory contains:

- manifest.toml      (matches contracts/schemas/manifest.schema.json)
- strategy_spec.toml (matches contracts/schemas/strategy_spec.schema.json)
- sleeve_spec.toml   (matches contracts/schemas/sleeve_spec.schema.json)
- feature_schema.json (matches contracts/schemas/feature_schema.schema.json)

The validator uses the same Pydantic config loaders the framework
uses at runtime, so a bundle that loads here is guaranteed to load
inside the runner.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
from typing import Any

from eventcontracts.config import load_sleeve_spec, load_strategy_spec
from eventcontracts.contracts import (
    validate_json_contract_file,
    validate_toml_contract_file,
)

ZERO_SHA256 = "0" * 64


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "validate-bundle",
        help="Validate an artifact bundle directory against contracts/.",
    )
    parser.add_argument("path", type=Path, help="Bundle directory")
    parser.set_defaults(handler=_handle)


def _handle(args: argparse.Namespace) -> int:
    bundle = args.path
    issues: list[str] = []

    if not bundle.is_dir():
        print(f"error: {bundle} is not a directory")
        return 2

    required = [
        ("manifest.toml", _check_manifest),
        ("strategy_spec.toml", _check_strategy_spec),
        ("sleeve_spec.toml", _check_sleeve_spec),
        ("feature_schema.json", _check_feature_schema),
    ]

    for filename, checker in required:
        path = bundle / filename
        if not path.exists():
            issues.append(f"missing: {filename}")
            continue
        try:
            checker(path)
        except Exception as exc:  # noqa: BLE001
            issues.append(f"{filename}: {exc}")

    if issues:
        for issue in issues:
            print(f"FAIL {issue}")
        return 1

    print(f"OK {bundle}")
    return 0


def _check_manifest(path: Path) -> None:
    data = validate_toml_contract_file(path, "manifest.schema.json")
    _validate_manifest_references(path.parent, data)


def _check_strategy_spec(path: Path) -> None:
    validate_toml_contract_file(path, "strategy_spec.schema.json")
    load_strategy_spec(path)


def _check_sleeve_spec(path: Path) -> None:
    validate_toml_contract_file(path, "sleeve_spec.schema.json")
    load_sleeve_spec(path)


def _check_feature_schema(path: Path) -> None:
    validate_json_contract_file(path, "feature_schema.schema.json")


def _validate_manifest_references(bundle: Path, manifest: dict[str, Any]) -> None:
    for entry in manifest["files"]:
        _validate_file_reference(bundle, entry["path"], entry["sha256"])

    feature_path = manifest["features"]["schema"]
    _validate_file_reference(bundle, feature_path, manifest["features"]["sha256"])

    model = manifest.get("model")
    if isinstance(model, dict) and model.get("path"):
        _validate_file_reference(bundle, model["path"], model.get("sha256", ""))

    parity = manifest.get("parity")
    if isinstance(parity, dict) and int(parity.get("expected_rows", 0)) > 0:
        _validate_file_reference(bundle, parity["cases"], parity["sha256"])


def _validate_file_reference(bundle: Path, relative_path: str, expected_sha256: str) -> None:
    target = (bundle / relative_path).resolve()
    if not _is_relative_to(target, bundle.resolve()):
        raise ValueError(f"file reference escapes bundle: {relative_path}")
    if not target.exists():
        raise ValueError(f"referenced file missing: {relative_path}")
    if relative_path == "manifest.toml":
        return
    if expected_sha256 and expected_sha256 != ZERO_SHA256:
        actual = _sha256_file(target)
        if actual != expected_sha256:
            raise ValueError(
                f"checksum mismatch for {relative_path}: expected {expected_sha256}, got {actual}"
            )


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
