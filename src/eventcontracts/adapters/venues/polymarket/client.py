"""Polymarket global market-data adapter placeholder."""

from __future__ import annotations

from collections.abc import AsyncIterator

from eventcontracts.domain.models import InstrumentId, Market, OrderBook, Trade


class PolymarketMarketDataClient:
    """Adapter for Gamma discovery, CLOB feeds, price history, and onchain fill joins."""

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
