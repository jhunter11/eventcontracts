"""Accounting scaffolds for fills, cash, positions, and settlements."""

from __future__ import annotations

from collections.abc import Sequence

from eventcontracts.domain.fills import Fill
from eventcontracts.domain.lifecycle import SettlementEvent
from eventcontracts.domain.models import InstrumentId, OutcomeSide
from eventcontracts.domain.positions import CashBalance, LedgerEntry, Position


class LedgerStore:
    """Append-only double-entry ledger storage boundary."""

    def append(self, entry: LedgerEntry) -> None:
        raise NotImplementedError

    def read_entries(self, *, sleeve_id: str) -> Sequence[LedgerEntry]:
        raise NotImplementedError


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
