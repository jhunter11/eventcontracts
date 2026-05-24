"""Kalshi venue adapter."""

from eventcontracts.adapters.venues.kalshi.client import (
    KalshiAuth,
    KalshiEnvironment,
    KalshiPublicClient,
    KalshiWebSocketClient,
    rest_envelope,
)
from eventcontracts.adapters.venues.kalshi.fees import KalshiFeeModel

KalshiMarketDataClient = KalshiPublicClient

__all__ = [
    "KalshiAuth",
    "KalshiEnvironment",
    "KalshiFeeModel",
    "KalshiMarketDataClient",
    "KalshiPublicClient",
    "KalshiWebSocketClient",
    "rest_envelope",
]
