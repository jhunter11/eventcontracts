"""JSONL ledger helpers for research and paper validation."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from eventcontracts.domain.metadata import FrozenMap, thaw_value


def to_jsonable(value: Any) -> Any:  # noqa: ANN401 - recursive JSON conversion
    """Convert framework dataclasses and scalar types into JSON-friendly values."""

    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {field.name: to_jsonable(getattr(value, field.name)) for field in dataclasses.fields(value)}
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, FrozenMap):
        return to_jsonable(thaw_value(value))
    if isinstance(value, Mapping):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, tuple | list):
        return [to_jsonable(v) for v in value]
    if isinstance(value, set | frozenset):
        return [to_jsonable(v) for v in sorted(value, key=repr)]
    return value


def stable_json_dumps(value: Any) -> str:  # noqa: ANN401 - accepts any serializable payload
    """Canonical JSON string used for hashing and JSONL writes."""

    return json.dumps(to_jsonable(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def stable_hash(value: Any) -> str:  # noqa: ANN401 - accepts any serializable payload
    """SHA-256 hash of a canonical JSON payload."""

    return hashlib.sha256(stable_json_dumps(value).encode("utf-8")).hexdigest()


def append_jsonl(path: str | Path, row: Any) -> None:  # noqa: ANN401 - accepts dataclasses or mappings
    """Append one JSON row to a ledger, creating the parent directory."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(to_jsonable(row), sort_keys=True, ensure_ascii=True) + "\n")


def write_jsonl(path: str | Path, rows: Iterable[Any]) -> None:  # noqa: ANN401 - accepts dataclasses or mappings
    """Replace a JSONL file with the given rows."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(to_jsonable(row), sort_keys=True, ensure_ascii=True) + "\n")


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    """Read a JSONL ledger into dictionaries."""

    out: list[dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict):
            raise ValueError(f"ledger row is not an object: {payload!r}")
        out.append(payload)
    return out


def file_sha256(path: str | Path) -> str:
    """SHA-256 hash of a file's raw bytes."""

    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def files_manifest(paths: Iterable[str | Path]) -> dict[str, str]:
    """Return a stable path -> sha256 manifest for existing files."""

    manifest: dict[str, str] = {}
    for p in paths:
        path = Path(p)
        if not path.exists():
            raise FileNotFoundError(path)
        manifest[str(path)] = file_sha256(path)
    return dict(sorted(manifest.items()))
