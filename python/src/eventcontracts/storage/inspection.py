"""Inspection helpers for local event-lake partitions."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from eventcontracts.domain.events import event_kind
from eventcontracts.storage.parquet_store import ParquetEventStore


def inspect_event_lake(root: Path | str, *, source: str = "*") -> dict[str, Any]:
    """Return machine-readable counts and partition health for an event lake."""

    root_path = Path(root)
    store = ParquetEventStore(root_path)
    raw = list(store.read(source=source))
    normalized = [event for event in store.read_normalized() if source == "*" or event.provenance.source == source]
    rejects = list(store.read_normalization_rejects(source=source))
    received_bounds = _datetime_bounds([event.received_at for event in raw])

    return {
        "root": str(root_path),
        "source": source,
        "raw_count": len(raw),
        "normalized_count": len(normalized),
        "reject_count": len(rejects),
        "raw_by_venue": dict(sorted(Counter(event.venue.value if event.venue else "unknown" for event in raw).items())),
        "raw_by_source": dict(sorted(Counter(event.source for event in raw).items())),
        "raw_by_channel": dict(sorted(Counter(event.channel for event in raw).items())),
        "normalized_by_kind": dict(sorted(Counter(event_kind(event) for event in normalized).items())),
        "normalized_by_source": dict(sorted(Counter(event.provenance.source for event in normalized).items())),
        "rejects_by_channel": dict(sorted(Counter(reject.raw.channel for reject in rejects).items())),
        "rejects_by_reason": dict(sorted(Counter(reason for reject in rejects for reason in reject.reasons).items())),
        "first_received_at": received_bounds[0].isoformat() if received_bounds[0] else None,
        "last_received_at": received_bounds[1].isoformat() if received_bounds[1] else None,
        "partition_files": {
            "raw": _parquet_file_count(root_path / "raw"),
            "normalized": _parquet_file_count(root_path / "normalized"),
            "normalization_rejects": _parquet_file_count(root_path / "normalization_rejects"),
            "manifests": (
                len(tuple((root_path / "manifests").glob("*.json"))) if (root_path / "manifests").exists() else 0
            ),
        },
    }


def _datetime_bounds(values: Iterable[datetime]) -> tuple[datetime | None, datetime | None]:
    ordered = sorted(values)
    if not ordered:
        return None, None
    return ordered[0], ordered[-1]


def _parquet_file_count(root: Path) -> int:
    if not root.exists():
        return 0
    return len(tuple(root.rglob("*.parquet")))
