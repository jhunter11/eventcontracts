"""Typed configuration helpers."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from eventcontracts.domain.events import EVENT_KINDS
from eventcontracts.domain.ids import (
    FeatureSchemaId,
    ModelName,
    ModelVersion,
    SleeveId,
    StrategyId,
)
from eventcontracts.domain.latency import ExecutionPriority, LatencyTier
from eventcontracts.domain.models import Venue
from eventcontracts.domain.spec import (
    EventSubscription,
    ModelRef,
    RiskProfile,
    SleeveSpec,
    StrategySpec,
)

ConfigModel = TypeVar("ConfigModel", bound=BaseModel)
ParameterValue = str | int | float | bool


class StrictConfig(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        protected_namespaces=(),
    )


class ExecutionPriorityConfig(StrictConfig):
    tier: LatencyTier = LatencyTier.STANDARD
    max_delay_ms: int | None = None
    expires_after_ms: int | None = None
    allow_rate_limit_borrow: bool = False
    reason: str = ""

    def to_domain(self) -> ExecutionPriority:
        return ExecutionPriority(
            tier=self.tier,
            max_delay_ms=self.max_delay_ms,
            expires_after_ms=self.expires_after_ms,
            allow_rate_limit_borrow=self.allow_rate_limit_borrow,
            reason=self.reason,
        )


class ModelRefConfig(StrictConfig):
    name: str
    version: str
    artifact_uri: str
    sha256: str

    def to_domain(self) -> ModelRef:
        return ModelRef(
            name=ModelName(self.name),
            version=ModelVersion(self.version),
            artifact_uri=self.artifact_uri,
            sha256=self.sha256,
        )


class EventSubscriptionConfig(StrictConfig):
    venues: tuple[Venue, ...]
    instrument_patterns: tuple[str, ...]
    event_kinds: tuple[str, ...]
    external_sources: tuple[str, ...] = ()

    @field_validator("event_kinds")
    @classmethod
    def validate_event_kinds(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        unknown = tuple(kind for kind in value if kind not in EVENT_KINDS)
        if unknown:
            raise ValueError(f"unknown event kind(s): {unknown}")
        return value

    def to_domain(self) -> EventSubscription:
        return EventSubscription(
            venues=self.venues,
            instrument_patterns=self.instrument_patterns,
            event_kinds=self.event_kinds,
            external_sources=self.external_sources,
        )


class StrategySpecConfig(StrictConfig):
    strategy_id: str
    name: str
    version: str
    description: str
    subscription: EventSubscriptionConfig
    default_execution_priority: ExecutionPriorityConfig = ExecutionPriorityConfig()
    parameters: Mapping[str, ParameterValue] = Field(default_factory=dict)
    model: ModelRefConfig | None = None
    feature_schema_id: str | None = None
    tags: Mapping[str, str] = Field(default_factory=dict)

    def to_domain(self) -> StrategySpec:
        return StrategySpec(
            strategy_id=StrategyId(self.strategy_id),
            name=self.name,
            version=self.version,
            description=self.description,
            subscription=self.subscription.to_domain(),
            default_execution_priority=self.default_execution_priority.to_domain(),
            parameters=self.parameters,
            model=self.model.to_domain() if self.model is not None else None,
            feature_schema_id=(
                FeatureSchemaId(self.feature_schema_id)
                if self.feature_schema_id is not None
                else None
            ),
            tags=self.tags,
        )


class RiskProfileConfig(StrictConfig):
    max_order_notional: Decimal
    max_position_notional: Decimal
    max_daily_loss: Decimal
    max_open_orders: int
    max_gross_exposure: Decimal
    currency: str

    def to_domain(self) -> RiskProfile:
        return RiskProfile(
            max_order_notional=self.max_order_notional,
            max_position_notional=self.max_position_notional,
            max_daily_loss=self.max_daily_loss,
            max_open_orders=self.max_open_orders,
            max_gross_exposure=self.max_gross_exposure,
            currency=self.currency,
        )


class SleeveSpecConfig(StrictConfig):
    sleeve_id: str
    strategy_id: str
    strategy_version: str
    venue: Venue
    capital_allocation: Decimal
    currency: str
    risk: RiskProfileConfig
    tags: Mapping[str, str] = Field(default_factory=dict)

    def to_domain(self) -> SleeveSpec:
        return SleeveSpec(
            sleeve_id=SleeveId(self.sleeve_id),
            strategy_id=StrategyId(self.strategy_id),
            strategy_version=self.strategy_version,
            venue=self.venue,
            capital_allocation=self.capital_allocation,
            currency=self.currency,
            risk=self.risk.to_domain(),
            tags=self.tags,
        )


class StoragePathConfig(StrictConfig):
    path: str
    format: str
    schema_version: str | None = None


class ReplayConfig(StrictConfig):
    path: str
    checkpoint_interval_seconds: int


class StorageConfig(StrictConfig):
    raw: StoragePathConfig
    normalized: StoragePathConfig
    replay: ReplayConfig


class VenueConfig(StrictConfig):
    venue: Mapping[str, Any]
    market_data: Mapping[str, Any]
    execution: Mapping[str, Any]
    fees: Mapping[str, Any]

    @field_validator("venue")
    @classmethod
    def validate_venue_name(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        name = value.get("name")
        if name is None:
            raise ValueError("venue.name is required")
        Venue(str(name))
        return value


def load_toml(path: str | Path) -> dict[str, Any]:
    """Load a TOML file with a clear error if it is missing."""

    resolved = Path(path).expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"config file not found: {resolved}")

    with resolved.open("rb") as config_file:
        return tomllib.load(config_file)


def load_typed_toml(path: str | Path, model: type[ConfigModel]) -> ConfigModel:
    return model.model_validate(load_toml(path))


def load_strategy_spec(path: str | Path) -> StrategySpec:
    return load_typed_toml(path, StrategySpecConfig).to_domain()


def load_sleeve_spec(path: str | Path) -> SleeveSpec:
    return load_typed_toml(path, SleeveSpecConfig).to_domain()


def load_storage_config(path: str | Path) -> StorageConfig:
    return load_typed_toml(path, StorageConfig)


def load_venue_config(path: str | Path) -> VenueConfig:
    return load_typed_toml(path, VenueConfig)
