"""Parquet-backed event storage.

Raw and normalized events both land here. The layout is::

    <root>/raw/venue=<venue>/source=<source>/date=YYYY-MM-DD/part-NNNN.parquet
    <root>/normalized/kind=<kind>/date=YYYY-MM-DD/part-NNNN.parquet

Each ``append*`` call buffers in memory; :meth:`flush` writes a new
``part-NNNN.parquet`` file under the appropriate partition directory.
Readers use :class:`ParquetEventStore.read` (or the DuckDB store) to
re-materialize the events in deterministic order.

The schema is the canonical JSON form of the envelope / event so that
both Python and Rust can read identical bytes.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as _pa_mod
import pyarrow.parquet as _pq_mod

from eventcontracts.domain.events import (
    EVENT_KINDS,
    NormalizedEvent,
    event_kind,
)
from eventcontracts.domain.models import Venue
from eventcontracts.domain.serialization import to_primitive
from eventcontracts.storage.interfaces import (
    EventEnvelope,
    EventStore,
    NormalizationReject,
    NormalizationRejectStore,
    NormalizedEventStore,
)

# pyarrow does not ship strict type stubs; treat the modules as Any so the
# rest of this file stays strict-mypy clean without scattering type:ignore.
pa: Any = _pa_mod
pq: Any = _pq_mod


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
        ("venue", pa.string()),
        ("source", pa.string()),
        ("channel", pa.string()),
        ("received_at", pa.timestamp("us", tz="UTC")),
        ("exchange_ts", pa.timestamp("us", tz="UTC")),
        ("payload_json", pa.string()),
        ("schema_version", pa.string()),
        ("metadata_json", pa.string()),
    ]
)


NORMALIZED_SCHEMA = pa.schema(
    [
        ("kind", pa.string()),
        ("event_id", pa.string()),
        ("exchange_ts", pa.timestamp("us", tz="UTC")),
        ("received_at", pa.timestamp("us", tz="UTC")),
        ("source", pa.string()),
        ("channel", pa.string()),
        ("payload_json", pa.string()),
    ]
)


REJECT_SCHEMA = pa.schema(
    [
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
    existing = sorted(directory.glob("part-*.parquet"))
    next_index = len(existing)
    return directory / f"part-{next_index:05d}.parquet"


def _envelope_to_row(env: EventEnvelope) -> dict[str, Any]:
    return {
        "venue": env.venue.value if env.venue is not None else None,
        "source": env.source,
        "channel": env.channel,
        "received_at": _utc_microsecond(env.received_at),
        "exchange_ts": _utc_microsecond(env.exchange_ts),
        "payload_json": json.dumps(to_primitive(env.payload), sort_keys=True),
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
    return {
        "venue": raw.venue.value if raw.venue is not None else None,
        "source": raw.source,
        "channel": raw.channel,
        "received_at": _utc_microsecond(raw.received_at),
        "exchange_ts": _utc_microsecond(raw.exchange_ts),
        "schema_version": raw.schema_version,
        "normalizer_version": reject.normalizer_version,
        "raw_sha256": reject.raw_sha256,
        "reasons_json": json.dumps(tuple(reject.reasons), sort_keys=True),
        "payload_json": json.dumps(to_primitive(raw.payload), sort_keys=True),
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
    return {
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
            if source == "*" or f"/source={source}/" in str(path):
                files.append(path)
        files.sort()
        return self._yield_envelopes(files)

    def _yield_envelopes(self, files: list[Path]) -> Iterator[EventEnvelope]:
        envelopes: list[EventEnvelope] = []
        for path in files:
            table = pq.ParquetFile(str(path)).read()
            for row in table.to_pylist():
                envelopes.append(_row_to_envelope(row))
        envelopes.sort(
            key=lambda e: (
                e.exchange_ts or e.received_at,
                e.received_at,
                e.source,
                e.channel,
            )
        )
        yield from envelopes

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
            if source == "*" or f"/source={source}/" in str(path):
                files.append(path)
        files.sort()
        return self._yield_rejects(files)

    def _yield_rejects(self, files: list[Path]) -> Iterator[NormalizationReject]:
        rejects: list[NormalizationReject] = []
        for path in files:
            table = pq.ParquetFile(str(path)).read()
            for row in table.to_pylist():
                rejects.append(_row_to_reject(row))
        rejects.sort(
            key=lambda reject: (
                reject.raw.exchange_ts or reject.raw.received_at,
                reject.raw.received_at,
                reject.raw.source,
                reject.raw.channel,
                reject.raw_sha256,
            )
        )
        yield from rejects

    def _yield_normalized(self, files: list[Path]) -> Iterator[NormalizedEvent]:
        rows: list[tuple[datetime, datetime, str, dict[str, Any]]] = []
        for path in files:
            table = pq.ParquetFile(str(path)).read()
            for row in table.to_pylist():
                payload = json.loads(row["payload_json"])
                ex_ts = row.get("exchange_ts")
                rx_ts = row["received_at"]
                if isinstance(ex_ts, datetime) and ex_ts.tzinfo is None:
                    ex_ts = ex_ts.replace(tzinfo=UTC)
                if isinstance(rx_ts, datetime) and rx_ts.tzinfo is None:
                    rx_ts = rx_ts.replace(tzinfo=UTC)
                rows.append((ex_ts or rx_ts, rx_ts, row["kind"], payload))
        rows.sort(key=lambda t: (t[0], t[1], t[3].get("event_id", "")))
        for _ex_ts, _rx_ts, kind, payload in rows:
            yield _deserialize_normalized(kind, payload)


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
