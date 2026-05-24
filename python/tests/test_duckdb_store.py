"""DuckDB read path over Parquet partitions."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("duckdb")

from eventcontracts.domain.events import EventProvenance, TradeEvent
from eventcontracts.domain.ids import EventId
from eventcontracts.domain.models import InstrumentId, OutcomeSide, Trade, Venue
from eventcontracts.storage import DuckDbEventStore, EventEnvelope, ParquetEventStore

NOW = datetime(2026, 1, 15, 14, 30, tzinfo=UTC)
INSTR = InstrumentId(venue=Venue.KALSHI, market_id="M-1", outcome_id=None)


def _seed_store(root: Path) -> ParquetEventStore:
    store = ParquetEventStore(root)
    for i in range(5):
        store.append(
            EventEnvelope(
                venue=Venue.KALSHI,
                source="kalshi-md",
                channel="trade",
                received_at=NOW.replace(second=i),
                exchange_ts=NOW.replace(second=i),
                payload={"market_id": "M-1", "price": "0.5", "quantity": str(i + 1)},
                schema_version="raw-event-v1",
            )
        )
    for i in range(3):
        ev = TradeEvent(
            event_id=EventId(f"t-{i:03d}"),
            trade=Trade(
                instrument_id=INSTR,
                side=OutcomeSide.YES,
                price=Decimal("0.50"),
                quantity=Decimal(str(i + 1)),
                trade_id=None,
                exchange_ts=NOW.replace(second=i),
                received_at=NOW.replace(second=i),
            ),
            provenance=EventProvenance(source="kalshi-md", channel="trade", venue=Venue.KALSHI),
        )
        store.append_normalized(ev)
    store.flush()
    return store


def test_raw_count_matches_writes(tmp_path: Path) -> None:
    _seed_store(tmp_path)
    with DuckDbEventStore(tmp_path) as duck:
        assert duck.raw_count() == 5


def test_normalized_count_matches_writes(tmp_path: Path) -> None:
    _seed_store(tmp_path)
    with DuckDbEventStore(tmp_path) as duck:
        assert duck.normalized_count() == 3


def test_kinds_present(tmp_path: Path) -> None:
    _seed_store(tmp_path)
    with DuckDbEventStore(tmp_path) as duck:
        assert duck.kinds_present() == ["trade"]


def test_trades_returns_parsed_payload(tmp_path: Path) -> None:
    _seed_store(tmp_path)
    with DuckDbEventStore(tmp_path) as duck:
        rows = duck.trades(market_id="M-1")
        assert len(rows) == 3
        # Event id is at the top level of the canonical normalized JSON.
        assert rows[0]["payload"]["event_id"].startswith("t-")


def test_ad_hoc_query(tmp_path: Path) -> None:
    _seed_store(tmp_path)
    with DuckDbEventStore(tmp_path) as duck:
        rows = duck.query("SELECT COUNT(*) FROM raw_events WHERE channel = 'trade'")
        assert rows[0][0] == 5
