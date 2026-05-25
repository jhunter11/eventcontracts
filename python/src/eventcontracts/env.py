"""Small `.env` loader for local CLI workflows.

The project intentionally avoids a runtime dependency on python-dotenv. This
module supports the subset we need for operator setup: `KEY=value`,
`export KEY=value`, comments, and single/double-quoted values.
"""

from __future__ import annotations

import os
from pathlib import Path


def load_env_file(path: str | Path, *, override: bool = False) -> dict[str, str]:
    """Load environment variables from a dotenv-style file.

    Existing process variables are preserved unless `override=True`.
    Returns only variables read from the file, regardless of whether they were
    written into `os.environ`.
    """

    resolved = Path(path).expanduser()
    loaded: dict[str, str] = {}
    if not resolved.exists():
        return loaded
    for raw_line in resolved.read_text(encoding="utf-8").splitlines():
        parsed = _parse_env_line(raw_line)
        if parsed is None:
            continue
        key, value = parsed
        loaded[key] = value
        if override or key not in os.environ:
            os.environ[key] = value
    return loaded


def load_default_env(*, start: str | Path | None = None, override: bool = False) -> Path | None:
    """Find and load the nearest `.env` at or above `start`.

    CLI commands may be launched from the repo root or from `python/`. Walking
    parents makes both cases work without requiring `source .env`.
    """

    current = Path(start or Path.cwd()).expanduser().resolve()
    if current.is_file():
        current = current.parent
    for directory in (current, *current.parents):
        candidate = directory / ".env"
        if candidate.exists():
            load_env_file(candidate, override=override)
            return candidate
    return None


def _parse_env_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export "):
        stripped = stripped[len("export ") :].strip()
    key, sep, value = stripped.partition("=")
    if not sep:
        return None
    key = key.strip()
    value = value.strip()
    if not key or not key.replace("_", "").isalnum() or key[0].isdigit():
        return None
    if value.startswith(("'", '"')):
        return key, _strip_quotes(value)
    return key, _strip_inline_comment(value)


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _strip_inline_comment(value: str) -> str:
    in_single = False
    in_double = False
    for index, char in enumerate(value):
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double:
            prefix = value[:index]
            if not prefix or prefix[-1].isspace():
                return prefix.rstrip()
    return value
