"""Stateful strategy context provider.

The provider is a small in-process portfolio view for paper/replay runners.
It implements the same read-only ``StrategyContext`` surface strategies see,
while accepting fills from the execution simulator so subsequent strategy
callbacks see their updated positions, cash, and exposure.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from eventcontracts.domain.decisions import IntentEnvelope, PlaceOrder
from eventcontracts.domain.events import (
    NormalizedEvent,
    OwnFillEvent,
    OwnOrderRejectEvent,
    OwnOrderUpdateEvent,
)
from eventcontracts.domain.features import FeatureVector, Prediction
from eventcontracts.domain.fills import Fill
from eventcontracts.domain.ids import ModelName, ModelVersion, SleeveId, StrategyId
from eventcontracts.domain.models import InstrumentId, OutcomeSide
from eventcontracts.domain.orders import Order, OrderSide, OrderStatus
from eventcontracts.domain.positions import CashBalance, Exposure, Position
from eventcontracts.execution.pnl import PnLTracker
from eventcontracts.models.pipeline import ModelRunner
from eventcontracts.strategy.context import StrategyContext


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


@dataclass(frozen=True)
class StatefulContext(StrategyContext):
    strategy_id_value: StrategyId
    sleeve_id_value: SleeveId
    clock_now: datetime
    positions_by_key: Mapping[tuple[InstrumentId, OutcomeSide], Position]
    cash_by_ccy: Mapping[str, CashBalance]
    exposure_snapshot: Exposure
    open_order_list: Sequence[Order] = ()
    features: FeatureVector | None = None
    model_runner: ModelRunner | None = None

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
        self,
        instrument_id: InstrumentId,
        side: OutcomeSide,
    ) -> Position | None:
        return self.positions_by_key.get((instrument_id, side))

    def positions(self) -> Sequence[Position]:
        return tuple(self.positions_by_key.values())

    def cash(self, currency: str) -> CashBalance:
        balance = self.cash_by_ccy.get(currency)
        if balance is not None:
            return balance
        return CashBalance(
            currency=currency,
            total=Decimal("0"),
            available=Decimal("0"),
            held_for_orders=Decimal("0"),
            settling=Decimal("0"),
            updated_at=self.clock_now,
        )

    def exposure(self) -> Exposure:
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
        if self.model_runner is not None:
            return self.model_runner.predict(ModelName(model_name), features)
        return Prediction(
            model_name=ModelName(model_name),
            model_version=ModelVersion("stateful-context"),
            instrument_id=features.instrument_id,
            timestamp=self.clock_now,
            horizon_seconds=0,
            value=0.0,
            confidence=None,
        )


@dataclass
class StatefulContextProvider:
    strategy_id: StrategyId
    sleeve_id: SleeveId
    currency: str
    starting_cash: Decimal
    clock: Callable[[], datetime] = _utcnow
    pnl_tracker: PnLTracker = field(default_factory=PnLTracker)
    model_runner: ModelRunner | None = None
    features: FeatureVector | None = None
    open_order_list: list[Order] = field(default_factory=list)
    reservations_by_client_order_id: dict[str, Decimal] = field(default_factory=dict)
    cash_by_ccy: dict[str, CashBalance] = field(init=False)

    def __post_init__(self) -> None:
        now = self.clock()
        self.cash_by_ccy = {
            self.currency: CashBalance(
                currency=self.currency,
                total=self.starting_cash,
                available=self.starting_cash,
                held_for_orders=Decimal("0"),
                settling=Decimal("0"),
                updated_at=now,
            )
        }

    def context(self) -> StrategyContext:
        now = self.clock()
        positions = {
            (position.instrument_id, position.outcome_side): position
            for position in self.pnl_tracker.positions(now=now)
        }
        return StatefulContext(
            strategy_id_value=self.strategy_id,
            sleeve_id_value=self.sleeve_id,
            clock_now=now,
            positions_by_key=positions,
            cash_by_ccy=dict(self.cash_by_ccy),
            exposure_snapshot=self._exposure(tuple(positions.values()), now),
            open_order_list=tuple(self.open_order_list),
            features=self.features,
            model_runner=self.model_runner,
        )

    def on_fill(self, fill: Fill) -> None:
        self.pnl_tracker.on_fill(fill)
        self._apply_fill_cash(fill)

    def on_event(self, event: NormalizedEvent) -> None:
        if isinstance(event, OwnFillEvent):
            self.on_fill(event.fill)
            return
        if isinstance(event, OwnOrderUpdateEvent):
            self.on_order_update(event.order)
            return
        if isinstance(event, OwnOrderRejectEvent):
            self.release_order(event.reject.client_order_id, at=event.reject.rejected_at)
            self.open_order_list = [
                order for order in self.open_order_list if order.client_order_id != event.reject.client_order_id
            ]
            return
        self.pnl_tracker.on_event(event)

    def on_order_update(self, order: Order) -> None:
        """Apply an authoritative private order update to cash/open-order state."""

        self._upsert_open_order(order)
        if order.status in (
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        ):
            self.release_order(order.client_order_id, at=order.updated_at)
            return
        self._ensure_order_reservation(order)

    def reserve_intent(self, envelope: IntentEnvelope) -> tuple[str, ...]:
        decision = envelope.decision
        if not isinstance(decision, PlaceOrder) or decision.order_side is not OrderSide.BUY:
            return ()
        client_order_id = str(decision.client_order_id)
        notional = (decision.price if decision.price is not None else Decimal("1")) * decision.quantity
        if client_order_id in self.reservations_by_client_order_id:
            return ()
        return self._reserve(client_order_id, notional, at=envelope.emitted_at)

    def release_order(self, client_order_id: object, *, at: datetime | None = None) -> Decimal:
        key = str(client_order_id)
        remaining = self.reservations_by_client_order_id.pop(key, Decimal("0"))
        if remaining <= 0:
            return Decimal("0")
        self._release_cash(remaining, at=at or self.clock())
        return remaining

    def set_feature_vector(self, features: FeatureVector | None) -> None:
        self.features = features

    def set_open_orders(self, orders: Sequence[Order]) -> None:
        self.open_order_list = list(orders)

    def _apply_fill_cash(self, fill: Fill) -> None:
        balance = self.cash_by_ccy.get(fill.fee_currency)
        if balance is None:
            balance = CashBalance(
                currency=fill.fee_currency,
                total=Decimal("0"),
                available=Decimal("0"),
                held_for_orders=Decimal("0"),
                settling=Decimal("0"),
                updated_at=fill.filled_at,
            )
        notional = fill.price * fill.quantity
        reserved_used = Decimal("0")
        if fill.order_side is OrderSide.BUY:
            reserved_used = self._consume_reservation(
                fill.client_order_id,
                notional,
                at=fill.filled_at,
            )
            balance = self.cash_by_ccy.get(fill.fee_currency, balance)
        delta = -(notional + fill.fee_amount) if fill.order_side is OrderSide.BUY else notional - fill.fee_amount
        total = max(balance.total + delta, Decimal("0"))
        available_delta = -fill.fee_amount if fill.order_side is OrderSide.BUY and reserved_used > 0 else delta
        available = max(balance.available + available_delta, Decimal("0"))
        self.cash_by_ccy[fill.fee_currency] = CashBalance(
            currency=fill.fee_currency,
            total=total,
            available=available,
            held_for_orders=balance.held_for_orders,
            settling=balance.settling,
            updated_at=fill.filled_at,
        )

    def _reserve(self, client_order_id: str, notional: Decimal, *, at: datetime) -> tuple[str, ...]:
        if notional <= 0:
            return ()
        balance = self.cash_by_ccy[self.currency]
        if balance.available < notional:
            return ("available_cash",)
        self.reservations_by_client_order_id[client_order_id] = notional
        self.cash_by_ccy[self.currency] = CashBalance(
            currency=balance.currency,
            total=balance.total,
            available=balance.available - notional,
            held_for_orders=balance.held_for_orders + notional,
            settling=balance.settling,
            updated_at=at,
        )
        return ()

    def _consume_reservation(
        self,
        client_order_id: object,
        amount: Decimal,
        *,
        at: datetime,
    ) -> Decimal:
        key = str(client_order_id)
        reserved = self.reservations_by_client_order_id.get(key, Decimal("0"))
        consumed = min(reserved, amount)
        if consumed <= 0:
            return Decimal("0")
        remaining = reserved - consumed
        if remaining > 0:
            self.reservations_by_client_order_id[key] = remaining
        else:
            self.reservations_by_client_order_id.pop(key, None)
        balance = self.cash_by_ccy[self.currency]
        self.cash_by_ccy[self.currency] = CashBalance(
            currency=balance.currency,
            total=balance.total,
            available=balance.available,
            held_for_orders=max(balance.held_for_orders - consumed, Decimal("0")),
            settling=balance.settling,
            updated_at=at,
        )
        return consumed

    def _release_cash(self, amount: Decimal, *, at: datetime) -> None:
        balance = self.cash_by_ccy[self.currency]
        self.cash_by_ccy[self.currency] = CashBalance(
            currency=balance.currency,
            total=balance.total,
            available=balance.available + amount,
            held_for_orders=max(balance.held_for_orders - amount, Decimal("0")),
            settling=balance.settling,
            updated_at=at,
        )

    def _ensure_order_reservation(self, order: Order) -> None:
        if order.order_side is not OrderSide.BUY or order.price is None:
            return
        remaining_qty = order.quantity - order.filled_quantity
        if remaining_qty <= 0:
            return
        needed = order.price * remaining_qty
        key = str(order.client_order_id)
        current = self.reservations_by_client_order_id.get(key, Decimal("0"))
        if current >= needed:
            return
        self._reserve(key, needed - current, at=order.updated_at)

    def _upsert_open_order(self, order: Order) -> None:
        terminal = {
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        }
        self.open_order_list = [
            existing for existing in self.open_order_list if existing.client_order_id != order.client_order_id
        ]
        if order.status not in terminal:
            self.open_order_list.append(order)

    def _exposure(
        self,
        positions: Sequence[Position],
        now: datetime,
    ) -> Exposure:
        long_notional = sum(
            (position.quantity * position.average_price for position in positions),
            Decimal("0"),
        )
        return Exposure(
            sleeve_id=self.sleeve_id,
            currency=self.currency,
            gross_notional=long_notional,
            net_notional=long_notional,
            long_notional=long_notional,
            short_notional=Decimal("0"),
            updated_at=now,
        )
