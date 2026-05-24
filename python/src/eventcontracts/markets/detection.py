"""Market detection for strategy subscriptions.

The detector is intentionally metadata-only: it decides which markets a
strategy sleeve should consider, but it does not trade, subscribe to feeds, or
mutate framework state. That keeps strategy discovery and market discovery
plug-and-play while preserving the runner/gateway boundaries.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from fnmatch import fnmatchcase

from eventcontracts.audit import AuditStamp, audit_stamp_for
from eventcontracts.domain.models import InstrumentId, Market, MarketStatus, Venue
from eventcontracts.domain.spec import EventSubscription
from eventcontracts.domain.validation import (
    require_aware_datetime,
    require_non_empty,
    require_positive_int,
)


@dataclass(frozen=True)
class MarketDetectionPolicy:
    """Conservative filter for automatically detected markets."""

    allowed_statuses: tuple[MarketStatus, ...] = (MarketStatus.OPEN,)
    categories: tuple[str, ...] = ()
    max_markets: int | None = None

    def __post_init__(self) -> None:
        if not self.allowed_statuses:
            raise ValueError("allowed_statuses must be non-empty")
        if self.max_markets is not None:
            require_positive_int(self.max_markets, "max_markets")
        for category in self.categories:
            require_non_empty(category, "category")
        object.__setattr__(self, "allowed_statuses", tuple(self.allowed_statuses))
        object.__setattr__(self, "categories", tuple(self.categories))


@dataclass(frozen=True)
class MarketCandidate:
    """A market selected for a strategy subscription."""

    market: Market
    score: int
    reasons: tuple[str, ...]
    detected_at: datetime
    audit: AuditStamp

    def __post_init__(self) -> None:
        require_aware_datetime(self.detected_at, "detected_at")
        if not self.reasons:
            raise ValueError("market candidate reasons must be non-empty")
        object.__setattr__(self, "reasons", tuple(self.reasons))


class MarketCatalog:
    """Reference-data catalog used by market detection."""

    def list_markets(self, venue: Venue | None = None) -> Sequence[Market]:
        raise NotImplementedError

    def get(self, instrument_id: InstrumentId) -> Market | None:
        raise NotImplementedError


class InMemoryMarketCatalog(MarketCatalog):
    """Deterministic catalog for tests, replay, and paper market discovery."""

    def __init__(self, markets: Iterable[Market] = ()) -> None:
        self._markets: dict[InstrumentId, Market] = {}
        self.extend(markets)

    def upsert(self, market: Market) -> None:
        self._markets[market.instrument_id] = market

    def extend(self, markets: Iterable[Market]) -> None:
        for market in markets:
            self.upsert(market)

    def list_markets(self, venue: Venue | None = None) -> Sequence[Market]:
        markets = (
            market
            for market in self._markets.values()
            if venue is None or market.instrument_id.venue is venue
        )
        return tuple(sorted(markets, key=lambda market: _market_sort_key(market)))

    def get(self, instrument_id: InstrumentId) -> Market | None:
        return self._markets.get(instrument_id)


class SubscriptionMarketDetector:
    """Match a strategy subscription against available market metadata."""

    def __init__(
        self,
        catalog: MarketCatalog,
        *,
        policy: MarketDetectionPolicy | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.catalog = catalog
        self.policy = policy or MarketDetectionPolicy()
        self.clock = clock

    def detect(self, subscription: EventSubscription) -> tuple[MarketCandidate, ...]:
        now = self._now()
        candidates: list[MarketCandidate] = []
        for venue in subscription.venues:
            for market in self.catalog.list_markets(venue):
                candidate = self._candidate_for(subscription, market, now)
                if candidate is not None:
                    candidates.append(candidate)

        candidates.sort(
            key=lambda candidate: (
                -candidate.score,
                candidate.market.instrument_id.venue.value,
                candidate.market.instrument_id.market_id,
                candidate.market.instrument_id.outcome_id or "",
            )
        )
        if self.policy.max_markets is not None:
            candidates = candidates[: self.policy.max_markets]
        return tuple(candidates)

    def _candidate_for(
        self,
        subscription: EventSubscription,
        market: Market,
        detected_at: datetime,
    ) -> MarketCandidate | None:
        if market.status not in self.policy.allowed_statuses:
            return None
        if self.policy.categories and market.category not in self.policy.categories:
            return None

        score = 0
        reasons: list[str] = []
        for pattern in subscription.instrument_patterns:
            match_score = _pattern_score(pattern, market)
            if match_score > 0:
                score += match_score
                reasons.append(f"matched_pattern:{pattern}")

        if not reasons:
            return None

        if market.status is MarketStatus.OPEN:
            score += 10
            reasons.append("status_open")
        if market.category is not None:
            score += 1
            reasons.append(f"category:{market.category}")

        audit = audit_stamp_for(
            {
                "market_id": market.instrument_id.market_id,
                "outcome_id": market.instrument_id.outcome_id,
                "venue": market.instrument_id.venue.value,
                "subscription_venues": tuple(venue.value for venue in subscription.venues),
                "patterns": subscription.instrument_patterns,
                "score": score,
                "reasons": tuple(reasons),
            },
            object_id=(
                "market-detection:"
                f"{market.instrument_id.venue.value}:"
                f"{market.instrument_id.market_id}:"
                f"{detected_at.isoformat()}"
            ),
            object_kind="market_candidate",
            schema_version="market-detection-v1",
            produced_at=detected_at,
            producer="subscription_market_detector",
            parent_ids=(market.instrument_id.market_id,),
        )
        return MarketCandidate(
            market=market,
            score=score,
            reasons=tuple(reasons),
            detected_at=detected_at,
            audit=audit,
        )

    def _now(self) -> datetime:
        now = self.clock()
        require_aware_datetime(now, "market detector clock")
        return now


def _pattern_score(pattern: str, market: Market) -> int:
    market_id = market.instrument_id.market_id
    outcome_id = market.instrument_id.outcome_id or ""
    title = market.title
    category = market.category or ""
    haystack = (market_id, outcome_id, title, category)

    if pattern in (market_id, outcome_id):
        return 100
    if any(fnmatchcase(value, pattern) for value in haystack):
        return 50
    lowered = pattern.lower()
    if any(fnmatchcase(value.lower(), lowered) for value in haystack):
        return 40
    return 0


def _market_sort_key(market: Market) -> tuple[str, str, str]:
    return (
        market.instrument_id.venue.value,
        market.instrument_id.market_id,
        market.instrument_id.outcome_id or "",
    )
