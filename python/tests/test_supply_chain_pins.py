"""Supply-chain pinning and operator runbook guards."""

from __future__ import annotations

import re

from tests.conftest import REPO_ROOT


def test_github_actions_are_pinned_to_full_shas() -> None:
    workflow = (REPO_ROOT / ".github/workflows/quality.yml").read_text(encoding="utf-8")
    uses = re.findall(r"uses:\s*([^\s]+)", workflow)

    assert uses
    assert all(re.search(r"@[0-9a-f]{40}$", item) for item in uses)


def test_docker_base_images_are_pinned_to_digests() -> None:
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    from_lines = [line for line in dockerfile.splitlines() if line.startswith("FROM ")]

    assert from_lines
    assert all("@sha256:" in line for line in from_lines)


def test_cargo_audit_config_and_credential_rotation_runbook_exist() -> None:
    assert (REPO_ROOT / "rust/audit.toml").is_file()
    assert (REPO_ROOT / "docs/runbooks/credential-rotation.md").is_file()
