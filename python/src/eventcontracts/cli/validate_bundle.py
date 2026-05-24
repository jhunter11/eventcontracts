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
import json
from pathlib import Path

from eventcontracts.config import load_sleeve_spec, load_strategy_spec


def register(subparsers: argparse._SubParsersAction) -> None:
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
    import tomllib

    data = tomllib.loads(path.read_text())
    for required_key in ("schema_version", "bundle_id", "strategy", "features", "files"):
        if required_key not in data:
            raise ValueError(f"missing top-level key: {required_key}")
    if data["schema_version"] != "1":
        raise ValueError(f"unsupported schema_version: {data['schema_version']}")


def _check_strategy_spec(path: Path) -> None:
    load_strategy_spec(path)


def _check_sleeve_spec(path: Path) -> None:
    load_sleeve_spec(path)


def _check_feature_schema(path: Path) -> None:
    data = json.loads(path.read_text())
    for required_key in ("schema_id", "schema_version", "features"):
        if required_key not in data:
            raise ValueError(f"missing top-level key: {required_key}")
    if not isinstance(data["features"], list) or not data["features"]:
        raise ValueError("features must be a non-empty list")
    for i, feature in enumerate(data["features"]):
        if "name" not in feature or "dtype" not in feature:
            raise ValueError(f"feature[{i}] missing name or dtype")
