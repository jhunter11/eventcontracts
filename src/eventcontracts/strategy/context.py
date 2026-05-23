"""Read-only view of the world that strategies receive on every callback.

A strategy must never mutate ``StrategyContext`` directly. Anything that needs
to change goes through a ``StrategyDecision`` returned from the callback —
that keeps the strategy pure-ish and makes its behavior easy to replay.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from eventcontracts.domain.features import FeatureVector, Prediction
from eventcontracts.domain.ids import SleeveId, StrategyId
from eventcontracts.domain.models import InstrumentId, OutcomeSide
from eventcontracts.domain.orders import Order
from eventcontracts.domain.positions import CashBalance, Exposure, Position


class StrategyContext(Protocol):
    @property
    def now(self) -> datetime: ...

    @property
    def strategy_id(self) -> StrategyId: ...

    @property
    def sleeve_id(self) -> SleeveId: ...

    def position(
        self, instrument_id: InstrumentId, side: OutcomeSide
    ) -> Position | None: ...

    def positions(self) -> Sequence[Position]: ...

    def cash(self, currency: str) -> CashBalance: ...

    def exposure(self) -> Exposure: ...

    def open_orders(self) -> Sequence[Order]: ...

    def feature(self, name: str) -> float | None: ...

    def feature_vector(self) -> FeatureVector | None: ...

    def predict(self, model_name: str, features: FeatureVector) -> Prediction: ...
