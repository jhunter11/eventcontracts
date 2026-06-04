"""Guardrails for stale or internally inconsistent research artifacts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from eventcontracts.research.ledger import files_manifest, stable_hash


@dataclass(frozen=True)
class ArtifactGuardResult:
    """Result of validating one generated research artifact."""

    ok: bool
    reasons: tuple[str, ...]
    input_manifest: Mapping[str, str]
    config_hash: str | None = None


def check_report_freshness(
    report_path: str | Path,
    input_paths: Sequence[str | Path],
    *,
    config_payload: Mapping[str, object] | None = None,
    recorded_input_manifest: Mapping[str, str] | None = None,
    recorded_config_hash: str | None = None,
) -> ArtifactGuardResult:
    """Validate report freshness and optional recorded hashes.

    This is intentionally narrow: it catches the common research failures where
    a report is older than its inputs, a config changed after the report, or a
    combined CSV/report still carries a stale manifest.
    """

    report = Path(report_path)
    reasons: list[str] = []
    if not report.exists():
        reasons.append(f"missing_report:{report}")
    manifest = files_manifest(input_paths)
    config_hash = stable_hash(config_payload) if config_payload is not None else None

    if report.exists():
        report_mtime = report.stat().st_mtime
        for input_path in input_paths:
            path = Path(input_path)
            if path.stat().st_mtime > report_mtime:
                reasons.append(f"input_newer_than_report:{path}")

    if recorded_input_manifest is not None and dict(recorded_input_manifest) != manifest:
        reasons.append("input_manifest_mismatch")
    if recorded_config_hash is not None and config_hash != recorded_config_hash:
        reasons.append("config_hash_mismatch")

    return ArtifactGuardResult(
        ok=not reasons,
        reasons=tuple(reasons),
        input_manifest=manifest,
        config_hash=config_hash,
    )


def assert_report_freshness(
    report_path: str | Path,
    input_paths: Sequence[str | Path],
    *,
    config_payload: Mapping[str, object] | None = None,
    recorded_input_manifest: Mapping[str, str] | None = None,
    recorded_config_hash: str | None = None,
) -> ArtifactGuardResult:
    """Raise when :func:`check_report_freshness` fails."""

    result = check_report_freshness(
        report_path,
        input_paths,
        config_payload=config_payload,
        recorded_input_manifest=recorded_input_manifest,
        recorded_config_hash=recorded_config_hash,
    )
    if not result.ok:
        raise ValueError(";".join(result.reasons))
    return result


def report_identity(
    *,
    input_paths: Sequence[str | Path],
    config_payload: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Manifest payload that should be written into generated reports."""

    return {
        "input_manifest": files_manifest(input_paths),
        "config_hash": stable_hash(config_payload) if config_payload is not None else None,
        "input_hash": stable_hash(files_manifest(input_paths)),
        "report_schema_version": "research-artifact-identity-v1",
    }
