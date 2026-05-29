"""Dry-run gateway and routing primitive tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from eventcontracts.audit import audit_stamp_for
from eventcontracts.domain import (
    ClientOrderId,
    CorrelationId,
    ExecutionPriority,
    InstrumentId,
    IntentEnvelope,
    LatencyTier,
    MarketSnapshot,
    NoAction,
    OrderBookLevel,
    OutcomeSide,
    PlaceOrder,
    SleeveId,
    StrategyId,
    Venue,
)
from eventcontracts.domain.orders import OrderSide, OrderType, TimeInForce
from eventcontracts.execution import OrderIntent
from eventcontracts.gateway import (
    DryRunVenueGateway,
    GatewayCommand,
    GatewayCommandKind,
    GatewayLastLook,
    GatewayLastLookPolicy,
    InMemoryIdempotencyStore,
    InMemoryPriorityScheduler,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)
INSTR = InstrumentId(venue=Venue.KALSHI, market_id="M-1")


def _envelope(correlation_id: str, priority: ExecutionPriority) -> IntentEnvelope:
    return IntentEnvelope(
        decision=NoAction(reason="routing test"),
        strategy_id=StrategyId("s"),
        sleeve_id=SleeveId("sl"),
        correlation_id=CorrelationId(correlation_id),
        emitted_at=NOW,
        priority=priority,
    )


def test_priority_scheduler_orders_by_tier_then_time() -> None:
    scheduler = InMemoryPriorityScheduler()
    scheduler.enqueue(_envelope("standard", ExecutionPriority(tier=LatencyTier.STANDARD)))
    scheduler.enqueue(_envelope("critical", ExecutionPriority(tier=LatencyTier.CRITICAL)))
    scheduler.enqueue(_envelope("fast", ExecutionPriority(tier=LatencyTier.FAST)))

    batch = scheduler.next_batch(now=NOW, limit=3)

    assert tuple(str(envelope.correlation_id) for envelope in batch) == (
        "critical",
        "fast",
        "standard",
    )


def test_priority_scheduler_drops_expired_intents() -> None:
    scheduler = InMemoryPriorityScheduler()
    scheduler.enqueue(
        _envelope(
            "stale",
            ExecutionPriority(tier=LatencyTier.FAST, expires_after_ms=10),
        )
    )

    stale = scheduler.drop_stale(now=NOW + timedelta(milliseconds=11))

    assert tuple(str(envelope.correlation_id) for envelope in stale) == ("stale",)
    assert scheduler.next_batch(now=NOW, limit=10) == ()


def test_idempotency_store_reserves_once_and_returns_completed_ack() -> None:
    command = _command()
    gateway = DryRunVenueGateway(clock=lambda: NOW)
    ack = gateway.submit(command)
    store = InMemoryIdempotencyStore()

    assert store.reserve("key-1", CorrelationId("corr-1")) is True
    assert store.reserve("key-1", CorrelationId("corr-1")) is False
    store.mark_complete("key-1", ack)

    assert store.lookup("key-1") == ack


def test_idempotency_store_evicts_by_ttl() -> None:
    now = NOW

    def clock() -> datetime:
        return now

    store = InMemoryIdempotencyStore(ttl=timedelta(seconds=1), clock=clock)
    assert store.reserve("key-1", CorrelationId("corr-1")) is True
    now = NOW + timedelta(seconds=2)

    assert store.reserve("key-1", CorrelationId("corr-2")) is True


def test_dry_run_gateway_records_without_live_send() -> None:
    command = _command()
    gateway = DryRunVenueGateway(clock=lambda: NOW)

    ack = gateway.submit(command)

    assert ack.accepted is True
    assert ack.reasons == ("dry_run_submit",)
    assert gateway.commands == [command]
    assert ack.audit.parent_ids == (command.audit.object_id,)


def test_dry_run_gateway_accepts_fresh_last_look() -> None:
    command = _command_with_envelope()
    latest = _snapshot(ask="0.515", received_at=NOW + timedelta(milliseconds=10))
    gateway = DryRunVenueGateway(
        clock=lambda: NOW + timedelta(milliseconds=10),
        last_look=GatewayLastLook(
            snapshot_for=lambda instrument, side: latest,
            policy=GatewayLastLookPolicy(
                max_price_movement=Decimal("0.01"),
                max_spread=Decimal("0.05"),
                max_slippage=Decimal("0.01"),
            ),
        ),
    )

    ack = gateway.submit(command)

    assert ack.accepted is True
    assert ack.reasons == ("dry_run_submit",)


def test_dry_run_gateway_rejects_when_last_look_price_moved() -> None:
    command = _command_with_envelope()
    latest = _snapshot(bid="0.60", ask="0.62", received_at=NOW + timedelta(milliseconds=10))
    gateway = DryRunVenueGateway(
        clock=lambda: NOW + timedelta(milliseconds=10),
        last_look=GatewayLastLook(
            snapshot_for=lambda instrument, side: latest,
            policy=GatewayLastLookPolicy(max_price_movement=Decimal("0.01")),
        ),
    )

    ack = gateway.submit(command)

    assert ack.accepted is False
    assert "last_look_price_moved" in ack.reasons
    assert ack.reject is not None


def _command() -> GatewayCommand:
    intent = OrderIntent(
        instrument_id=INSTR,
        side=OutcomeSide.YES,
        order_side=OrderSide.BUY,
        price=Decimal("0.45"),
        quantity=Decimal("1"),
        order_type="limit",
        metadata={"client_order_id": "co-1"},
    )
    audit = audit_stamp_for(
        {"intent": "co-1"},
        object_id="gateway-command:corr-1",
        object_kind="gateway_command",
        schema_version="gateway-v1",
        produced_at=NOW,
        producer="test",
    )
    return GatewayCommand(
        kind=GatewayCommandKind.SUBMIT,
        venue=Venue.KALSHI,
        correlation_id=CorrelationId("corr-1"),
        audit=audit,
        intent=intent,
        client_order_id=ClientOrderId("co-1"),
    )


def _command_with_envelope() -> GatewayCommand:
    decision = PlaceOrder(
        client_order_id=ClientOrderId("co-1"),
        instrument_id=INSTR,
        outcome_side=OutcomeSide.YES,
        order_side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTD,
        quantity=Decimal("1"),
        price=Decimal("0.51"),
        expires_at=NOW + timedelta(seconds=1),
        market_snapshot=_snapshot(),
    )
    envelope = IntentEnvelope(
        decision=decision,
        strategy_id=StrategyId("s"),
        sleeve_id=SleeveId("sl"),
        correlation_id=CorrelationId("corr-1"),
        emitted_at=NOW,
    )
    command = _command()
    return GatewayCommand(
        kind=command.kind,
        venue=command.venue,
        correlation_id=command.correlation_id,
        audit=command.audit,
        intent=command.intent,
        client_order_id=command.client_order_id,
        envelope=envelope,
    )


def _snapshot(
    *,
    bid: str = "0.49",
    ask: str = "0.51",
    received_at: datetime = NOW,
) -> MarketSnapshot:
    return MarketSnapshot(
        instrument_id=INSTR,
        side=OutcomeSide.YES,
        bid=OrderBookLevel(price=Decimal(bid), quantity=Decimal("100")),
        ask=OrderBookLevel(price=Decimal(ask), quantity=Decimal("100")),
        exchange_ts=received_at,
        received_at=received_at,
        source="fixture",
        source_sequence="1",
    )
