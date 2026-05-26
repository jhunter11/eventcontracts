"""Execution simulator boundaries."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from eventcontracts.domain.decisions import IntentEnvelope, PlaceOrder
from eventcontracts.domain.metadata import FrozenMap, freeze_mapping
from eventcontracts.domain.models import InstrumentId, OutcomeSide
from eventcontracts.domain.orders import OrderSide, TimeInForce
from eventcontracts.domain.validation import (
    require_aware_datetime,
    require_currency,
    require_non_empty,
    require_non_negative_decimal,
    require_positive_decimal,
    require_probability_decimal,
)


@dataclass(frozen=True)
class OrderIntent:
    """Executable order request produced after strategy + risk handoff.

    Handoff:
    ``runner.IntentEnvelope`` carries the strategy decision and provenance.
    ``intent_to_order`` converts supported order decisions into this execution
    shape. Paper/live execution should consume this object, not raw decisions.
    """

    instrument_id: InstrumentId
    side: OutcomeSide
    price: Decimal
    quantity: Decimal
    order_type: str
    order_side: OrderSide = OrderSide.BUY
    time_in_force: TimeInForce = TimeInForce.GTC
    post_only: bool = False
    metadata: Mapping[str, Any] = field(default_factory=FrozenMap)

    def __post_init__(self) -> None:
        require_probability_decimal(self.price, "price")
        require_positive_decimal(self.quantity, "quantity")
        require_non_empty(self.order_type, "order_type")
        object.__setattr__(self, "metadata", freeze_mapping(self.metadata))


@dataclass(frozen=True)
class SimulatedFill:
    order: OrderIntent
    price: Decimal
    quantity: Decimal
    filled_at: datetime
    liquidity: str
    fee_amount: Decimal
    fee_currency: str

    def __post_init__(self) -> None:
        require_probability_decimal(self.price, "price")
        require_positive_decimal(self.quantity, "quantity")
        require_aware_datetime(self.filled_at, "filled_at")
        require_non_empty(self.liquidity, "liquidity")
        require_non_negative_decimal(self.fee_amount, "fee_amount")
        require_currency(self.fee_currency, "fee_currency")


class ExecutionSimulator:
    """Consumes replayed market events and produces simulated fills."""

    def submit(self, order: OrderIntent) -> list[SimulatedFill]:
        raise NotImplementedError


def intent_to_order(envelope: IntentEnvelope) -> OrderIntent | None:
    """Convert supported order-affecting envelopes into execution intents."""

    decision = envelope.decision
    if not isinstance(decision, PlaceOrder):
        return None
    if decision.price is None:
        raise ValueError("paper execution requires a price on PlaceOrder")
    return OrderIntent(
        instrument_id=decision.instrument_id,
        side=decision.outcome_side,
        order_side=decision.order_side,
        price=decision.price,
        quantity=decision.quantity,
        order_type=decision.order_type.value,
        post_only=decision.order_type.value == "post_only",
        time_in_force=decision.time_in_force,
        metadata={
            "client_order_id": str(decision.client_order_id),
            "strategy_id": str(envelope.strategy_id),
            "sleeve_id": str(envelope.sleeve_id),
            "correlation_id": str(envelope.correlation_id),
        },
    )


class ImmediateFillSimulator(ExecutionSimulator):
    """Deterministic placeholder simulator that fills accepted orders immediately.

    This is not a market-realistic simulator. It exists so the data path can be
    tested end to end before fee, queue, slippage, and latency models are added.
    """

    def __init__(self, *, filled_at: datetime, fee_currency: str = "USD") -> None:
        self.filled_at = filled_at
        self.fee_currency = fee_currency

    def submit(self, order: OrderIntent) -> list[SimulatedFill]:
        return [
            SimulatedFill(
                order=order,
                price=order.price,
                quantity=order.quantity,
                filled_at=self.filled_at,
                liquidity="unknown",
                fee_amount=Decimal("0"),
                fee_currency=self.fee_currency,
            )
        ]
