"""Common venue adapter interfaces."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

from eventcontracts.domain.fees import FeeModel
from eventcontracts.domain.models import InstrumentId, Market, OrderBook, Trade, Venue


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


class VenueAdapter(Protocol):
    capabilities: VenueCapabilities
    market_data: VenueMarketDataClient
    fee_model: FeeModel
