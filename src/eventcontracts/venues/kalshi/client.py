"""Kalshi market-data adapter placeholder."""

from __future__ import annotations

from collections.abc import AsyncIterator

from eventcontracts.domain.models import InstrumentId, Market, OrderBook, Trade


class KalshiMarketDataClient:
    """Adapter for Kalshi REST, WebSocket, historical, and queue-position APIs."""

    async def list_markets(self) -> list[Market]:
        raise NotImplementedError

    async def get_order_book(self, instrument_id: InstrumentId) -> OrderBook:
        raise NotImplementedError

    async def stream_order_books(self) -> AsyncIterator[OrderBook]:
        raise NotImplementedError
        yield

    async def stream_trades(self) -> AsyncIterator[Trade]:
        raise NotImplementedError
        yield
