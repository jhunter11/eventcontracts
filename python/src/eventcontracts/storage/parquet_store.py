"""Parquet-backed event storage.

Raw and normalized events both land here. The layout is::

    <root>/raw/venue=<venue>/source=<source>/date=YYYY-MM-DD/part-<uuid>.parquet
    <root>/normalized/kind=<kind>/date=YYYY-MM-DD/part-<uuid>.parquet

Each ``append*`` call buffers in memory; :meth:`flush` writes a new
``part-<uuid>.parquet`` file under the appropriate partition directory.
Readers use :class:`ParquetEventStore.read` (or the DuckDB store) to
re-materialize the events in deterministic order.

The schema is the canonical JSON form of the envelope / event so that
both Python and Rust can read identical bytes.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable, Iterator, Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from heapq import merge
from pathlib import Path
from typing import Any
from uuid import uuid4

import pyarrow as _pa_mod
import pyarrow.parquet as _pq_mod

from eventcontracts.domain.events import (
    EVENT_KINDS,
    NormalizedEvent,
    event_kind,
)
from eventcontracts.domain.models import Venue
from eventcontracts.domain.serialization import to_primitive
from eventcontracts.domain.validation import require_not_future_datetime
from eventcontracts.storage.interfaces import (
    EventEnvelope,
    EventStore,
    NormalizationReject,
    NormalizationRejectStore,
    NormalizedEventStore,
)
from eventcontracts.storage.sorting import (
    envelope_sort_key,
    normalization_reject_sort_key,
    normalized_event_sort_key,
)

# pyarrow does not ship strict type stubs; treat the modules as Any so the
# rest of this file stays strict-mypy clean without scattering type:ignore.
pa: Any = _pa_mod
pq: Any = _pq_mod

PARQUET_SCHEMA_METADATA_KEY = b"eventcontracts.schema_version"
RAW_PARQUET_SCHEMA_VERSION = 1
NORMALIZED_PARQUET_SCHEMA_VERSION = 1
REJECT_PARQUET_SCHEMA_VERSION = 1
PRIVATE_CHANNEL_HINTS = frozenset(
    {
        "fill",
        "fills",
        "order",
        "orders",
        "own_fill",
        "own_order",
        "own_order_update",
        "own_order_reject",
    }
)
SENSITIVE_PAYLOAD_KEYS = frozenset(
    {
        "account",
        "account_id",
        "api_key",
        "email",
        "key_id",
        "order_id",
        "private_key",
        "secret",
        "token",
        "user_id",
        "venue_order_id",
        "wallet",
    }
)


def _parse_decimal(value: Any, field_name: str) -> Decimal:
    if value is None:
        raise ValueError(f"{field_name} is required")
    return Decimal(str(value))


def _parse_datetime(value: Any, field_name: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return datetime.fromisoformat(str(value))


def _parse_required_datetime(value: Any, field_name: str) -> datetime:
    parsed = _parse_datetime(value, field_name)
    if parsed is None:
        raise ValueError(f"{field_name} is required")
    return parsed


RAW_SCHEMA = pa.schema(
    [
        ("_schema_version", pa.int16()),
        ("venue", pa.string()),
        ("source", pa.string()),
        ("channel", pa.string()),
        ("received_at", pa.timestamp("us", tz="UTC")),
        ("exchange_ts", pa.timestamp("us", tz="UTC")),
        ("payload_json", pa.string()),
        ("schema_version", pa.string()),
        ("metadata_json", pa.string()),
    ]
    ,
    metadata={PARQUET_SCHEMA_METADATA_KEY: str(RAW_PARQUET_SCHEMA_VERSION).encode("ascii")},
)


NORMALIZED_SCHEMA = pa.schema(
    [
        ("_schema_version", pa.int16()),
        ("kind", pa.string()),
        ("event_id", pa.string()),
        ("exchange_ts", pa.timestamp("us", tz="UTC")),
        ("received_at", pa.timestamp("us", tz="UTC")),
        ("source", pa.string()),
        ("channel", pa.string()),
        ("payload_json", pa.string()),
    ]
    ,
    metadata={PARQUET_SCHEMA_METADATA_KEY: str(NORMALIZED_PARQUET_SCHEMA_VERSION).encode("ascii")},
)


REJECT_SCHEMA = pa.schema(
    [
        ("_schema_version", pa.int16()),
        ("venue", pa.string()),
        ("source", pa.string()),
        ("channel", pa.string()),
        ("received_at", pa.timestamp("us", tz="UTC")),
        ("exchange_ts", pa.timestamp("us", tz="UTC")),
        ("schema_version", pa.string()),
        ("normalizer_version", pa.string()),
        ("raw_sha256", pa.string()),
        ("reasons_json", pa.string()),
        ("payload_json", pa.string()),
        ("metadata_json", pa.string()),
    ]
    ,
    metadata={PARQUET_SCHEMA_METADATA_KEY: str(REJECT_PARQUET_SCHEMA_VERSION).encode("ascii")},
)


def _utc_microsecond(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _partition_dir_raw(root: Path, venue: Venue | None, source: str, day: date) -> Path:
    venue_seg = venue.value if venue is not None else "unknown"
    return root / "raw" / f"venue={venue_seg}" / f"source={source}" / f"date={day.isoformat()}"


def _partition_dir_normalized(root: Path, kind: str, day: date) -> Path:
    return root / "normalized" / f"kind={kind}" / f"date={day.isoformat()}"


def _partition_dir_reject(root: Path, venue: Venue | None, source: str, channel: str, day: date) -> Path:
    venue_seg = venue.value if venue is not None else "unknown"
    return (
        root
        / "normalization_rejects"
        / f"venue={venue_seg}"
        / f"source={source}"
        / f"channel={channel}"
        / f"date={day.isoformat()}"
    )


def _next_part_path(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    while True:
        path = directory / f"part-{uuid4().hex}.parquet"
        if not path.exists():
            return path


def _payload_for_storage(env: EventEnvelope) -> Mapping[str, Any]:
    if not _is_private_payload(env):
        return env.payload
    redacted = _redact_private_payload(to_primitive(env.payload))
    if not isinstance(redacted, Mapping):
        raise ValueError("event payload must be a mapping")
    return redacted


def _is_private_payload(env: EventEnvelope) -> bool:
    source = env.source.lower()
    channel = env.channel.lower()
    return (
        "private" in source
        or "portfolio" in source
        or channel in PRIVATE_CHANNEL_HINTS
        or channel.startswith("own_")
    )


def _redact_private_payload(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _redacted_marker(item)
            if str(key).lower() in SENSITIVE_PAYLOAD_KEYS
            else _redact_private_payload(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_private_payload(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_private_payload(item) for item in value)
    return value


def _redacted_marker(value: Any) -> str:
    digest = hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _envelope_to_row(env: EventEnvelope) -> dict[str, Any]:
    require_not_future_datetime(env.received_at, "received_at")
    payload = _payload_for_storage(env)
    return {
        "_schema_version": RAW_PARQUET_SCHEMA_VERSION,
        "venue": env.venue.value if env.venue is not None else None,
        "source": env.source,
        "channel": env.channel,
        "received_at": _utc_microsecond(env.received_at),
        "exchange_ts": _utc_microsecond(env.exchange_ts),
        "payload_json": json.dumps(to_primitive(payload), sort_keys=True),
        "schema_version": env.schema_version,
        "metadata_json": json.dumps(to_primitive(env.metadata), sort_keys=True),
    }


def _row_to_envelope(row: dict[str, Any]) -> EventEnvelope:
    venue_value = row.get("venue")
    venue = Venue(venue_value) if venue_value else None
    received_at = row["received_at"]
    exchange_ts = row.get("exchange_ts")
    if isinstance(received_at, datetime) and received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=UTC)
    if isinstance(exchange_ts, datetime) and exchange_ts.tzinfo is None:
        exchange_ts = exchange_ts.replace(tzinfo=UTC)
    return EventEnvelope(
        venue=venue,
        source=row["source"],
        channel=row["channel"],
        received_at=received_at,
        exchange_ts=exchange_ts,
        payload=json.loads(row["payload_json"]),
        schema_version=row["schema_version"],
        metadata=json.loads(row.get("metadata_json") or "{}"),
    )


def _reject_to_row(reject: NormalizationReject) -> dict[str, Any]:
    raw = reject.raw
    require_not_future_datetime(raw.received_at, "received_at")
    payload = _payload_for_storage(raw)
    return {
        "_schema_version": REJECT_PARQUET_SCHEMA_VERSION,
        "venue": raw.venue.value if raw.venue is not None else None,
        "source": raw.source,
        "channel": raw.channel,
        "received_at": _utc_microsecond(raw.received_at),
        "exchange_ts": _utc_microsecond(raw.exchange_ts),
        "schema_version": raw.schema_version,
        "normalizer_version": reject.normalizer_version,
        "raw_sha256": reject.raw_sha256,
        "reasons_json": json.dumps(tuple(reject.reasons), sort_keys=True),
        "payload_json": json.dumps(to_primitive(payload), sort_keys=True),
        "metadata_json": json.dumps(to_primitive(raw.metadata), sort_keys=True),
    }


def _row_to_reject(row: dict[str, Any]) -> NormalizationReject:
    raw = _row_to_envelope(
        {
            "venue": row.get("venue"),
            "source": row["source"],
            "channel": row["channel"],
            "received_at": row["received_at"],
            "exchange_ts": row.get("exchange_ts"),
            "payload_json": row["payload_json"],
            "schema_version": row["schema_version"],
            "metadata_json": row.get("metadata_json") or "{}",
        }
    )
    reasons = json.loads(row["reasons_json"])
    if not isinstance(reasons, list):
        raise ValueError("reject reasons_json must decode to a list")
    return NormalizationReject(
        raw=raw,
        reasons=tuple(str(reason) for reason in reasons),
        raw_sha256=str(row["raw_sha256"]),
        normalizer_version=str(row["normalizer_version"]),
    )


def _normalized_kind_to_payload(event: NormalizedEvent) -> dict[str, Any]:
    """Canonical JSON-shape for a normalized event.

    We keep the exhaustive fields (event_id, provenance) at top level and
    nest the event-specific payload under ``payload``.
    """

    return to_primitive(event)  # type: ignore[return-value]


def _normalized_event_to_row(event: NormalizedEvent) -> dict[str, Any]:
    kind = event_kind(event)
    payload = _normalized_kind_to_payload(event)
    provenance = payload.get("provenance", {})
    timestamps = _extract_event_timestamps(event)
    require_not_future_datetime(timestamps[1], "received_at")
    return {
        "_schema_version": NORMALIZED_PARQUET_SCHEMA_VERSION,
        "kind": kind,
        "event_id": str(event.event_id),
        "exchange_ts": _utc_microsecond(timestamps[0]),
        "received_at": _utc_microsecond(timestamps[1]),
        "source": provenance.get("source", "unknown"),
        "channel": provenance.get("channel", "unknown"),
        "payload_json": json.dumps(payload, sort_keys=True),
    }


def _extract_event_timestamps(event: NormalizedEvent) -> tuple[datetime | None, datetime]:
    """Pull (exchange_ts, received_at) out of any normalized variant."""

    from eventcontracts.domain.events import (
        ExternalSignalEvent,
        LifecycleEvent,
        OrderBookEvent,
        OwnFillEvent,
        OwnOrderRejectEvent,
        OwnOrderUpdateEvent,
        QuoteEvent,
        SettlementResolvedEvent,
        TimerEvent,
        TradeEvent,
    )

    if isinstance(event, QuoteEvent):
        return event.quote.exchange_ts, event.quote.received_at
    if isinstance(event, TradeEvent):
        return event.trade.exchange_ts, event.trade.received_at
    if isinstance(event, OrderBookEvent):
        return event.book.exchange_ts, event.book.received_at
    if isinstance(event, LifecycleEvent):
        return event.lifecycle.exchange_ts, event.lifecycle.received_at
    if isinstance(event, SettlementResolvedEvent):
        return event.settlement.settled_at, event.settlement.settled_at
    if isinstance(event, ExternalSignalEvent):
        return event.exchange_ts, event.received_at
    if isinstance(event, TimerEvent):
        return event.timestamp, event.timestamp
    if isinstance(event, OwnFillEvent):
        return event.fill.exchange_ts, event.fill.filled_at
    if isinstance(event, OwnOrderUpdateEvent):
        return None, event.order.updated_at
    if isinstance(event, OwnOrderRejectEvent):
        return None, event.reject.rejected_at
    raise TypeError(f"unsupported event variant: {type(event).__name__}")


class ParquetEventStore(EventStore, NormalizedEventStore, NormalizationRejectStore):
    """File-backed Parquet store implementing both event-store ports.

    Construction parameters:

    * ``root``: directory under which ``raw/`` and ``normalized/``
      partitions live.
    * ``batch_size``: maximum buffered rows before an implicit flush.

    Call :meth:`flush` to force a write at any time. Readers do not need
    to flush — they walk the partition tree directly.
    """

    def __init__(self, root: Path | str, *, batch_size: int = 1000) -> None:
        self.root = Path(root)
        self.batch_size = batch_size
        self._raw_buffer: list[EventEnvelope] = []
        self._normalized_buffer: list[NormalizedEvent] = []
        self._reject_buffer: list[NormalizationReject] = []

    # ---------- writes ----------

    def append(self, event: EventEnvelope) -> None:
        self._raw_buffer.append(event)
        if len(self._raw_buffer) >= self.batch_size:
            self._flush_raw()

    def append_normalized(self, event: NormalizedEvent) -> None:
        self._normalized_buffer.append(event)
        if len(self._normalized_buffer) >= self.batch_size:
            self._flush_normalized()

    def append_normalization_reject(self, reject: NormalizationReject) -> None:
        self._reject_buffer.append(reject)
        if len(self._reject_buffer) >= self.batch_size:
            self._flush_rejects()

    def flush(self) -> None:
        self._flush_raw()
        self._flush_normalized()
        self._flush_rejects()

    def _flush_raw(self) -> None:
        if not self._raw_buffer:
            return
        # Group rows by partition.
        by_partition: dict[tuple[Venue | None, str, date], list[EventEnvelope]] = {}
        for env in self._raw_buffer:
            day = env.received_at.astimezone(UTC).date()
            by_partition.setdefault((env.venue, env.source, day), []).append(env)
        for (venue, source, day), envelopes in by_partition.items():
            rows = [_envelope_to_row(e) for e in envelopes]
            table = pa.Table.from_pylist(rows, schema=RAW_SCHEMA)
            path = _next_part_path(_partition_dir_raw(self.root, venue, source, day))
            pq.write_table(table, path, compression="snappy")
        self._raw_buffer.clear()

    def _flush_normalized(self) -> None:
        if not self._normalized_buffer:
            return
        by_partition: dict[tuple[str, date], list[NormalizedEvent]] = {}
        for event in self._normalized_buffer:
            ts = _extract_event_timestamps(event)[1]
            day = ts.astimezone(UTC).date() if ts.tzinfo else ts.date()
            kind = event_kind(event)
            by_partition.setdefault((kind, day), []).append(event)
        for (kind, day), events in by_partition.items():
            rows = [_normalized_event_to_row(e) for e in events]
            table = pa.Table.from_pylist(rows, schema=NORMALIZED_SCHEMA)
            path = _next_part_path(_partition_dir_normalized(self.root, kind, day))
            pq.write_table(table, path, compression="snappy")
        self._normalized_buffer.clear()

    def _flush_rejects(self) -> None:
        if not self._reject_buffer:
            return
        by_partition: dict[tuple[Venue | None, str, str, date], list[NormalizationReject]] = {}
        for reject in self._reject_buffer:
            raw = reject.raw
            day = raw.received_at.astimezone(UTC).date()
            by_partition.setdefault((raw.venue, raw.source, raw.channel, day), []).append(reject)
        for (venue, source, channel, day), rejects in by_partition.items():
            rows = [_reject_to_row(reject) for reject in rejects]
            table = pa.Table.from_pylist(rows, schema=REJECT_SCHEMA)
            path = _next_part_path(_partition_dir_reject(self.root, venue, source, channel, day))
            pq.write_table(table, path, compression="snappy")
        self._reject_buffer.clear()

    # ---------- reads ----------

    def read(self, source: str = "*") -> Iterable[EventEnvelope]:
        self.flush()
        raw_root = self.root / "raw"
        if not raw_root.exists():
            return iter(())
        files: list[Path] = []
        for path in raw_root.rglob("*.parquet"):
            if source == "*" or f"/source={source}/" in path.as_posix():
                files.append(path)
        files.sort()
        return self._yield_envelopes(files)

    def _yield_envelopes(self, files: list[Path]) -> Iterator[EventEnvelope]:
        iterators = (_sorted_envelopes_from_file(path) for path in files)
        yield from merge(*iterators, key=envelope_sort_key)

    def read_normalized(self) -> Iterable[NormalizedEvent]:
        self.flush()
        norm_root = self.root / "normalized"
        if not norm_root.exists():
            return iter(())
        files = sorted(norm_root.rglob("*.parquet"))
        return self._yield_normalized(files)

    def read_normalization_rejects(self, source: str = "*") -> Iterable[NormalizationReject]:
        self.flush()
        reject_root = self.root / "normalization_rejects"
        if not reject_root.exists():
            return iter(())
        files: list[Path] = []
        for path in reject_root.rglob("*.parquet"):
            if source == "*" or f"/source={source}/" in path.as_posix():
                files.append(path)
        files.sort()
        return self._yield_rejects(files)

    def _yield_rejects(self, files: list[Path]) -> Iterator[NormalizationReject]:
        iterators = (_sorted_rejects_from_file(path) for path in files)
        yield from merge(*iterators, key=normalization_reject_sort_key)

    def _yield_normalized(self, files: list[Path]) -> Iterator[NormalizedEvent]:
        iterators = (_sorted_normalized_from_file(path) for path in files)
        yield from merge(*iterators, key=normalized_event_sort_key)

    def expire_private_partitions(
        self,
        *,
        retention_days: int,
        now: datetime | None = None,
    ) -> int:
        """Delete private raw/reject parquet files older than the TTL.

        Raw partitions do not include channel in the path, so raw TTL applies
        to sources marked private/portfolio. Reject partitions include both
        source and channel and use either signal.
        """

        if retention_days < 0:
            raise ValueError("retention_days must be non-negative")
        self.flush()
        reference = (now or datetime.now(UTC)).astimezone(UTC).date()
        cutoff = reference - timedelta(days=retention_days)
        deleted = 0
        for file_path in list((self.root / "raw").rglob("*.parquet")):
            if _private_partition_file(file_path, include_channel=False) and _partition_day(file_path) < cutoff:
                file_path.unlink()
                deleted += 1
                _prune_empty_parents(file_path.parent, stop=self.root)
        for file_path in list((self.root / "normalization_rejects").rglob("*.parquet")):
            if _private_partition_file(file_path, include_channel=True) and _partition_day(file_path) < cutoff:
                file_path.unlink()
                deleted += 1
                _prune_empty_parents(file_path.parent, stop=self.root)
        return deleted


def _sorted_envelopes_from_file(path: Path) -> Iterator[EventEnvelope]:
    rows = [
        _row_to_envelope(row)
        for row in _read_table_checked(path, RAW_PARQUET_SCHEMA_VERSION).to_pylist()
    ]
    rows.sort(key=envelope_sort_key)
    yield from rows


def _sorted_rejects_from_file(path: Path) -> Iterator[NormalizationReject]:
    rows = [
        _row_to_reject(row)
        for row in _read_table_checked(path, REJECT_PARQUET_SCHEMA_VERSION).to_pylist()
    ]
    rows.sort(key=normalization_reject_sort_key)
    yield from rows


def _sorted_normalized_from_file(path: Path) -> Iterator[NormalizedEvent]:
    rows: list[NormalizedEvent] = []
    for row in _read_table_checked(path, NORMALIZED_PARQUET_SCHEMA_VERSION).to_pylist():
        payload = json.loads(row["payload_json"])
        rows.append(_deserialize_normalized(row["kind"], payload))
    rows.sort(key=normalized_event_sort_key)
    yield from rows


def _partition_day(path: Path) -> date:
    for part in path.parts:
        if part.startswith("date="):
            return date.fromisoformat(part.removeprefix("date="))
    raise ValueError(f"partition path missing date segment: {path}")


def _private_partition_file(path: Path, *, include_channel: bool) -> bool:
    source_private = False
    channel_private = False
    for part in path.parts:
        lowered = part.lower()
        if lowered.startswith("source="):
            source = lowered.removeprefix("source=")
            source_private = "private" in source or "portfolio" in source
        if include_channel and lowered.startswith("channel="):
            channel = lowered.removeprefix("channel=")
            channel_private = channel in PRIVATE_CHANNEL_HINTS or channel.startswith("own_")
    return source_private or channel_private


def _prune_empty_parents(path: Path, *, stop: Path) -> None:
    stop = stop.resolve()
    current = path.resolve()
    while current != stop and _is_relative_to(current, stop):
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _parse_schema_version(raw: Any) -> int | None:
    """Decode the KV schema-version marker, or ``None`` if absent/unparseable.

    A missing or unparseable marker means a *legacy* file written before the
    version marker existed; callers treat that as "older, read tolerantly".
    """

    if raw is None:
        return None
    try:
        return int(raw.decode("ascii") if isinstance(raw, bytes) else str(raw))
    except (ValueError, AttributeError):
        return None


def _read_table_checked(path: Path, expected_version: int) -> Any:
    """Read a parquet partition, tolerating legacy/older schema versions.

    Forward-compatibility is the rule the data lake needs: a schema bump must
    not brick previously-captured data. So:

    * version == expected -> strict path (require the integrity column too);
    * version missing or < expected -> *upcast*: read the data as-is. The data
      columns are read-compatible; row deserializers fill any newer optional
      fields with defaults. Run ``ec migrate-data`` to stamp the current marker;
    * version > expected -> hard fail (the file was written by a newer build).
    """

    parquet_file = pq.ParquetFile(str(path))
    metadata = parquet_file.metadata.metadata or {}
    actual_version = _parse_schema_version(metadata.get(PARQUET_SCHEMA_METADATA_KEY))
    if actual_version is not None and actual_version > expected_version:
        raise ValueError(
            f"parquet schema_version newer than supported for {path}: "
            f"{actual_version} > {expected_version}; upgrade eventcontracts to read it"
        )
    table = parquet_file.read()
    if actual_version == expected_version:
        # A current-version file must carry the integrity column verbatim.
        if "_schema_version" not in table.column_names:
            raise ValueError(f"parquet schema_version column missing: {path}")
        versions = set(table.column("_schema_version").to_pylist())
        if versions != {expected_version}:
            raise ValueError(
                f"mixed parquet schema_version values for {path}: {sorted(versions)}"
            )
    # Legacy/older files are read tolerantly; their absent integrity column is
    # not required (the data columns themselves are compatible).
    return table


def _migrate_partition_file(path: Path, target_version: int) -> bool:
    """Stamp a legacy/older parquet file with the current schema marker in place.

    Returns ``True`` if the file was rewritten, ``False`` if already current.
    Type-preserving: it only adds the ``_schema_version`` integrity column (when
    absent) and the KV metadata marker, then atomically replaces the file. It
    does not re-run redaction or re-serialize payloads.
    """

    # Read the file fully into memory first: this releases the OS file handle
    # before os.replace (Windows blocks replacing an open file), and reading a
    # single ParquetFile (not a dataset path) avoids hive-partition inference
    # that would clash the `venue=` directory with the file's own `venue` column.
    with open(path, "rb") as handle:
        raw_bytes = handle.read()
    parquet_file = pq.ParquetFile(pa.BufferReader(raw_bytes))
    metadata = parquet_file.metadata.metadata or {}
    actual_version = _parse_schema_version(metadata.get(PARQUET_SCHEMA_METADATA_KEY))
    table = parquet_file.read()
    has_column = "_schema_version" in table.column_names
    if actual_version == target_version and has_column:
        return False

    if "_schema_version" not in table.column_names:
        version_column = pa.array([target_version] * table.num_rows, type=pa.int16())
        table = table.add_column(
            0, pa.field("_schema_version", pa.int16()), version_column
        )
    schema_metadata = dict(table.schema.metadata or {})
    schema_metadata[PARQUET_SCHEMA_METADATA_KEY] = str(target_version).encode("ascii")
    table = table.replace_schema_metadata(schema_metadata)

    tmp_path = path.with_name(path.name + ".migrate.tmp")
    pq.write_table(table, str(tmp_path))
    os.replace(tmp_path, path)
    return True


def migrate_event_lake(root: Path | str) -> dict[str, int]:
    """Upgrade every legacy/older partition under ``root`` to the current schema.

    Walks raw, normalized, and reject partition trees and stamps any file whose
    schema marker is missing or older. Idempotent: re-running migrates nothing.
    """

    root = Path(root)
    targets = (
        ("raw", root / "raw", RAW_PARQUET_SCHEMA_VERSION),
        ("normalized", root / "normalized", NORMALIZED_PARQUET_SCHEMA_VERSION),
        ("rejects", root / "normalization_rejects", REJECT_PARQUET_SCHEMA_VERSION),
    )
    counts = {"raw": 0, "normalized": 0, "rejects": 0, "skipped": 0}
    for label, subtree, version in targets:
        if not subtree.exists():
            continue
        for file_path in sorted(subtree.rglob("*.parquet")):
            if _migrate_partition_file(file_path, version):
                counts[label] += 1
            else:
                counts["skipped"] += 1
    return counts


def _deserialize_normalized(kind: str, payload: dict[str, Any]) -> NormalizedEvent:
    """Rebuild a :class:`NormalizedEvent` from canonical JSON."""

    from eventcontracts.domain.events import (
        EventProvenance,
        ExternalSignalEvent,
        LifecycleEvent,
        OrderBookEvent,
        OwnFillEvent,
        OwnOrderRejectEvent,
        OwnOrderUpdateEvent,
        QuoteEvent,
        SettlementResolvedEvent,
        TimerEvent,
        TradeEvent,
    )
    from eventcontracts.domain.fills import Fill
    from eventcontracts.domain.ids import (
        ClientOrderId,
        CorrelationId,
        EventId,
        FillId,
        SleeveId,
        StrategyId,
        VenueOrderId,
    )
    from eventcontracts.domain.lifecycle import (
        MarketLifecycleEvent,
        MarketLifecycleKind,
        SettlementEvent,
    )
    from eventcontracts.domain.models import (
        InstrumentId,
        OrderBookLevel,
        OutcomeSide,
        Quote,
        Trade,
    )
    from eventcontracts.domain.models import (
        OrderBook as DomainOrderBook,
    )
    from eventcontracts.domain.models import (
        Venue as DomainVenue,
    )
    from eventcontracts.domain.orders import (
        Liquidity,
        Order,
        OrderReject,
        OrderSide,
        OrderStatus,
        OrderType,
        TimeInForce,
    )

    if kind not in EVENT_KINDS:
        raise ValueError(f"unknown event kind: {kind}")

    def _instr(data: dict[str, Any]) -> InstrumentId:
        return InstrumentId(
            venue=DomainVenue(data["venue"]),
            market_id=data["market_id"],
            outcome_id=data.get("outcome_id"),
        )

    def _provenance(data: dict[str, Any]) -> EventProvenance:
        return EventProvenance(
            source=data.get("source", "unknown"),
            channel=data.get("channel", "unknown"),
            schema_version=data.get("schema_version", "normalized-event-v1"),
            venue=DomainVenue(data["venue"]) if data.get("venue") else None,
            source_sequence=data.get("source_sequence"),
            normalization_version=data.get("normalization_version", "normalizer-v1"),
            metadata=data.get("metadata", {}),
        )

    provenance = _provenance(payload.get("provenance", {}))
    event_id = EventId(payload["event_id"])

    if kind == "trade":
        trade_data = payload["trade"]
        trade = Trade(
            instrument_id=_instr(trade_data["instrument_id"]),
            side=OutcomeSide(trade_data["side"]) if trade_data.get("side") else None,
            price=_parse_decimal(trade_data["price"], "trade.price"),
            quantity=_parse_decimal(trade_data["quantity"], "trade.quantity"),
            trade_id=trade_data.get("trade_id"),
            exchange_ts=_parse_datetime(trade_data["exchange_ts"], "trade.exchange_ts"),
            received_at=_parse_required_datetime(trade_data["received_at"], "trade.received_at"),
            aggressor_side=OutcomeSide(trade_data["aggressor_side"]) if trade_data.get("aggressor_side") else None,
        )
        return TradeEvent(event_id=event_id, trade=trade, provenance=provenance)

    if kind == "quote":
        quote_data = payload["quote"]
        bid = quote_data.get("bid")
        ask = quote_data.get("ask")
        bid_level = (
            OrderBookLevel(
                price=_parse_decimal(bid["price"], "bid"),
                quantity=_parse_decimal(bid["quantity"], "bid"),
            )
            if bid
            else None
        )
        ask_level = (
            OrderBookLevel(
                price=_parse_decimal(ask["price"], "ask"),
                quantity=_parse_decimal(ask["quantity"], "ask"),
            )
            if ask
            else None
        )
        quote = Quote(
            instrument_id=_instr(quote_data["instrument_id"]),
            side=OutcomeSide(quote_data["side"]),
            bid=bid_level,
            ask=ask_level,
            exchange_ts=_parse_datetime(quote_data["exchange_ts"], "quote.exchange_ts"),
            received_at=_parse_required_datetime(quote_data["received_at"], "quote.received_at"),
        )
        return QuoteEvent(event_id=event_id, quote=quote, provenance=provenance)

    if kind == "book":
        book_data = payload["book"]
        def _levels(items: list[dict[str, Any]]) -> tuple[OrderBookLevel, ...]:
            return tuple(
                OrderBookLevel(
                    price=_parse_decimal(it["price"], "level"),
                    quantity=_parse_decimal(it["quantity"], "level"),
                )
                for it in items
            )

        book = DomainOrderBook(
            instrument_id=_instr(book_data["instrument_id"]),
            yes_bids=_levels(book_data["yes_bids"]),
            yes_asks=_levels(book_data["yes_asks"]),
            no_bids=_levels(book_data["no_bids"]),
            no_asks=_levels(book_data["no_asks"]),
            exchange_ts=_parse_datetime(book_data["exchange_ts"], "book.exchange_ts"),
            received_at=_parse_required_datetime(book_data["received_at"], "book.received_at"),
        )
        return OrderBookEvent(event_id=event_id, book=book, provenance=provenance)

    if kind == "lifecycle":
        lc = payload["lifecycle"]
        lifecycle = MarketLifecycleEvent(
            instrument_id=_instr(lc["instrument_id"]),
            kind=MarketLifecycleKind(lc["kind"]),
            exchange_ts=_parse_datetime(lc["exchange_ts"], "lifecycle.exchange_ts"),
            received_at=_parse_required_datetime(lc["received_at"], "lifecycle.received_at"),
            reason=lc.get("reason"),
            metadata=lc.get("metadata", {}),
        )
        return LifecycleEvent(event_id=event_id, lifecycle=lifecycle, provenance=provenance)

    if kind == "settlement":
        st = payload["settlement"]
        settlement = SettlementEvent(
            instrument_id=_instr(st["instrument_id"]),
            resolved_side=OutcomeSide(st["resolved_side"]) if st.get("resolved_side") else None,
            payout_per_contract=_parse_decimal(st["payout_per_contract"], "settlement.payout"),
            currency=st["currency"],
            settled_at=_parse_required_datetime(st["settled_at"], "settlement.settled_at"),
            source=st["source"],
            metadata=st.get("metadata", {}),
        )
        return SettlementResolvedEvent(event_id=event_id, settlement=settlement, provenance=provenance)

    if kind == "external":
        return ExternalSignalEvent(
            event_id=event_id,
            source=payload["source"],
            exchange_ts=_parse_datetime(payload["exchange_ts"], "external.exchange_ts"),
            received_at=_parse_required_datetime(payload["received_at"], "external.received_at"),
            schema_version=payload["schema_version"],
            payload=payload.get("payload", {}),
            provenance=provenance,
        )

    if kind == "timer":
        return TimerEvent(
            event_id=event_id,
            timestamp=_parse_required_datetime(payload["timestamp"], "timer.timestamp"),
            label=payload["label"],
            provenance=provenance,
        )

    if kind == "own_fill":
        fill_data = payload["fill"]
        filled_at = _parse_datetime(fill_data["filled_at"], "fill.filled_at")
        assert filled_at is not None  # required field
        fill = Fill(
            fill_id=FillId(fill_data["fill_id"]),
            venue_order_id=VenueOrderId(fill_data["venue_order_id"]),
            client_order_id=ClientOrderId(fill_data["client_order_id"]),
            instrument_id=_instr(fill_data["instrument_id"]),
            outcome_side=OutcomeSide(fill_data["outcome_side"]),
            order_side=OrderSide(fill_data["order_side"]),
            price=_parse_decimal(fill_data["price"], "fill.price"),
            quantity=_parse_decimal(fill_data["quantity"], "fill.quantity"),
            liquidity=Liquidity(fill_data["liquidity"]),
            fee_amount=_parse_decimal(fill_data["fee_amount"], "fill.fee_amount"),
            fee_currency=fill_data["fee_currency"],
            filled_at=filled_at,
            exchange_ts=_parse_datetime(fill_data.get("exchange_ts"), "fill.exchange_ts"),
            correlation_id=CorrelationId(fill_data["correlation_id"]),
            strategy_id=StrategyId(fill_data["strategy_id"]),
            sleeve_id=SleeveId(fill_data["sleeve_id"]),
            metadata=fill_data.get("metadata", {}),
        )
        return OwnFillEvent(event_id=event_id, fill=fill, provenance=provenance)

    if kind == "own_order_update":
        order_data = payload["order"]
        created_at = _parse_datetime(order_data["created_at"], "order.created_at")
        updated_at = _parse_datetime(order_data["updated_at"], "order.updated_at")
        assert created_at is not None and updated_at is not None
        venue_order_id_value = order_data.get("venue_order_id")
        order = Order(
            client_order_id=ClientOrderId(order_data["client_order_id"]),
            venue_order_id=VenueOrderId(venue_order_id_value) if venue_order_id_value else None,
            instrument_id=_instr(order_data["instrument_id"]),
            outcome_side=OutcomeSide(order_data["outcome_side"]),
            order_side=OrderSide(order_data["order_side"]),
            order_type=OrderType(order_data["order_type"]),
            time_in_force=TimeInForce(order_data["time_in_force"]),
            price=(
                _parse_decimal(order_data["price"], "order.price")
                if order_data.get("price") is not None
                else None
            ),
            quantity=_parse_decimal(order_data["quantity"], "order.quantity"),
            filled_quantity=_parse_decimal(
                order_data["filled_quantity"], "order.filled_quantity"
            ),
            status=OrderStatus(order_data["status"]),
            created_at=created_at,
            updated_at=updated_at,
            correlation_id=CorrelationId(order_data["correlation_id"]),
            strategy_id=StrategyId(order_data["strategy_id"]),
            sleeve_id=SleeveId(order_data["sleeve_id"]),
            expires_at=_parse_datetime(order_data.get("expires_at"), "order.expires_at"),
            metadata=order_data.get("metadata", {}),
        )
        return OwnOrderUpdateEvent(event_id=event_id, order=order, provenance=provenance)

    if kind == "own_order_reject":
        reject_data = payload["reject"]
        rejected_at = _parse_datetime(reject_data["rejected_at"], "reject.rejected_at")
        assert rejected_at is not None
        reject = OrderReject(
            client_order_id=ClientOrderId(reject_data["client_order_id"]),
            reason=reject_data["reason"],
            rejected_at=rejected_at,
            venue_code=reject_data.get("venue_code"),
        )
        return OwnOrderRejectEvent(event_id=event_id, reject=reject, provenance=provenance)

    raise ValueError(f"deserialization not implemented for kind={kind}")
