from __future__ import annotations

import time
from pathlib import Path

import pytest

from eventcontracts.research.artifact_guard import (
    assert_report_freshness,
    check_report_freshness,
    report_identity,
)


def test_report_identity_contains_hashes(tmp_path: Path) -> None:
    data = tmp_path / "data.csv"
    data.write_text("x\n1\n", encoding="utf-8")

    ident = report_identity(input_paths=[data], config_payload={"edge": 100})

    assert ident["config_hash"]
    assert ident["input_hash"]
    input_manifest = ident["input_manifest"]
    assert isinstance(input_manifest, dict)
    assert str(data) in input_manifest


def test_report_freshness_detects_stale_report(tmp_path: Path) -> None:
    data = tmp_path / "data.csv"
    report = tmp_path / "report.json"
    report.write_text("{}", encoding="utf-8")
    time.sleep(0.01)
    data.write_text("x\n1\n", encoding="utf-8")

    result = check_report_freshness(report, [data])

    assert not result.ok
    assert any(reason.startswith("input_newer_than_report") for reason in result.reasons)


def test_report_freshness_detects_manifest_mismatch(tmp_path: Path) -> None:
    data = tmp_path / "data.csv"
    data.write_text("x\n1\n", encoding="utf-8")
    report = tmp_path / "report.json"
    report.write_text("{}", encoding="utf-8")

    result = check_report_freshness(
        report,
        [data],
        config_payload={"edge": 100},
        recorded_input_manifest={str(data): "wrong"},
        recorded_config_hash="wrong",
    )

    assert not result.ok
    assert "input_manifest_mismatch" in result.reasons
    assert "config_hash_mismatch" in result.reasons
    with pytest.raises(ValueError):
        assert_report_freshness(
            report,
            [data],
            config_payload={"edge": 100},
            recorded_input_manifest={str(data): "wrong"},
        )
