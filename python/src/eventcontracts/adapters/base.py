"""Common venue adapter interfaces."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Protocol

from eventcontracts.domain.decisions import IntentEnvelope
from eventcontracts.domain.fees import FeeModel
from eventcontracts.domain.fills import Fill
from eventcontracts.domain.lifecycle import MarketLifecycleEvent, SettlementEvent
from eventcontracts.domain.models import InstrumentId, Market, OrderBook, Trade, Venue
from eventcontracts.domain.orders import Order, OrderReject
from eventcontracts.execution.simulator import OrderIntent
from eventcontracts.storage.interfaces import EventEnvelope


@dataclass(frozen=True)
class VenueCapabilities:
    venue: Venue
    supports_queue_position: bool
    supports_onchain_fill_join: bool
    supports_fix: bool
    websocket_auth_required: bool


class VenueMarketDataClient(Protocol):
    async def list_markets(self) -> list[Market]:
        """Return discoverable markets."""

    async def get_order_book(self, instrument_id: InstrumentId) -> OrderBook:
        """Return a point-in-time order book."""

    async def stream_order_books(self) -> AsyncIterator[OrderBook]:
        """Yield order-book updates."""

    async def stream_trades(self) -> AsyncIterator[Trade]:
        """Yield trade updates."""

    async def stream_lifecycle(self) -> AsyncIterator[MarketLifecycleEvent]:
        """Yield market lifecycle updates."""

    async def historical_envelopes(
        self, instrument_id: InstrumentId, start: str, end: str
    ) -> AsyncIterator[EventEnvelope]:
        """Yield raw historical envelopes for a venue date range."""


class VenueReferenceDataClient(Protocol):
    async def get_market(self, instrument_id: InstrumentId) -> Market:
        """Return normalized market metadata."""

    async def settlement_rules(self, instrument_id: InstrumentId) -> dict[str, str]:
        """Return venue-specific settlement and resolution rule metadata."""

    async def stream_settlements(self) -> AsyncIterator[SettlementEvent]:
        """Yield settlement and resolution updates."""


class VenueExecutionClient(Protocol):
    async def submit_order(
        self, intent: OrderIntent, envelope: IntentEnvelope
    ) -> Order | OrderReject:
        """Submit one venue-bound order intent."""

    async def cancel_order(self, order: Order, reason: str) -> Order | OrderReject:
        """Cancel a live venue order."""

    async def replace_order(
        self, order: Order, new_intent: OrderIntent
    ) -> Order | OrderReject:
        """Replace a live venue order."""

    async def open_orders(self) -> Sequence[Order]:
        """Return live open orders visible at the venue."""

    async def stream_fills(self) -> AsyncIterator[Fill]:
        """Yield own-account fills from venue private feeds."""


class VenueAdapter(Protocol):
    capabilities: VenueCapabilities
    market_data: VenueMarketDataClient
    reference_data: VenueReferenceDataClient
    execution: VenueExecutionClient
    fee_model: FeeModel
