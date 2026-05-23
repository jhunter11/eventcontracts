"""Crypto reference-data adapters."""

from __future__ import annotations


class BinanceReferenceDataClient:
    """Placeholder for mark price, funding, and volatility inputs."""

    def stream_mark_prices(self) -> None:
        raise NotImplementedError
