"""Ledger storage and deterministic settlement accounting."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from eventcontracts.audit import AuditStamp, audit_stamp_for
from eventcontracts.domain import InstrumentId, OutcomeSide, SleeveId, Venue
from eventcontracts.domain.lifecycle import SettlementEvent
from eventcontracts.domain.positions import LedgerEntry, Position
from eventcontracts.ledger import (
    InMemoryLedgerStore,
    InMemorySettlementAccounting,
    JsonlLedgerStore,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)
INSTR = InstrumentId(venue=Venue.KALSHI, market_id="M-1")
SLEEVE = SleeveId("sleeve-a")


def _entry(entry_id: str, sleeve_id: SleeveId = SLEEVE) -> LedgerEntry:
    return LedgerEntry(
        entry_id=entry_id,
        sleeve_id=sleeve_id,
        timestamp=NOW,
        kind="cash",
        instrument_id=INSTR,
        amount=Decimal("12.34"),
        currency="USD",
        correlation_id=None,
        notes="fixture",
    )


def _audit(entry_id: str) -> AuditStamp:
    return audit_stamp_for(
        {"entry_id": entry_id},
        object_id=f"ledger:{entry_id}",
        object_kind="ledger_entry",
        schema_version="ledger-v1",
        produced_at=NOW,
        producer="pytest",
    )


def test_in_memory_ledger_store_filters_by_sleeve() -> None:
    store = InMemoryLedgerStore()
    store.append(_entry("a"), _audit("a"))
    store.append(_entry("b", SleeveId("sleeve-b")), _audit("b"))

    assert [entry.entry_id for entry in store.read_entries(sleeve_id=str(SLEEVE))] == ["a"]


def test_jsonl_ledger_store_round_trips_entries(tmp_path: Path) -> None:
    store = JsonlLedgerStore(tmp_path / "ledger.jsonl")
    store.append(_entry("a"), _audit("a"))

    loaded = JsonlLedgerStore(tmp_path / "ledger.jsonl").read_entries(sleeve_id=str(SLEEVE))

    assert len(loaded) == 1
    assert loaded[0] == _entry("a")


def test_settlement_accounting_is_idempotent_and_pays_winning_side() -> None:
    accounting = InMemorySettlementAccounting(
        sleeve_id=SLEEVE,
        currency="USD",
        positions=(
            Position(
                instrument_id=INSTR,
                outcome_side=OutcomeSide.YES,
                quantity=Decimal("3"),
                average_price=Decimal("0.40"),
                realized_pnl=Decimal("0"),
                unrealized_pnl=Decimal("0"),
                updated_at=NOW,
            ),
            Position(
                instrument_id=INSTR,
                outcome_side=OutcomeSide.NO,
                quantity=Decimal("2"),
                average_price=Decimal("0.60"),
                realized_pnl=Decimal("0"),
                unrealized_pnl=Decimal("0"),
                updated_at=NOW,
            ),
        ),
    )
    settlement = SettlementEvent(
        instrument_id=INSTR,
        resolved_side=OutcomeSide.YES,
        payout_per_contract=Decimal("1"),
        currency="USD",
        settled_at=NOW,
        source="venue",
    )

    first = accounting.finalize(settlement)
    second = accounting.finalize(settlement)

    assert [entry.amount for entry in first] == [Decimal("3"), Decimal("0")]
    assert second == ()
