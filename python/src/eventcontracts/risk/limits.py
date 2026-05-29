"""Risk limit primitives.

Each ``check_*`` function returns a tuple of rejection reasons (empty tuple
means "no objection"). Composing them gives the full pre-trade gate behavior
used by :class:`eventcontracts.risk.policy.SleeveRiskGate`.

Limits are deterministic and side-effect-free; stateful concerns (daily loss
ledger, kill switches) live in :mod:`eventcontracts.risk.state`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_CEILING, Decimal, InvalidOperation

from eventcontracts.domain.decisions import PlaceOrder
from eventcontracts.domain.orders import Order, OrderSide, OrderType, TimeInForce
from eventcontracts.domain.positions import CashBalance, Exposure, Position
from eventcontracts.domain.spec import RiskProfile


@dataclass(frozen=True)
class RiskLimits:
    max_order_notional: Decimal
    max_position_notional: Decimal
    max_daily_loss: Decimal
    currency: str


def order_notional(order: PlaceOrder) -> Decimal:
    """Conservative notional estimate for a strategy order decision."""

    price = order.price if order.price is not None else Decimal("1")
    return price * order.quantity


def check_order_notional(order: PlaceOrder, profile: RiskProfile) -> tuple[str, ...]:
    """Reject if the order notional exceeds the per-order limit."""

    if order_notional(order) > profile.max_order_notional:
        return ("max_order_notional",)
    return ()


def check_position_notional(
    order: PlaceOrder,
    profile: RiskProfile,
    positions: Sequence[Position],
) -> tuple[str, ...]:
    """Reject if the order would push position notional over the limit.

    Notional is computed at the same outcome-side bucket as the order. For
    a binary contract, a BUY on YES adds to the YES position; a SELL on YES
    reduces it (and only triggers the limit if it crosses to net short, which
    binary contracts on Kalshi do not allow, so the SELL path is conservative).
    """

    key = (order.instrument_id, order.outcome_side)
    current_quantity = Decimal("0")
    current_avg = Decimal("0")
    for pos in positions:
        if (pos.instrument_id, pos.outcome_side) == key:
            current_quantity = pos.quantity
            current_avg = pos.average_price

    delta = order.quantity if order.order_side is OrderSide.BUY else -order.quantity
    projected_quantity = current_quantity + delta
    price = order.price if order.price is not None else Decimal("1")
    if projected_quantity < 0:
        # Reducing past zero is treated as a sale of the existing position only;
        # the venue rejects the rest. Cap at zero for notional purposes.
        projected_quantity = Decimal("0")

    avg = current_avg if current_quantity > 0 else price
    projected_notional = projected_quantity * avg
    if projected_notional > profile.max_position_notional:
        return ("max_position_notional",)
    return ()


def check_open_orders(open_orders: Sequence[Order], profile: RiskProfile) -> tuple[str, ...]:
    if len(open_orders) >= profile.max_open_orders:
        return ("max_open_orders",)
    return ()


def check_gross_exposure(
    order: PlaceOrder,
    profile: RiskProfile,
    exposure: Exposure | None,
) -> tuple[str, ...]:
    """Reject if a position-opening BUY would push gross exposure over the limit.

    A SELL closes existing inventory on a prediction venue and therefore cannot
    increase gross exposure — so risk-reducing exits are never blocked here.
    This mirrors the Rust gate, which nets sells via a *signed* projected
    position (see ``rust/crates/risk/src/lib.rs::sell_reduces_position_notional``).
    Selling beyond the held quantity would open a short, which the venue itself
    rejects, so allowing it through this gate is harmless.
    """

    if order.order_side is not OrderSide.BUY:
        return ()
    if exposure is None:
        return ()
    projected = exposure.gross_notional + order_notional(order)
    if projected > profile.max_gross_exposure:
        return ("max_gross_exposure",)
    return ()


def check_available_cash(order: PlaceOrder, balance: CashBalance) -> tuple[str, ...]:
    """Reject if a position-opening BUY would spend more cash than is available.

    SELL orders close existing inventory and *return* cash; they never require
    buying power. Blocking a sell on low cash would stop the sleeve from
    de-risking, so risk-reducing exits always pass this gate. (A sell beyond the
    held quantity would open a short, which the venue rejects.)
    """

    if order.order_side is not OrderSide.BUY:
        return ()
    if order_notional(order) > balance.available:
        return ("available_cash",)
    return ()


def check_fee_adjusted_edge(order: PlaceOrder) -> tuple[str, ...]:
    """Reject negative model edge after taker fees when fair value is supplied.

    Strategies carry the optional contract through decision metadata:
    ``fair_price`` (outcome-side fair value), ``min_executable_edge_ticks``
    (1 tick = 0.0001), and ``fee_rate_bps`` (default Kalshi taker curve,
    700 bps). If no fair value is present, this gate is intentionally inert.
    """

    fair_raw = order.metadata.get("fair_price")
    if fair_raw is None:
        return ()
    if order.price is None:
        return ("negative_edge_after_fees",)
    try:
        fair = Decimal(str(fair_raw))
        minimum_ticks = int(str(order.metadata.get("min_executable_edge_ticks", "0")))
        fee_rate_bps = int(str(order.metadata.get("fee_rate_bps", "700")))
    except (ValueError, InvalidOperation):
        return ("invalid_edge_metadata",)

    fee_per_contract = _kalshi_fee_per_contract(
        price=order.price,
        quantity=order.quantity,
        rate_bps=fee_rate_bps,
    )
    gross_edge = (
        fair - order.price
        if order.order_side is OrderSide.BUY
        else order.price - fair
    )
    minimum = Decimal(minimum_ticks) / Decimal("10000")
    if gross_edge - fee_per_contract < minimum:
        return ("negative_edge_after_fees",)
    return ()


def check_execution_bounds(
    order: PlaceOrder,
    profile: RiskProfile,
    when: datetime,
) -> tuple[str, ...]:
    """Reject unbounded execution instructions before they reach a venue."""

    reasons: list[str] = []
    if order.order_type is OrderType.MARKET and not profile.allow_market_orders:
        reasons.append("market_orders_disabled")
    if order.order_type is OrderType.MARKET and order.price is None:
        reasons.append("unpriced_market_order")

    if order.time_in_force is TimeInForce.GTC and order.expires_at is None and not profile.allow_unbounded_gtc:
        reasons.append("unbounded_gtc_order")
    if order.time_in_force is TimeInForce.GTD and order.expires_at is None:
        reasons.append("missing_order_expiry")

    if order.expires_at is not None:
        lifetime_ms = int((order.expires_at - when).total_seconds() * 1000)
        if lifetime_ms <= 0:
            reasons.append("expired_order_intent")
        elif lifetime_ms > profile.max_order_lifetime_ms:
            reasons.append("order_ttl_too_long")

    return tuple(reasons)


def _kalshi_fee_per_contract(
    *,
    price: Decimal,
    quantity: Decimal,
    rate_bps: int,
) -> Decimal:
    if quantity <= 0 or rate_bps <= 0:
        return Decimal("0")
    rate = Decimal(rate_bps) / Decimal("10000")
    raw = rate * price * (Decimal("1") - price) * quantity
    total = raw.quantize(Decimal("0.01"), rounding=ROUND_CEILING)
    return total / quantity


def check_market_snapshot(
    order: PlaceOrder,
    profile: RiskProfile,
    when: datetime,
) -> tuple[str, ...]:
    """Reject orders that lack fresh, continuous executable market evidence."""

    snapshot = order.market_snapshot
    if snapshot is None:
        return ("missing_market_snapshot",)

    reasons: list[str] = []
    if snapshot.instrument_id != order.instrument_id:
        reasons.append("market_snapshot_instrument_mismatch")
    if snapshot.side is not order.outcome_side:
        reasons.append("market_snapshot_side_mismatch")
    if snapshot.exchange_ts is None:
        reasons.append("market_snapshot_missing_exchange_ts")
    if snapshot.source_sequence is None:
        reasons.append("market_snapshot_missing_sequence")
    if snapshot.sequence_gap:
        reasons.append("market_snapshot_sequence_gap")
    if snapshot.bid is None or snapshot.ask is None:
        reasons.append("market_snapshot_missing_bbo")
    else:
        spread = snapshot.ask.price - snapshot.bid.price
        if spread < Decimal("0"):
            reasons.append("market_snapshot_crossed_bbo")
        elif spread > profile.max_spread:
            reasons.append("market_snapshot_spread_too_wide")
        executable_depth: Decimal | None = None
        if order.order_type is OrderType.MARKET:
            executable_depth = (
                snapshot.ask.quantity
                if order.order_side is OrderSide.BUY
                else snapshot.bid.quantity
            )
        elif order.price is not None:
            if order.order_side is OrderSide.BUY and order.price >= snapshot.ask.price:
                executable_depth = snapshot.ask.quantity
            elif order.order_side is OrderSide.SELL and order.price <= snapshot.bid.price:
                executable_depth = snapshot.bid.quantity
        if executable_depth is not None and order.quantity > executable_depth:
            reasons.append("order_quantity_exceeds_l1_depth")

    age_ms = snapshot.age_ms(when)
    if age_ms < 0:
        reasons.append("market_snapshot_from_future")
    elif age_ms > profile.max_market_data_age_ms:
        reasons.append("stale_market_snapshot")

    return tuple(reasons)


def check_daily_loss(
    profile: RiskProfile,
    realized_loss_today: Decimal,
) -> tuple[str, ...]:
    """Reject everything once the sleeve has already lost more than the limit today."""

    if profile.max_daily_loss > 0 and realized_loss_today >= profile.max_daily_loss:
        return ("max_daily_loss",)
    return ()
