"""Market detection and subscription matching tests."""

from __future__ import annotations

from datetime import UTC, datetime

from eventcontracts.domain import (
    EventSubscription,
    InstrumentId,
    Market,
    MarketStatus,
    Venue,
)
from eventcontracts.markets import (
    InMemoryMarketCatalog,
    MarketDetectionPolicy,
    SubscriptionMarketDetector,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _market(market_id: str, *, status: MarketStatus = MarketStatus.OPEN) -> Market:
    return Market(
        instrument_id=InstrumentId(venue=Venue.KALSHI, market_id=market_id),
        title=f"Weather market {market_id}",
        category="weather",
        status=status,
    )


def test_market_detector_matches_subscription_patterns() -> None:
    catalog = InMemoryMarketCatalog(
        [
            _market("WEATHER-NYC-HIGH-TEMP"),
            _market("ELECTION-2028"),
        ]
    )
    detector = SubscriptionMarketDetector(catalog, clock=lambda: NOW)
    subscription = EventSubscription(
        venues=(Venue.KALSHI,),
        instrument_patterns=("WEATHER-*",),
        event_kinds=("trade", "quote"),
    )

    candidates = detector.detect(subscription)

    assert len(candidates) == 1
    assert candidates[0].market.instrument_id.market_id == "WEATHER-NYC-HIGH-TEMP"
    assert candidates[0].audit.object_kind == "market_candidate"


def test_market_detector_filters_status_and_limits_results() -> None:
    catalog = InMemoryMarketCatalog(
        [
            _market("WEATHER-A"),
            _market("WEATHER-B"),
            _market("WEATHER-C", status=MarketStatus.CLOSED),
        ]
    )
    detector = SubscriptionMarketDetector(
        catalog,
        policy=MarketDetectionPolicy(max_markets=1),
        clock=lambda: NOW,
    )
    subscription = EventSubscription(
        venues=(Venue.KALSHI,),
        instrument_patterns=("WEATHER-*",),
        event_kinds=("trade",),
    )

    candidates = detector.detect(subscription)

    assert len(candidates) == 1
    assert candidates[0].market.status is MarketStatus.OPEN
