"""Parquet-backed event store: round-trip, partitioning, determinism."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from eventcontracts.domain.events import (
    EventProvenance,
    ExternalSignalEvent,
    OwnFillEvent,
    OwnOrderRejectEvent,
    OwnOrderUpdateEvent,
    QuoteEvent,
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
from eventcontracts.domain.models import (
    InstrumentId,
    OrderBookLevel,
    OutcomeSide,
    Quote,
    Trade,
    Venue,
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
from eventcontracts.storage import (
    EventEnvelope,
    InMemoryEventStore,
    NormalizationReject,
    ParquetEventStore,
)
from eventcontracts.storage.parquet_store import (
    PARQUET_SCHEMA_METADATA_KEY,
    RAW_PARQUET_SCHEMA_VERSION,
    migrate_event_lake,
)
from eventcontracts.storage.sorting import normalized_event_sort_key

NOW = datetime(2026, 1, 15, 14, 30, tzinfo=UTC)
INSTR = InstrumentId(venue=Venue.KALSHI, market_id="M-1", outcome_id=None)


def _envelope(
    channel: str,
    payload: dict[str, object],
    *,
    source: str = "kalshi-md",
    at: datetime = NOW,
    metadata: dict[str, object] | None = None,
) -> EventEnvelope:
    return EventEnvelope(
        venue=Venue.KALSHI,
        source=source,
        channel=channel,
        received_at=at,
        exchange_ts=at,
        payload=payload,
        schema_version="raw-event-v1",
        metadata=metadata or {},
    )


def _trade_event(price: str, qty: str) -> TradeEvent:
    return TradeEvent(
        event_id=EventId(f"t-{price}-{qty}"),
        trade=Trade(
            instrument_id=INSTR,
            side=OutcomeSide.YES,
            price=Decimal(price),
            quantity=Decimal(qty),
            trade_id=None,
            exchange_ts=NOW,
            received_at=NOW,
        ),
        provenance=EventProvenance(source="kalshi-md", channel="trade", venue=Venue.KALSHI),
    )


def _quote_event(bid: str, ask: str) -> QuoteEvent:
    return QuoteEvent(
        event_id=EventId(f"q-{bid}-{ask}"),
        quote=Quote(
            instrument_id=INSTR,
            side=OutcomeSide.YES,
            bid=OrderBookLevel(price=Decimal(bid), quantity=Decimal("10")),
            ask=OrderBookLevel(price=Decimal(ask), quantity=Decimal("10")),
            exchange_ts=NOW,
            received_at=NOW,
        ),
        provenance=EventProvenance(source="kalshi-md", channel="quote", venue=Venue.KALSHI),
    )


def test_raw_envelope_round_trip(tmp_path: Path) -> None:
    store = ParquetEventStore(tmp_path)
    env = _envelope("trade", {"market_id": "M-1", "price": "0.5", "quantity": "10"})
    store.append(env)
    store.flush()

    read_back = list(store.read())
    assert len(read_back) == 1
    assert read_back[0].source == "kalshi-md"
    assert read_back[0].payload["price"] == "0.5"


def test_raw_parquet_writes_schema_version_metadata_and_column(tmp_path: Path) -> None:
    store = ParquetEventStore(tmp_path)
    store.append(_envelope("trade", {"market_id": "M-1"}))
    store.flush()

    path = next((tmp_path / "raw").rglob("*.parquet"))
    parquet_file = pq.ParquetFile(path)
    metadata = parquet_file.metadata.metadata or {}
    table = parquet_file.read()

    assert metadata[PARQUET_SCHEMA_METADATA_KEY] == str(RAW_PARQUET_SCHEMA_VERSION).encode("ascii")
    assert set(table.column("_schema_version").to_pylist()) == {RAW_PARQUET_SCHEMA_VERSION}


def test_raw_parquet_newer_schema_version_raises(tmp_path: Path) -> None:
    # A file written by a NEWER build than this one must hard-fail (we can't know
    # how to read it). Older/missing versions are tolerated (see the legacy test).
    store = ParquetEventStore(tmp_path)
    store.append(_envelope("trade", {"market_id": "M-1"}))
    store.flush()
    partition = next((tmp_path / "raw").rglob("date=*"))
    bad = partition / "part-bad.parquet"
    schema = pa.schema(
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
        ],
        metadata={PARQUET_SCHEMA_METADATA_KEY: b"2"},
    )
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "_schema_version": 2,
                    "venue": "kalshi",
                    "source": "kalshi-md",
                    "channel": "trade",
                    "received_at": NOW,
                    "exchange_ts": NOW,
                    "payload_json": "{}",
                    "schema_version": "raw-event-v2",
                    "metadata_json": "{}",
                }
            ],
            schema=schema,
        ),
        bad,
    )

    with pytest.raises(ValueError, match="newer than supported"):
        list(store.read(source="*"))


def test_legacy_parquet_without_schema_version_reads_and_migrates(tmp_path: Path) -> None:
    # V6-D1: a file written before the schema marker existed (no KV metadata and
    # no `_schema_version` column) must read via upcast, and `migrate-data` must
    # stamp it so the strict path applies afterward.
    store = ParquetEventStore(tmp_path)
    store.append(_envelope("trade", {"market_id": "M-1"}))
    store.flush()
    partition = next((tmp_path / "raw").rglob("date=*"))
    legacy = partition / "part-legacy.parquet"
    legacy_schema = pa.schema(
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
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "venue": "kalshi",
                    "source": "kalshi-md",
                    "channel": "trade",
                    "received_at": NOW,
                    "exchange_ts": NOW,
                    "payload_json": '{"market_id": "L-1"}',
                    "schema_version": "raw-event-v0",
                    "metadata_json": "{}",
                }
            ],
            schema=legacy_schema,
        ),
        legacy,
    )

    # Reads via upcast (would previously crash with a strict mismatch).
    read_ids = {e.payload.get("market_id") for e in store.read(source="*")}
    assert "L-1" in read_ids

    counts = migrate_event_lake(tmp_path)
    assert counts["raw"] >= 1  # the legacy file was stamped
    parquet_file = pq.ParquetFile(legacy)
    metadata = parquet_file.metadata.metadata or {}
    assert metadata[PARQUET_SCHEMA_METADATA_KEY] == str(RAW_PARQUET_SCHEMA_VERSION).encode("ascii")
    assert "_schema_version" in parquet_file.schema_arrow.names

    # Still reads after migration (now via the strict path), and is idempotent.
    assert "L-1" in {e.payload.get("market_id") for e in store.read(source="*")}
    assert migrate_event_lake(tmp_path)["raw"] == 0


def test_raw_envelope_rejects_far_future_received_at() -> None:
    with pytest.raises(ValueError, match="future"):
        _envelope(
            "trade",
            {"market_id": "M-1"},
            at=datetime(9999, 1, 1, tzinfo=UTC),
        )


def test_private_raw_payloads_are_redacted_before_parquet_write(tmp_path: Path) -> None:
    store = ParquetEventStore(tmp_path)
    store.append(
        _envelope(
            "order",
            {
                "account_id": "acct-secret",
                "order_id": "venue-order-secret",
                "market_id": "M-1",
                "nested": {"token": "token-secret"},
            },
            source="kalshi-private",
        )
    )
    store.flush()

    path = next((tmp_path / "raw").rglob("*.parquet"))
    payload_json = pq.ParquetFile(path).read().column("payload_json").to_pylist()[0]

    assert "acct-secret" not in payload_json
    assert "venue-order-secret" not in payload_json
    assert "token-secret" not in payload_json
    assert "market_id" in payload_json


def test_private_reject_payloads_are_redacted_before_parquet_write(tmp_path: Path) -> None:
    store = ParquetEventStore(tmp_path)
    raw = _envelope(
        "own_fill",
        {"account_id": "acct-secret", "fill_id": "fill-public-ish"},
        source="kalshi-private",
    )
    store.append_normalization_reject(
        NormalizationReject(
            raw=raw,
            reasons=("fixture",),
            raw_sha256="a" * 64,
        )
    )
    store.flush()

    path = next((tmp_path / "normalization_rejects").rglob("*.parquet"))
    payload_json = pq.ParquetFile(path).read().column("payload_json").to_pylist()[0]

    assert "acct-secret" not in payload_json
    assert "fill-public-ish" in payload_json


def test_private_partition_ttl_deletes_only_private_old_files(tmp_path: Path) -> None:
    store = ParquetEventStore(tmp_path)
    old = datetime(2026, 1, 1, tzinfo=UTC)
    recent = datetime(2026, 1, 10, tzinfo=UTC)
    store.append(_envelope("order", {"account_id": "old"}, source="kalshi-private", at=old))
    store.append(_envelope("trade", {"market_id": "public"}, source="kalshi-md", at=old))
    store.append(_envelope("order", {"account_id": "recent"}, source="kalshi-private", at=recent))
    store.flush()

    deleted = store.expire_private_partitions(
        retention_days=3,
        now=datetime(2026, 1, 10, tzinfo=UTC),
    )

    assert deleted == 1
    remaining = list((tmp_path / "raw").rglob("*.parquet"))
    assert len(remaining) == 2
    assert any("source=kalshi-md" in path.as_posix() for path in remaining)


def test_normalized_round_trip(tmp_path: Path) -> None:
    store = ParquetEventStore(tmp_path)
    store.append_normalized(_trade_event("0.40", "10"))
    store.append_normalized(_quote_event("0.39", "0.41"))
    store.flush()

    events = list(store.read_normalized())
    assert len(events) == 2
    # Ordering by exchange_ts then received_at is deterministic.
    assert isinstance(events[0], TradeEvent | QuoteEvent)


def test_partitioning_creates_expected_directories(tmp_path: Path) -> None:
    store = ParquetEventStore(tmp_path)
    store.append(_envelope("trade", {"market_id": "M-1", "price": "0.5", "quantity": "1"}))
    store.append(
        _envelope(
            "trade",
            {"market_id": "M-2", "price": "0.6", "quantity": "1"},
            at=NOW.replace(day=16),
        )
    )
    store.flush()

    expected_partitions = [
        tmp_path / "raw" / "venue=kalshi" / "source=kalshi-md" / "date=2026-01-15",
        tmp_path / "raw" / "venue=kalshi" / "source=kalshi-md" / "date=2026-01-16",
    ]
    for path in expected_partitions:
        assert path.exists(), f"missing partition {path}"
        files = list(path.glob("*.parquet"))
        assert files, f"no parquet files in {path}"


def test_flush_uses_unique_uuid_part_names_without_overwrite(tmp_path: Path) -> None:
    store = ParquetEventStore(tmp_path)
    store.append(_envelope("trade", {"market_id": "M-1", "price": "0.5", "quantity": "1"}))
    store.flush()
    store.append(_envelope("trade", {"market_id": "M-1", "price": "0.6", "quantity": "1"}))
    store.flush()

    partition = tmp_path / "raw" / "venue=kalshi" / "source=kalshi-md" / "date=2026-01-15"
    files = sorted(partition.glob("part-*.parquet"))

    assert len(files) == 2
    assert files[0].name != files[1].name
    assert all(len(path.stem.removeprefix("part-")) == 32 for path in files)
    assert len(list(store.read(source="kalshi-md"))) == 2


def test_parquet_and_inmemory_raw_sorting_match_on_timestamp_ties(tmp_path: Path) -> None:
    events = (
        _envelope("trade", {"market_id": "M-1"}, source="b", metadata={"source_sequence": "2"}),
        _envelope("trade", {"market_id": "M-1"}, source="a", metadata={"source_sequence": "3"}),
        _envelope("book", {"market_id": "M-1"}, source="a", metadata={"source_sequence": "1"}),
    )
    parquet = ParquetEventStore(tmp_path, batch_size=10)
    memory = InMemoryEventStore()
    for event in events:
        parquet.append(event)
        memory.append(event)
    parquet.flush()

    parquet_order = [(e.source, e.channel, e.metadata.get("source_sequence")) for e in parquet.read(source="*")]
    memory_order = [(e.source, e.channel, e.metadata.get("source_sequence")) for e in memory.read(source="*")]

    assert parquet_order == memory_order


def test_external_signal_sorting_uses_received_at_not_exchange_ts() -> None:
    early_published_late_received = ExternalSignalEvent(
        event_id=EventId("external-late"),
        source="provider",
        exchange_ts=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        received_at=datetime(2026, 1, 1, 12, 0, 5, tzinfo=UTC),
        schema_version="external-v1",
        payload={"market_id": "M-1"},
    )
    later_published_early_received = ExternalSignalEvent(
        event_id=EventId("external-early"),
        source="provider",
        exchange_ts=datetime(2026, 1, 1, 12, 0, 3, tzinfo=UTC),
        received_at=datetime(2026, 1, 1, 12, 0, 4, tzinfo=UTC),
        schema_version="external-v1",
        payload={"market_id": "M-1"},
    )

    ordered = sorted(
        (early_published_late_received, later_published_early_received),
        key=normalized_event_sort_key,
    )

    assert [event.event_id for event in ordered] == [
        EventId("external-early"),
        EventId("external-late"),
    ]


def test_round_trip_preserves_ordering_with_many_events(tmp_path: Path) -> None:
    store = ParquetEventStore(tmp_path, batch_size=3)
    for i in range(10):
        ev = TradeEvent(
            event_id=EventId(f"t-{i:03d}"),
            trade=Trade(
                instrument_id=INSTR,
                side=OutcomeSide.YES,
                price=Decimal("0.50"),
                quantity=Decimal("1"),
                trade_id=None,
                exchange_ts=NOW.replace(second=i),
                received_at=NOW.replace(second=i),
            ),
            provenance=EventProvenance(source="kalshi-md", channel="trade"),
        )
        store.append_normalized(ev)
    store.flush()

    events = list(store.read_normalized())
    assert [e.event_id for e in events] == [EventId(f"t-{i:03d}") for i in range(10)]


def test_filter_by_source(tmp_path: Path) -> None:
    store = ParquetEventStore(tmp_path)
    store.append(_envelope("trade", {"market_id": "M-1", "price": "0.5", "quantity": "1"}, source="kalshi-md"))
    store.append(_envelope("trade", {"market_id": "M-1", "price": "0.5", "quantity": "1"}, source="nws"))
    store.flush()

    kalshi_only = list(store.read(source="kalshi-md"))
    assert len(kalshi_only) == 1
    assert kalshi_only[0].source == "kalshi-md"


def _fill_event() -> OwnFillEvent:
    fill = Fill(
        fill_id=FillId("f-1"),
        venue_order_id=VenueOrderId("vo-1"),
        client_order_id=ClientOrderId("co-1"),
        instrument_id=INSTR,
        outcome_side=OutcomeSide.YES,
        order_side=OrderSide.BUY,
        price=Decimal("0.42"),
        quantity=Decimal("10"),
        liquidity=Liquidity.TAKER,
        fee_amount=Decimal("0.18"),
        fee_currency="USD",
        filled_at=NOW,
        exchange_ts=NOW,
        correlation_id=CorrelationId("c-1"),
        strategy_id=StrategyId("s-1"),
        sleeve_id=SleeveId("sl-1"),
    )
    return OwnFillEvent(
        event_id=EventId("of-1"),
        fill=fill,
        provenance=EventProvenance(source="oms", channel="own_fill", venue=Venue.KALSHI),
    )


def _order_update_event() -> OwnOrderUpdateEvent:
    order = Order(
        client_order_id=ClientOrderId("co-2"),
        venue_order_id=VenueOrderId("vo-2"),
        instrument_id=INSTR,
        outcome_side=OutcomeSide.YES,
        order_side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        price=Decimal("0.40"),
        quantity=Decimal("100"),
        filled_quantity=Decimal("25"),
        status=OrderStatus.PARTIALLY_FILLED,
        created_at=NOW,
        updated_at=NOW,
        correlation_id=CorrelationId("c-2"),
        strategy_id=StrategyId("s-1"),
        sleeve_id=SleeveId("sl-1"),
    )
    return OwnOrderUpdateEvent(
        event_id=EventId("ou-1"),
        order=order,
        provenance=EventProvenance(source="oms", channel="own_order", venue=Venue.KALSHI),
    )


def _order_reject_event() -> OwnOrderRejectEvent:
    reject = OrderReject(
        client_order_id=ClientOrderId("co-3"),
        reason="risk: max_order_notional",
        rejected_at=NOW,
        venue_code="LIMIT_EXCEEDED",
    )
    return OwnOrderRejectEvent(
        event_id=EventId("or-1"),
        reject=reject,
        provenance=EventProvenance(source="risk", channel="reject"),
    )


def test_own_events_round_trip(tmp_path: Path) -> None:
    store = ParquetEventStore(tmp_path)
    store.append_normalized(_fill_event())
    store.append_normalized(_order_update_event())
    store.append_normalized(_order_reject_event())
    store.flush()

    events = list(store.read_normalized())
    kinds = {type(event).__name__ for event in events}
    assert kinds == {"OwnFillEvent", "OwnOrderUpdateEvent", "OwnOrderRejectEvent"}

    by_id = {str(event.event_id): event for event in events}

    fill_event = by_id["of-1"]
    assert isinstance(fill_event, OwnFillEvent)
    assert fill_event.fill.price == Decimal("0.42")
    assert fill_event.fill.fee_amount == Decimal("0.18")
    assert fill_event.fill.liquidity is Liquidity.TAKER

    order_event = by_id["ou-1"]
    assert isinstance(order_event, OwnOrderUpdateEvent)
    assert order_event.order.status is OrderStatus.PARTIALLY_FILLED
    assert order_event.order.filled_quantity == Decimal("25")

    reject_event = by_id["or-1"]
    assert isinstance(reject_event, OwnOrderRejectEvent)
    assert reject_event.reject.venue_code == "LIMIT_EXCEEDED"
