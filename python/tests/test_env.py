"""Local dotenv loading tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from eventcontracts.env import load_default_env, load_env_file


def test_load_env_file_sets_missing_values_without_overriding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        """
# comment
export EVENTCONTRACTS_EXAMPLE=from_file
EVENTCONTRACTS_QUOTED="keeps # hash"
EVENTCONTRACTS_INLINE=abc # comment
EVENTCONTRACTS_EXISTING=file_value
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("EVENTCONTRACTS_EXISTING", "process_value")

    loaded = load_env_file(env_path)

    assert loaded["EVENTCONTRACTS_EXAMPLE"] == "from_file"
    assert loaded["EVENTCONTRACTS_QUOTED"] == "keeps # hash"
    assert loaded["EVENTCONTRACTS_INLINE"] == "abc"
    assert loaded["EVENTCONTRACTS_EXISTING"] == "file_value"
    assert loaded["EVENTCONTRACTS_EXISTING"] != "process_value"


def test_load_default_env_walks_parent_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("EVENTCONTRACTS_PARENT_ENV=1\n", encoding="utf-8")
    child = tmp_path / "a" / "b"
    child.mkdir(parents=True)
    monkeypatch.chdir(child)
    monkeypatch.delenv("EVENTCONTRACTS_PARENT_ENV", raising=False)

    found = load_default_env()

    assert found == env_path
