"""In-memory port implementations for tests, backtests, and local experiments.

These let you wire up an end-to-end loop without any I/O. Production runners
swap them for NATS, S3, ONNX, etc. They live under ``eventcontracts.testing``
so production import paths cannot reach them by accident.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from eventcontracts.domain.decisions import IntentEnvelope
from eventcontracts.domain.events import NormalizedEvent
from eventcontracts.domain.features import FeatureVector, Prediction
from eventcontracts.domain.ids import (
    ModelName,
    ModelVersion,
    SleeveId,
    StrategyId,
)
from eventcontracts.domain.models import InstrumentId, OutcomeSide
from eventcontracts.domain.orders import Order
from eventcontracts.domain.positions import CashBalance, Exposure, Position
from eventcontracts.runner.ports import RiskDecision
from eventcontracts.strategy.context import StrategyContext


@dataclass
class InMemoryEventSource:
    events: Sequence[NormalizedEvent]

    def stream(self) -> Iterator[NormalizedEvent]:
        yield from self.events


@dataclass
class InMemoryIntentSink:
    emitted: list[IntentEnvelope] = field(default_factory=list)

    def emit(self, envelope: IntentEnvelope) -> None:
        self.emitted.append(envelope)


@dataclass
class InMemoryStateStore:
    blobs: dict[str, bytes] = field(default_factory=dict)

    def save(self, strategy_id: StrategyId, state: bytes) -> None:
        self.blobs[str(strategy_id)] = state

    def load(self, strategy_id: StrategyId) -> bytes | None:
        return self.blobs.get(str(strategy_id))


@dataclass
class InMemoryClock:
    current: datetime = field(
        default_factory=lambda: datetime(2026, 1, 1, tzinfo=UTC)
    )

    def now(self) -> datetime:
        return self.current

    def advance(self, *, seconds: float = 0.0) -> None:
        self.current = self.current + timedelta(seconds=seconds)


@dataclass
class AllowAllRiskGate:
    def evaluate(
        self, envelope: IntentEnvelope, ctx: StrategyContext
    ) -> RiskDecision:
        return RiskDecision(allowed=True)


@dataclass
class InMemoryContext:
    """A minimal ``StrategyContext`` implementation backed by plain dicts.

    Intended for tests and for backtests where the position keeper and feature
    store are running in-process. Production sleeves implement a context that
    reads from the actual keepers/stores.
    """

    strategy_id_value: StrategyId
    sleeve_id_value: SleeveId
    clock_now: datetime
    positions_by_key: dict[tuple[InstrumentId, OutcomeSide], Position] = field(
        default_factory=dict
    )
    cash_by_ccy: dict[str, CashBalance] = field(default_factory=dict)
    open_order_list: list[Order] = field(default_factory=list)
    features: FeatureVector | None = None
    exposure_snapshot: Exposure | None = None

    @property
    def now(self) -> datetime:
        return self.clock_now

    @property
    def strategy_id(self) -> StrategyId:
        return self.strategy_id_value

    @property
    def sleeve_id(self) -> SleeveId:
        return self.sleeve_id_value

    def position(
        self, instrument_id: InstrumentId, side: OutcomeSide
    ) -> Position | None:
        return self.positions_by_key.get((instrument_id, side))

    def positions(self) -> Sequence[Position]:
        return tuple(self.positions_by_key.values())

    def cash(self, currency: str) -> CashBalance:
        if currency not in self.cash_by_ccy:
            self.cash_by_ccy[currency] = CashBalance(
                currency=currency,
                total=Decimal("0"),
                available=Decimal("0"),
                held_for_orders=Decimal("0"),
                settling=Decimal("0"),
                updated_at=self.clock_now,
            )
        return self.cash_by_ccy[currency]

    def exposure(self) -> Exposure:
        if self.exposure_snapshot is None:
            return Exposure(
                sleeve_id=self.sleeve_id_value,
                currency="USD",
                gross_notional=Decimal("0"),
                net_notional=Decimal("0"),
                long_notional=Decimal("0"),
                short_notional=Decimal("0"),
                updated_at=self.clock_now,
            )
        return self.exposure_snapshot

    def open_orders(self) -> Sequence[Order]:
        return tuple(self.open_order_list)

    def feature(self, name: str) -> float | None:
        if self.features is None:
            return None
        return self.features.get(name)

    def feature_vector(self) -> FeatureVector | None:
        return self.features

    def predict(self, model_name: str, features: FeatureVector) -> Prediction:
        # In-process placeholder: returns a constant prediction so that tests
        # can wire the full path. Real contexts call the model loader.
        return Prediction(
            model_name=ModelName(model_name),
            model_version=ModelVersion("inmemory"),
            instrument_id=features.instrument_id,
            timestamp=self.clock_now,
            horizon_seconds=0,
            value=0.0,
            confidence=None,
        )


@dataclass
class StaticContextProvider:
    """Returns the same ``StrategyContext`` each call.

    Tests use this to keep the loop deterministic. Production providers build
    a fresh context every event from the latest position keeper snapshot.
    """

    ctx: StrategyContext

    def context(self) -> StrategyContext:
        return self.ctx


def collect(events: Iterable[NormalizedEvent]) -> InMemoryEventSource:
    return InMemoryEventSource(events=tuple(events))
