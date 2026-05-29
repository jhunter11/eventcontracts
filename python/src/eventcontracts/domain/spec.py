"""Static specs describing strategies, sleeves, models, and risk profiles.

These are the configuration values a runner needs to wire up a strategy.
Specs are loaded from TOML at startup and are immutable for the lifetime of
the process; changes require a roll-restart of the affected sleeve.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal

from eventcontracts.domain.events import EVENT_KINDS
from eventcontracts.domain.ids import (
    FeatureSchemaId,
    ModelName,
    ModelVersion,
    SleeveId,
    StrategyId,
)
from eventcontracts.domain.latency import DEFAULT_EXECUTION_PRIORITY, ExecutionPriority
from eventcontracts.domain.metadata import FrozenMap, freeze_mapping
from eventcontracts.domain.models import Venue
from eventcontracts.domain.validation import (
    require_currency,
    require_non_empty,
    require_non_negative_decimal,
    require_positive_decimal,
    require_positive_int,
)


@dataclass(frozen=True)
class ModelRef:
    name: ModelName
    version: ModelVersion
    artifact_uri: str
    sha256: str

    def __post_init__(self) -> None:
        require_non_empty(str(self.name), "model name")
        require_non_empty(str(self.version), "model version")
        require_non_empty(self.artifact_uri, "artifact_uri")
        require_non_empty(self.sha256, "sha256")


@dataclass(frozen=True)
class RiskProfile:
    max_order_notional: Decimal
    max_position_notional: Decimal
    max_daily_loss: Decimal
    max_open_orders: int
    max_gross_exposure: Decimal
    currency: str
    max_market_data_age_ms: int = 1000
    max_spread: Decimal = Decimal("1")
    max_order_lifetime_ms: int = 5000
    allow_market_orders: bool = False
    allow_unbounded_gtc: bool = False

    def __post_init__(self) -> None:
        require_positive_decimal(self.max_order_notional, "max_order_notional")
        require_positive_decimal(self.max_position_notional, "max_position_notional")
        require_non_negative_decimal(self.max_daily_loss, "max_daily_loss")
        require_positive_int(self.max_open_orders, "max_open_orders")
        require_positive_decimal(self.max_gross_exposure, "max_gross_exposure")
        require_currency(self.currency, "currency")
        require_positive_int(self.max_market_data_age_ms, "max_market_data_age_ms")
        require_positive_decimal(self.max_spread, "max_spread")
        require_positive_int(self.max_order_lifetime_ms, "max_order_lifetime_ms")


@dataclass(frozen=True)
class EventSubscription:
    venues: tuple[Venue, ...]
    instrument_patterns: tuple[str, ...]
    event_kinds: tuple[str, ...]
    external_sources: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.venues:
            raise ValueError("subscription venues must be non-empty")
        if not self.instrument_patterns:
            raise ValueError("subscription instrument_patterns must be non-empty")
        if not self.event_kinds:
            raise ValueError("subscription event_kinds must be non-empty")
        unknown = tuple(kind for kind in self.event_kinds if kind not in EVENT_KINDS)
        if unknown:
            raise ValueError(f"unknown event kind(s): {unknown}")
        for pattern in self.instrument_patterns:
            require_non_empty(pattern, "instrument pattern")
        for source in self.external_sources:
            require_non_empty(source, "external source")
        object.__setattr__(self, "venues", tuple(self.venues))
        object.__setattr__(self, "instrument_patterns", tuple(self.instrument_patterns))
        object.__setattr__(self, "event_kinds", tuple(self.event_kinds))
        object.__setattr__(self, "external_sources", tuple(self.external_sources))


@dataclass(frozen=True)
class StrategySpec:
    strategy_id: StrategyId
    name: str
    version: str
    description: str
    subscription: EventSubscription
    default_execution_priority: ExecutionPriority = DEFAULT_EXECUTION_PRIORITY
    parameters: Mapping[str, str | int | Decimal | bool] = field(default_factory=FrozenMap)
    model: ModelRef | None = None
    feature_schema_id: FeatureSchemaId | None = None
    tags: Mapping[str, str] = field(default_factory=FrozenMap)

    def __post_init__(self) -> None:
        require_non_empty(str(self.strategy_id), "strategy_id")
        require_non_empty(self.name, "name")
        require_non_empty(self.version, "version")
        if self.feature_schema_id is not None:
            require_non_empty(str(self.feature_schema_id), "feature_schema_id")
        object.__setattr__(self, "parameters", freeze_mapping(self.parameters))
        object.__setattr__(self, "tags", freeze_mapping(self.tags))


@dataclass(frozen=True)
class SleeveSpec:
    sleeve_id: SleeveId
    strategy_id: StrategyId
    strategy_version: str
    venue: Venue
    capital_allocation: Decimal
    currency: str
    risk: RiskProfile
    tags: Mapping[str, str] = field(default_factory=FrozenMap)

    def __post_init__(self) -> None:
        require_non_empty(str(self.sleeve_id), "sleeve_id")
        require_non_empty(str(self.strategy_id), "strategy_id")
        require_non_empty(self.strategy_version, "strategy_version")
        require_positive_decimal(self.capital_allocation, "capital_allocation")
        require_currency(self.currency, "currency")
        object.__setattr__(self, "tags", freeze_mapping(self.tags))
