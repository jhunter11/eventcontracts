"""Shared timing, quote, valuation, and markout records for research ledgers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from eventcontracts.domain.validation import (
    require_aware_datetime,
    require_non_empty,
    require_optional_aware_datetime,
    require_probability_decimal,
)


def age_ms(source_ts: datetime | None, received_at: datetime) -> float | None:
    """Milliseconds between a source timestamp and local receipt."""

    require_optional_aware_datetime(source_ts, "source_ts")
    require_aware_datetime(received_at, "received_at")
    if source_ts is None:
        return None
    return max((received_at - source_ts).total_seconds() * 1000.0, 0.0)


def is_stale(age_ms_value: float | None, max_age_ms: float | None) -> bool:
    """Return True when an optional age exceeds an optional limit."""

    if age_ms_value is None or max_age_ms is None:
        return False
    return age_ms_value > max_age_ms


@dataclass(frozen=True)
class SourceStamp:
    """Source timestamp provenance for a non-venue or venue data point."""

    source: str
    source_ts: datetime | None
    received_at: datetime
    sequence: str | None = None
    raw_age_ms: float | None = None
    stale: bool = False
    stale_reason: str | None = None

    def __post_init__(self) -> None:
        require_non_empty(self.source, "source")
        require_optional_aware_datetime(self.source_ts, "source_ts")
        require_aware_datetime(self.received_at, "received_at")
        if self.sequence is not None:
            require_non_empty(self.sequence, "sequence")
        if self.raw_age_ms is not None and self.raw_age_ms < 0:
            raise ValueError("raw_age_ms must be non-negative")
        if self.stale and not self.stale_reason:
            raise ValueError("stale_reason must be set when stale is true")

    @classmethod
    def from_timestamps(
        cls,
        *,
        source: str,
        source_ts: datetime | None,
        received_at: datetime,
        max_age_ms: float | None = None,
        sequence: str | None = None,
    ) -> SourceStamp:
        computed_age = age_ms(source_ts, received_at)
        stale = is_stale(computed_age, max_age_ms)
        reason = f"age_ms>{max_age_ms:g}" if stale and max_age_ms is not None else None
        return cls(
            source=source,
            source_ts=source_ts,
            received_at=received_at,
            sequence=sequence,
            raw_age_ms=computed_age,
            stale=stale,
            stale_reason=reason,
        )


@dataclass(frozen=True)
class MarketQuoteSnapshot:
    """Research-facing top-of-book snapshot."""

    venue: str
    market_id: str
    ticker: str
    received_at: datetime
    yes_bid: Decimal | None
    yes_ask: Decimal | None
    no_bid: Decimal | None = None
    no_ask: Decimal | None = None
    bid_size: Decimal | None = None
    ask_size: Decimal | None = None
    quote_age_ms: float | None = None
    lifecycle_status: str | None = None

    def __post_init__(self) -> None:
        require_non_empty(self.venue, "venue")
        require_non_empty(self.market_id, "market_id")
        require_non_empty(self.ticker, "ticker")
        require_aware_datetime(self.received_at, "received_at")
        for name in ("yes_bid", "yes_ask", "no_bid", "no_ask"):
            value = getattr(self, name)
            if value is not None:
                require_probability_decimal(value, name)
        for name in ("bid_size", "ask_size"):
            value = getattr(self, name)
            if value is not None and value < Decimal("0"):
                raise ValueError(f"{name} must be non-negative")
        if self.quote_age_ms is not None and self.quote_age_ms < 0:
            raise ValueError("quote_age_ms must be non-negative")

    @property
    def yes_mid(self) -> Decimal | None:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return (self.yes_bid + self.yes_ask) / Decimal("2")

    @property
    def yes_spread(self) -> Decimal | None:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return self.yes_ask - self.yes_bid


@dataclass(frozen=True)
class ModelValuation:
    """A model fair value plus the exact features that produced it."""

    model_id: str
    schema_version: str
    market_id: str
    as_of: datetime
    fair_yes: Decimal
    fair_no: Decimal
    confidence: Decimal | None
    feature_hash: str
    feature_payload: Mapping[str, Any] = field(default_factory=dict)
    no_trade_reason: str | None = None

    def __post_init__(self) -> None:
        require_non_empty(self.model_id, "model_id")
        require_non_empty(self.schema_version, "schema_version")
        require_non_empty(self.market_id, "market_id")
        require_aware_datetime(self.as_of, "as_of")
        require_probability_decimal(self.fair_yes, "fair_yes")
        require_probability_decimal(self.fair_no, "fair_no")
        if self.confidence is not None:
            require_probability_decimal(self.confidence, "confidence")
        require_non_empty(self.feature_hash, "feature_hash")


@dataclass(frozen=True)
class EdgeEvaluation:
    """Post-cost executable-edge calculation for one side of a market."""

    market_id: str
    as_of: datetime
    side: str
    fair_price: Decimal
    executable_price: Decimal | None
    raw_edge: Decimal | None
    fee: Decimal | None
    spread_cost: Decimal | None
    net_edge: Decimal | None
    candidate: bool
    reason: str

    def __post_init__(self) -> None:
        require_non_empty(self.market_id, "market_id")
        require_aware_datetime(self.as_of, "as_of")
        if self.side not in {"YES", "NO"}:
            raise ValueError("side must be YES or NO")
        require_probability_decimal(self.fair_price, "fair_price")
        for name in ("executable_price", "fee", "spread_cost"):
            value = getattr(self, name)
            if value is not None and value < Decimal("0"):
                raise ValueError(f"{name} must be non-negative")
        require_non_empty(self.reason, "reason")


@dataclass(frozen=True)
class MarkoutPoint:
    """Quote or settlement state observed after a paper decision."""

    market_id: str
    decision_id: str
    offset_ms: int
    observed_at: datetime
    yes_bid: Decimal | None
    yes_ask: Decimal | None
    yes_mid: Decimal | None
    settlement_payout: Decimal | None = None

    def __post_init__(self) -> None:
        require_non_empty(self.market_id, "market_id")
        require_non_empty(self.decision_id, "decision_id")
        if self.offset_ms < 0:
            raise ValueError("offset_ms must be non-negative")
        require_aware_datetime(self.observed_at, "observed_at")
        for name in ("yes_bid", "yes_ask", "yes_mid", "settlement_payout"):
            value = getattr(self, name)
            if value is not None:
                require_probability_decimal(value, name)
