"""Accounting scaffolds for fills, cash, positions, and settlements."""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from eventcontracts.audit import AuditStamp
from eventcontracts.domain.fills import Fill
from eventcontracts.domain.ids import CorrelationId, SleeveId
from eventcontracts.domain.lifecycle import SettlementEvent
from eventcontracts.domain.models import InstrumentId, OutcomeSide
from eventcontracts.domain.positions import CashBalance, LedgerEntry, Position
from eventcontracts.domain.serialization import to_primitive
from eventcontracts.domain.validation import require_currency, require_non_empty


class LedgerStore:
    """Append-only double-entry ledger storage boundary."""

    def append(self, entry: LedgerEntry, audit: AuditStamp) -> None:
        raise NotImplementedError

    def read_entries(self, *, sleeve_id: str) -> Sequence[LedgerEntry]:
        raise NotImplementedError


@dataclass
class InMemoryLedgerStore(LedgerStore):
    """Append-only in-memory ledger for paper and tests."""

    entries: list[tuple[LedgerEntry, AuditStamp]] = field(default_factory=list)

    def append(self, entry: LedgerEntry, audit: AuditStamp) -> None:
        self.entries.append((entry, audit))

    def read_entries(self, *, sleeve_id: str) -> Sequence[LedgerEntry]:
        require_non_empty(sleeve_id, "sleeve_id")
        return tuple(entry for entry, _ in self.entries if str(entry.sleeve_id) == sleeve_id)


class JsonlLedgerStore(LedgerStore):
    """Durable JSONL ledger store for local paper-live sleeves."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append(self, entry: LedgerEntry, audit: AuditStamp) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "entry": to_primitive(entry),
            "audit": to_primitive(audit),
        }
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, sort_keys=True) + "\n")

    def read_entries(self, *, sleeve_id: str) -> Sequence[LedgerEntry]:
        require_non_empty(sleeve_id, "sleeve_id")
        if not self.path.exists():
            return ()
        entries: list[LedgerEntry] = []
        with self.path.open("r", encoding="utf-8") as file:
            for line in file:
                if not line.strip():
                    continue
                record = json.loads(line)
                entry = _ledger_entry_from_dict(record["entry"])
                if str(entry.sleeve_id) == sleeve_id:
                    entries.append(entry)
        return tuple(entries)


class PositionKeeper:
    """Maintain positions from fills and settlement events."""

    def apply_fill(self, fill: Fill) -> tuple[LedgerEntry, ...]:
        raise NotImplementedError

    def apply_settlement(self, settlement: SettlementEvent) -> tuple[LedgerEntry, ...]:
        raise NotImplementedError

    def position(self, instrument_id: InstrumentId, side: OutcomeSide) -> Position | None:
        raise NotImplementedError

    def all_positions(self) -> Sequence[Position]:
        raise NotImplementedError


class CashKeeper:
    """Maintain cash, holds, and settling balances."""

    def reserve_for_order(self, amount: LedgerEntry) -> None:
        raise NotImplementedError

    def release_order_hold(self, amount: LedgerEntry) -> None:
        raise NotImplementedError

    def apply_fill_cash(self, fill: Fill) -> tuple[LedgerEntry, ...]:
        raise NotImplementedError

    def balance(self, currency: str) -> CashBalance:
        raise NotImplementedError


class SettlementAccounting:
    """Convert venue settlement outcomes into cash and position ledger entries."""

    def accrue(self, settlement: SettlementEvent) -> tuple[LedgerEntry, ...]:
        raise NotImplementedError

    def finalize(self, settlement: SettlementEvent) -> tuple[LedgerEntry, ...]:
        raise NotImplementedError


@dataclass
class InMemorySettlementAccounting(SettlementAccounting):
    """Settlement accounting over a supplied position snapshot.

    This is intentionally small and deterministic: paper-live callers pass the
    latest positions for one sleeve, and each settlement is idempotent by
    `(instrument_id, settled_at, source)`.
    """

    sleeve_id: SleeveId
    currency: str
    positions: Iterable[Position] = ()
    _finalized_settlements: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        require_non_empty(str(self.sleeve_id), "sleeve_id")
        require_currency(self.currency, "currency")
        self.positions = tuple(self.positions)

    def accrue(self, settlement: SettlementEvent) -> tuple[LedgerEntry, ...]:
        return ()

    def finalize(self, settlement: SettlementEvent) -> tuple[LedgerEntry, ...]:
        settlement_id = _settlement_id(settlement)
        if settlement_id in self._finalized_settlements:
            return ()
        self._finalized_settlements.add(settlement_id)
        entries: list[LedgerEntry] = []
        for position in self.positions:
            if position.instrument_id != settlement.instrument_id:
                continue
            payout = (
                position.quantity * settlement.payout_per_contract
                if position.outcome_side is settlement.resolved_side
                else Decimal("0")
            )
            entries.append(
                LedgerEntry(
                    entry_id=f"settlement:{settlement_id}:{position.outcome_side.value}",
                    sleeve_id=self.sleeve_id,
                    timestamp=settlement.settled_at,
                    kind="settlement_payout",
                    instrument_id=settlement.instrument_id,
                    amount=payout,
                    currency=self.currency,
                    correlation_id=None,
                    notes=f"resolved_side={settlement.resolved_side}",
                )
            )
        return tuple(entries)


def _settlement_id(settlement: SettlementEvent) -> str:
    return (
        f"{settlement.instrument_id.venue.value}:"
        f"{settlement.instrument_id.market_id}:"
        f"{settlement.settled_at.isoformat()}:"
        f"{settlement.source}"
    )


def _ledger_entry_from_dict(data: dict[str, object]) -> LedgerEntry:
    from eventcontracts.domain.models import InstrumentId, Venue

    instrument_data = data.get("instrument_id")
    instrument = None
    if isinstance(instrument_data, dict):
        instrument = InstrumentId(
            venue=Venue(str(instrument_data["venue"])),
            market_id=str(instrument_data["market_id"]),
            outcome_id=(
                str(instrument_data["outcome_id"])
                if instrument_data.get("outcome_id") is not None
                else None
            ),
        )
    return LedgerEntry(
        entry_id=str(data["entry_id"]),
        sleeve_id=SleeveId(str(data["sleeve_id"])),
        timestamp=datetime.fromisoformat(str(data["timestamp"]).replace("Z", "+00:00")),
        kind=str(data["kind"]),
        instrument_id=instrument,
        amount=Decimal(str(data["amount"])),
        currency=str(data["currency"]),
        correlation_id=(
            CorrelationId(str(data["correlation_id"]))
            if data.get("correlation_id") is not None
            else None
        ),
        notes=str(data.get("notes", "")),
    )
