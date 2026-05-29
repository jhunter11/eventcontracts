"""Immutable strategy/model artifact bundles.

Bundles are the promotion unit between Python research/paper workflows and the
Rust live runtime. They intentionally copy every runtime input into one local
directory, write checksums into `manifest.toml`, and keep promotion pointers
outside the immutable bundle contents.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eventcontracts.audit import AuditStamp, audit_stamp_for
from eventcontracts.config import load_sleeve_spec, load_strategy_spec
from eventcontracts.contracts import validate_toml_contract_file
from eventcontracts.domain.ids import FeatureSchemaId, ModelName, ModelVersion
from eventcontracts.domain.metadata import FrozenMap, freeze_mapping
from eventcontracts.domain.spec import SleeveSpec, StrategySpec
from eventcontracts.domain.validation import require_aware_datetime, require_non_empty
from eventcontracts.features.pipeline import FeatureDefinition, FeatureDType, FeatureSchema
from eventcontracts.models.pipeline import ModelArtifact

BUNDLE_MANIFEST_SCHEMA_VERSION = "1"
BUNDLE_AUDIT_SCHEMA_VERSION = "artifact-bundle-v1"


@dataclass(frozen=True)
class BundleFile:
    path: str
    sha256: str
    size_bytes: int

    def __post_init__(self) -> None:
        require_non_empty(self.path, "path")
        require_non_empty(self.sha256, "sha256")
        if len(self.sha256) != 64:
            raise ValueError("sha256 must be a hex sha256 digest")
        if self.size_bytes < 0:
            raise ValueError("size_bytes must be non-negative")


@dataclass(frozen=True)
class ParityCases:
    path: str
    sha256: str
    expected_rows: int

    def __post_init__(self) -> None:
        require_non_empty(self.path, "path")
        require_non_empty(self.sha256, "sha256")
        if len(self.sha256) != 64:
            raise ValueError("sha256 must be a hex sha256 digest")
        if self.expected_rows < 0:
            raise ValueError("expected_rows must be non-negative")


@dataclass(frozen=True)
class ArtifactBundle:
    bundle_id: str
    strategy: StrategySpec
    feature_schema: FeatureSchema
    files: tuple[BundleFile, ...]
    created_at: datetime
    manifest_path: str
    audit: AuditStamp
    root_path: str = ""
    sleeve: SleeveSpec | None = None
    model: ModelArtifact | None = None
    parity: ParityCases | None = None
    promoted: bool = False
    metadata: Mapping[str, Any] = field(default_factory=FrozenMap)

    def __post_init__(self) -> None:
        require_non_empty(self.bundle_id, "bundle_id")
        require_non_empty(self.manifest_path, "manifest_path")
        require_aware_datetime(self.created_at, "created_at")
        object.__setattr__(self, "files", tuple(self.files))
        object.__setattr__(self, "metadata", freeze_mapping(self.metadata))


class ArtifactBundleWriter:
    """Write immutable local strategy/model bundles."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def write_from_files(
        self,
        *,
        strategy_spec_path: str | Path,
        feature_schema_path: str | Path,
        sleeve_spec_path: str | Path | None = None,
        model_path: str | Path | None = None,
        parity_cases_path: str | Path | None = None,
        bundle_id: str | None = None,
        promoted: bool = True,
        created_by: str = "eventcontracts.bundle-writer",
        created_at: datetime | None = None,
    ) -> ArtifactBundle:
        """Copy source artifacts into a new immutable bundle directory."""

        strategy = load_strategy_spec(strategy_spec_path)
        feature_schema = load_feature_schema(feature_schema_path)
        sleeve = load_sleeve_spec(sleeve_spec_path) if sleeve_spec_path is not None else None
        created = created_at or datetime.now(UTC)
        require_aware_datetime(created, "created_at")
        resolved_bundle_id = bundle_id or f"{strategy.name}/{strategy.version}"
        bundle_root = self.root / _safe_bundle_dir(resolved_bundle_id)
        if bundle_root.exists():
            raise FileExistsError(f"artifact bundle already exists: {bundle_root}")
        bundle_root.mkdir(parents=True, exist_ok=False)

        copied: list[BundleFile] = []
        copied.append(_copy_file(Path(strategy_spec_path), bundle_root / "strategy_spec.toml"))
        copied.append(_copy_file(Path(feature_schema_path), bundle_root / "feature_schema.json"))
        if sleeve_spec_path is not None:
            copied.append(_copy_file(Path(sleeve_spec_path), bundle_root / "sleeve_spec.toml"))
        model: ModelArtifact | None = None
        if model_path is not None:
            model_file = _copy_file(Path(model_path), bundle_root / "model" / Path(model_path).name)
            copied.append(model_file)
            model = _model_artifact_from_file(strategy, model_file, created)
        parity: ParityCases | None = None
        if parity_cases_path is not None:
            parity_file = _copy_file(Path(parity_cases_path), bundle_root / "parity" / Path(parity_cases_path).name)
            copied.append(parity_file)
            parity = ParityCases(
                path=parity_file.path,
                sha256=parity_file.sha256,
                expected_rows=_count_non_empty_lines(Path(parity_cases_path)),
            )

        manifest = _manifest_document(
            bundle_id=resolved_bundle_id,
            created_at=created,
            created_by=created_by,
            strategy=strategy,
            feature_schema=feature_schema,
            feature_schema_file=_require_file(copied, "feature_schema.json"),
            sleeve=sleeve,
            model=model,
            parity=parity,
            files=tuple(copied),
            promoted=promoted,
        )
        manifest_path = bundle_root / "manifest.toml"
        manifest_path.write_text(_manifest_to_toml(manifest), encoding="utf-8")
        validate_toml_contract_file(manifest_path, "manifest.schema.json")

        manifest_doc = _load_manifest(manifest_path)
        audit = audit_stamp_for(
            manifest_doc,
            object_id=resolved_bundle_id,
            object_kind="artifact_bundle",
            schema_version=BUNDLE_AUDIT_SCHEMA_VERSION,
            produced_at=created,
            producer=created_by,
        )
        return ArtifactBundle(
            bundle_id=resolved_bundle_id,
            strategy=strategy,
            sleeve=sleeve,
            feature_schema=feature_schema,
            model=model,
            parity=parity,
            promoted=promoted,
            files=tuple(copied),
            created_at=created,
            manifest_path=str(manifest_path),
            root_path=str(bundle_root),
            audit=audit,
            metadata={"created_by": created_by},
        )

    def write_manifest(self, bundle: ArtifactBundle) -> BundleFile:
        root = _bundle_root_for_write(bundle, self.root)
        manifest_doc = _manifest_document(
            bundle_id=bundle.bundle_id,
            created_at=bundle.created_at,
            created_by=str(bundle.metadata.get("created_by", "eventcontracts.bundle-writer")),
            strategy=bundle.strategy,
            feature_schema=bundle.feature_schema,
            feature_schema_file=_require_file(bundle.files, "feature_schema.json"),
            sleeve=bundle.sleeve,
            model=bundle.model,
            parity=bundle.parity,
            files=bundle.files,
            promoted=bundle.promoted,
        )
        path = root / "manifest.toml"
        path.write_text(_manifest_to_toml(manifest_doc), encoding="utf-8")
        validate_toml_contract_file(path, "manifest.schema.json")
        return _bundle_file(path, root=root)

    def write_strategy_spec(self, spec: StrategySpec) -> BundleFile:
        path = self.root / "strategy_spec.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_strategy_spec_to_toml(spec), encoding="utf-8")
        return _bundle_file(path, root=self.root)

    def write_feature_schema(self, schema: FeatureSchema) -> BundleFile:
        path = self.root / "feature_schema.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(feature_schema_to_dict(schema), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return _bundle_file(path, root=self.root)

    def write_model(self, artifact: ModelArtifact) -> BundleFile:
        source = Path(artifact.uri)
        if not source.exists():
            raise FileNotFoundError(f"model artifact not found: {source}")
        return _copy_file(source, self.root / "model" / source.name)


class ArtifactBundleLoader:
    """Load immutable bundles for replay, paper, or live sleeves."""

    def load(self, uri: str | Path, *, require_promoted: bool = True) -> ArtifactBundle:
        root = Path(uri)
        manifest_path = root / "manifest.toml" if root.is_dir() else root
        validate_toml_contract_file(manifest_path, "manifest.schema.json")
        manifest = _load_manifest(manifest_path)
        promoted = bool(manifest.get("promoted", False))
        if require_promoted and not promoted:
            raise ValueError(f"artifact bundle is not promoted: {manifest_path}")
        bundle_root = manifest_path.parent

        strategy = load_strategy_spec(_bundle_child(bundle_root, manifest["strategy"]["strategy_spec"]))
        features = manifest["features"]
        feature_schema = load_feature_schema(_bundle_child(bundle_root, features["schema"]))
        sleeve = (
            load_sleeve_spec(_bundle_child(bundle_root, manifest["sleeve"]["sleeve_spec"]))
            if isinstance(manifest.get("sleeve"), dict)
            else None
        )
        model = _load_model(manifest, bundle_root)
        parity = _load_parity(manifest)
        created_at = _parse_datetime(str(manifest["created_at"]))
        files = tuple(
            BundleFile(
                path=str(item["path"]),
                sha256=str(item["sha256"]),
                size_bytes=int(item.get("size_bytes", _bundle_child(bundle_root, item["path"]).stat().st_size)),
            )
            for item in manifest["files"]
        )
        created_by = str(manifest.get("created_by", "unknown"))
        audit = audit_stamp_for(
            manifest,
            object_id=str(manifest["bundle_id"]),
            object_kind="artifact_bundle",
            schema_version=BUNDLE_AUDIT_SCHEMA_VERSION,
            produced_at=created_at,
            producer=created_by,
        )
        return ArtifactBundle(
            bundle_id=str(manifest["bundle_id"]),
            strategy=strategy,
            sleeve=sleeve,
            feature_schema=feature_schema,
            model=model,
            parity=parity,
            promoted=promoted,
            files=files,
            created_at=created_at,
            manifest_path=str(manifest_path),
            root_path=str(bundle_root),
            audit=audit,
            metadata={"created_by": created_by},
        )

    def list_versions(self, strategy_name: str) -> Sequence[str]:
        require_non_empty(strategy_name, "strategy_name")
        if not Path(strategy_name).name == strategy_name:
            raise ValueError("strategy_name must not contain path separators")
        if not Path(".").exists():
            return ()
        versions: list[str] = []
        for manifest_path in sorted(Path.cwd().rglob("manifest.toml")):
            try:
                manifest = _load_manifest(manifest_path)
            except (OSError, tomllib.TOMLDecodeError):
                continue
            strategy = manifest.get("strategy")
            if isinstance(strategy, dict) and strategy.get("name") == strategy_name:
                versions.append(str(strategy.get("version", "")))
        return tuple(version for version in versions if version)


class ArtifactBundleValidator:
    """Validate bundle integrity and runtime compatibility."""

    def validate_checksums(self, bundle: ArtifactBundle) -> None:
        root = Path(bundle.root_path or Path(bundle.manifest_path).parent).resolve()
        for file in bundle.files:
            path = _bundle_child(root, file.path)
            actual = sha256_file(path)
            if actual != file.sha256:
                raise ValueError(f"checksum mismatch for {file.path}: {actual} != {file.sha256}")
            actual_size = path.stat().st_size
            if actual_size != file.size_bytes:
                raise ValueError(f"size mismatch for {file.path}: {actual_size} != {file.size_bytes}")

    def validate_strategy_registered(self, bundle: ArtifactBundle) -> None:
        from eventcontracts.strategy.registry import ensure_registered

        archetype = str(
            bundle.strategy.tags.get("archetype")
            or bundle.strategy.parameters.get("archetype")
            or ""
        )
        if archetype in {"threshold", "external_edge", "model_edge", "scalper", "arb"}:
            return
        ensure_registered(bundle.strategy.name)

    def validate_parity_cases(self, bundle: ArtifactBundle) -> None:
        if bundle.parity is None:
            return
        path = _bundle_child(Path(bundle.root_path or Path(bundle.manifest_path).parent), bundle.parity.path)
        if bundle.parity.expected_rows == 0 and not path.exists():
            return
        if not path.exists():
            raise FileNotFoundError(f"parity cases not found: {path}")
        actual = sha256_file(path)
        if actual != bundle.parity.sha256:
            raise ValueError(f"parity checksum mismatch for {bundle.parity.path}: {actual} != {bundle.parity.sha256}")

    def validate_live_sleeve_has_parity(self, bundle: ArtifactBundle) -> None:
        if bundle.sleeve is None:
            return
        mode = str(bundle.sleeve.tags.get("mode", "paper")).lower()
        if mode == "paper":
            return
        if bundle.parity is None or bundle.parity.expected_rows <= 0:
            raise ValueError(
                f"live sleeve {bundle.sleeve.sleeve_id} requires non-empty parity cases"
            )

    def validate(self, bundle: ArtifactBundle) -> None:
        self.validate_checksums(bundle)
        self.validate_strategy_registered(bundle)
        self.validate_parity_cases(bundle)
        self.validate_live_sleeve_has_parity(bundle)


class PromotionRegistry:
    """Mutable promotion pointer outside immutable bundle contents."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def promote(self, bundle: ArtifactBundle, stage: str) -> None:
        require_non_empty(stage, "stage")
        ArtifactBundleValidator().validate(bundle)
        pointer = {
            "strategy_name": bundle.strategy.name,
            "strategy_version": bundle.strategy.version,
            "stage": stage,
            "bundle_id": bundle.bundle_id,
            "manifest_path": str(Path(bundle.manifest_path).resolve()),
            "promoted_at": datetime.now(UTC).isoformat(),
            "bundle_sha256": bundle.audit.canonical_sha256,
        }
        path = self._pointer_path(bundle.strategy.name, stage)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(pointer, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def current(self, strategy_name: str, stage: str) -> ArtifactBundle | None:
        path = self._pointer_path(strategy_name, stage)
        if not path.exists():
            return None
        pointer = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(pointer, dict):
            raise ValueError(f"invalid promotion pointer: {path}")
        return ArtifactBundleLoader().load(str(pointer["manifest_path"]))

    def _pointer_path(self, strategy_name: str, stage: str) -> Path:
        require_non_empty(strategy_name, "strategy_name")
        require_non_empty(stage, "stage")
        return self.root / _safe_bundle_dir(strategy_name) / f"{_safe_bundle_dir(stage)}.json"


def sha256_file(path: str | Path) -> str:
    hasher = hashlib.sha256()
    with Path(path).open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def load_feature_schema(path: str | Path) -> FeatureSchema:
    with Path(path).open("r", encoding="utf-8") as file:
        loaded = json.load(file)
    if not isinstance(loaded, dict):
        raise ValueError("feature schema must be a JSON object")
    features = tuple(
        FeatureDefinition(
            name=str(item["name"]),
            dtype=FeatureDType(str(item["dtype"])),
            nullable=bool(item.get("nullable", False)),
            default=item.get("default"),
            description=str(item.get("description", "")),
        )
        for item in loaded["features"]
    )
    return FeatureSchema(
        schema_id=FeatureSchemaId(str(loaded["schema_id"])),
        schema_version=str(loaded["schema_version"]),
        features=features,
        target_name=str(loaded["target_name"]) if loaded.get("target_name") is not None else None,
        target_horizon_seconds=(
            int(loaded["target_horizon_seconds"]) if loaded.get("target_horizon_seconds") is not None else None
        ),
    )


def feature_schema_to_dict(schema: FeatureSchema) -> dict[str, Any]:
    document: dict[str, Any] = {
        "schema_id": str(schema.schema_id),
        "schema_version": schema.schema_version,
        "features": [
            {
                "name": feature.name,
                "dtype": feature.dtype.value,
                "nullable": feature.nullable,
                "default": feature.default,
                "description": feature.description,
            }
            for feature in schema.features
        ],
    }
    if schema.target_name is not None:
        document["target_name"] = schema.target_name
    if schema.target_horizon_seconds is not None:
        document["target_horizon_seconds"] = schema.target_horizon_seconds
    return document


def _bundle_file(path: Path, *, root: Path) -> BundleFile:
    return BundleFile(
        path=path.relative_to(root).as_posix(),
        sha256=sha256_file(path),
        size_bytes=path.stat().st_size,
    )


def _copy_file(source: Path, target: Path) -> BundleFile:
    if not source.exists():
        raise FileNotFoundError(f"bundle source file not found: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    root = target.parent.parent if target.parent.name in {"model", "parity"} else target.parent
    return _bundle_file(target, root=root)


def _count_non_empty_lines(path: Path) -> int:
    with path.open("r", encoding="utf-8") as file:
        return sum(1 for line in file if line.strip())


def _bundle_child(root: Path, relative: str | Path) -> Path:
    root_resolved = root.resolve()
    child = (root_resolved / relative).resolve()
    if not child.is_relative_to(root_resolved):
        raise ValueError(f"bundle path escapes root: {relative}")
    return child


def _safe_bundle_dir(value: str) -> str:
    safe = value.replace("/", "__").replace("\\", "__").strip(".")
    require_non_empty(safe, "bundle path segment")
    return safe


def _require_file(files: Sequence[BundleFile], path: str) -> BundleFile:
    for file in files:
        if file.path == path:
            return file
    raise ValueError(f"bundle file missing: {path}")


def _load_manifest(path: Path) -> dict[str, Any]:
    with path.open("rb") as file:
        loaded = tomllib.load(file)
    if not isinstance(loaded, dict):
        raise ValueError("bundle manifest must be a TOML object")
    return loaded


def _load_model(manifest: Mapping[str, Any], bundle_root: Path) -> ModelArtifact | None:
    model_doc = manifest.get("model")
    if not isinstance(model_doc, dict) or model_doc.get("optional") is True and not model_doc.get("path"):
        return None
    path = _bundle_child(bundle_root, str(model_doc["path"]))
    expected_sha = str(model_doc["sha256"])
    actual_sha = sha256_file(path)
    if actual_sha != expected_sha:
        raise ValueError(f"model checksum mismatch for {path}: {actual_sha} != {expected_sha}")
    created_at = _parse_datetime(str(manifest["created_at"]))
    audit = audit_stamp_for(
        model_doc,
        object_id=f"{model_doc['name']}:{model_doc['version']}",
        object_kind="model_artifact",
        schema_version="model-artifact-v1",
        produced_at=created_at,
        producer=str(manifest.get("created_by", "unknown")),
    )
    return ModelArtifact(
        name=ModelName(str(model_doc["name"])),
        version=ModelVersion(str(model_doc["version"])),
        uri=str(path),
        sha256=str(model_doc["sha256"]),
        format=str(model_doc["format"]),
        created_at=created_at,
        audit=audit,
    )


def _load_parity(manifest: Mapping[str, Any]) -> ParityCases | None:
    parity_doc = manifest.get("parity")
    if not isinstance(parity_doc, dict):
        return None
    return ParityCases(
        path=str(parity_doc["cases"]),
        sha256=str(parity_doc["sha256"]),
        expected_rows=int(parity_doc["expected_rows"]),
    )


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require_aware_datetime(parsed, "datetime")
    return parsed


def _model_artifact_from_file(strategy: StrategySpec, file: BundleFile, created_at: datetime) -> ModelArtifact:
    model_ref = strategy.model
    name = model_ref.name if model_ref is not None else ModelName(strategy.name)
    version = model_ref.version if model_ref is not None else ModelVersion(strategy.version)
    audit = audit_stamp_for(
        {"path": file.path, "sha256": file.sha256},
        object_id=f"{name}:{version}",
        object_kind="model_artifact",
        schema_version="model-artifact-v1",
        produced_at=created_at,
        producer="eventcontracts.bundle-writer",
    )
    return ModelArtifact(
        name=name,
        version=version,
        uri=file.path,
        sha256=file.sha256,
        format=Path(file.path).suffix.lstrip(".") or "artifact",
        created_at=created_at,
        audit=audit,
    )


def _manifest_document(
    *,
    bundle_id: str,
    created_at: datetime,
    created_by: str,
    strategy: StrategySpec,
    feature_schema: FeatureSchema,
    feature_schema_file: BundleFile,
    sleeve: SleeveSpec | None,
    model: ModelArtifact | None,
    parity: ParityCases | None,
    files: Sequence[BundleFile],
    promoted: bool,
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "schema_version": BUNDLE_MANIFEST_SCHEMA_VERSION,
        "bundle_id": bundle_id,
        "created_at": created_at.isoformat(),
        "created_by": created_by,
        "promoted": promoted,
        "strategy": {
            "name": strategy.name,
            "version": strategy.version,
            "strategy_spec": "strategy_spec.toml",
        },
        "features": {
            "schema": feature_schema_file.path,
            "schema_id": str(feature_schema.schema_id),
            "schema_version": feature_schema.schema_version,
            "sha256": feature_schema_file.sha256,
        },
        "files": [
            {"path": file.path, "sha256": file.sha256, "size_bytes": file.size_bytes}
            for file in sorted(files, key=lambda item: item.path)
        ],
    }
    if sleeve is not None:
        document["sleeve"] = {"sleeve_spec": "sleeve_spec.toml"}
    if model is not None:
        document["model"] = {
            "name": str(model.name),
            "version": str(model.version),
            "format": model.format,
            "path": model.uri,
            "sha256": model.sha256,
            "optional": False,
        }
    else:
        document["model"] = {
            "name": strategy.name,
            "version": strategy.version,
            "format": "rules",
            "path": "",
            "sha256": "0" * 64,
            "optional": True,
        }
    if parity is not None:
        document["parity"] = {
            "cases": parity.path,
            "sha256": parity.sha256,
            "expected_rows": parity.expected_rows,
        }
    return document


def _manifest_to_toml(document: Mapping[str, Any]) -> str:
    lines = [
        f"schema_version = {_toml_scalar(document['schema_version'])}",
        f"bundle_id = {_toml_scalar(document['bundle_id'])}",
        f"created_at = {_toml_scalar(document['created_at'])}",
        f"created_by = {_toml_scalar(document.get('created_by', ''))}",
        f"promoted = {_toml_scalar(document.get('promoted', False))}",
        "",
        "[strategy]",
        *_toml_table(document["strategy"]),
        "",
        "[model]",
        *_toml_table(document["model"]),
        "",
        "[features]",
        *_toml_table(document["features"]),
    ]
    if "sleeve" in document:
        lines.extend(("", "[sleeve]", *_toml_table(document["sleeve"])))
    if "parity" in document:
        lines.extend(("", "[parity]", *_toml_table(document["parity"])))
    for file in document["files"]:
        lines.extend(("", "[[files]]", *_toml_table(file)))
    return "\n".join(lines) + "\n"


def _toml_table(values: Mapping[str, Any]) -> list[str]:
    return [f"{key} = {_toml_scalar(value)}" for key, value in values.items()]


def _toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    return json.dumps(str(value))


def _strategy_spec_to_toml(spec: StrategySpec) -> str:
    lines = [
        f"strategy_id = {_toml_scalar(str(spec.strategy_id))}",
        f"name = {_toml_scalar(spec.name)}",
        f"version = {_toml_scalar(spec.version)}",
        f"description = {_toml_scalar(spec.description)}",
    ]
    if spec.feature_schema_id is not None:
        lines.append(f"feature_schema_id = {_toml_scalar(str(spec.feature_schema_id))}")
    lines.extend(
        (
            "",
            "[subscription]",
            f"venues = {_toml_array([venue.value for venue in spec.subscription.venues])}",
            f"instrument_patterns = {_toml_array(spec.subscription.instrument_patterns)}",
            f"event_kinds = {_toml_array(spec.subscription.event_kinds)}",
            f"external_sources = {_toml_array(spec.subscription.external_sources)}",
            "",
            "[default_execution_priority]",
            f"tier = {_toml_scalar(spec.default_execution_priority.tier.value)}",
        )
    )
    if spec.parameters:
        lines.extend(("", "[parameters]"))
        lines.extend(f"{key} = {_toml_scalar(value)}" for key, value in sorted(spec.parameters.items()))
    if spec.tags:
        lines.extend(("", "[tags]"))
        lines.extend(f"{key} = {_toml_scalar(value)}" for key, value in sorted(spec.tags.items()))
    return "\n".join(lines) + "\n"


def _toml_array(values: Sequence[Any]) -> str:
    return "[" + ", ".join(_toml_scalar(value) for value in values) + "]"


def _bundle_root_for_write(bundle: ArtifactBundle, default_root: Path) -> Path:
    if bundle.root_path:
        return Path(bundle.root_path)
    return default_root
