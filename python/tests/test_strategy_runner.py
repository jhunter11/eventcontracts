"""End-to-end smoke test for the strategy/runner plug pattern."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from eventcontracts.domain import (
    CashBalance,
    ClientOrderId,
    CorrelationId,
    EventId,
    EventProvenance,
    EventSubscription,
    ExecutionPriority,
    ExternalSignalEvent,
    InstrumentId,
    IntentEnvelope,
    LatencyTier,
    NoAction,
    OrderBook,
    OrderBookEvent,
    OrderBookLevel,
    OrderSide,
    OrderType,
    OutcomeSide,
    PlaceOrder,
    Quote,
    QuoteEvent,
    RiskProfile,
    SleeveId,
    SleeveSpec,
    StrategyId,
    StrategySpec,
    TimeInForce,
    Trade,
    TradeEvent,
    Venue,
)
from eventcontracts.plugins.strategies import example_threshold  # noqa: F401 — registers
from eventcontracts.plugins.strategies.microstructure_obi_scalper import (
    MicrostructureObiScalperStrategy,
)
from eventcontracts.plugins.strategies.weather_temperature_arbitrage import (
    WeatherTemperatureArbitrageStrategy,
)
from eventcontracts.risk import SleeveRiskGate
from eventcontracts.runner import StatefulContextProvider, StrategyRunner
from eventcontracts.strategy import create, known
from eventcontracts.strategy.base import StrategyBase, StrategyFeedback
from eventcontracts.strategy.lifecycle import StrategyState
from eventcontracts.testing import (
    AllowAllRiskGate,
    InMemoryClock,
    InMemoryContext,
    InMemoryEventSource,
    InMemoryIntentSink,
    StaticContextProvider,
)


def _spec() -> StrategySpec:
    return StrategySpec(
        strategy_id=StrategyId("example-threshold-v1"),
        name="example_threshold",
        version="0.1.0",
        description="reference strategy used by the smoke test",
        subscription=EventSubscription(
            venues=(Venue.KALSHI,),
            instrument_patterns=("*",),
            event_kinds=("trade",),
        ),
        parameters={"buy_below": "0.50", "size": "5"},
    )


def _sleeve() -> SleeveSpec:
    return SleeveSpec(
        sleeve_id=SleeveId("sleeve-a"),
        strategy_id=StrategyId("example-threshold-v1"),
        strategy_version="0.1.0",
        venue=Venue.KALSHI,
        capital_allocation=Decimal("1000"),
        currency="USD",
        risk=RiskProfile(
            max_order_notional=Decimal("100"),
            max_position_notional=Decimal("500"),
            max_daily_loss=Decimal("50"),
            max_open_orders=5,
            max_gross_exposure=Decimal("500"),
            currency="USD",
        ),
    )


def _trade(price: str, instrument: InstrumentId) -> TradeEvent:
    return TradeEvent(
        event_id=EventId(f"e-{price}"),
        trade=Trade(
            instrument_id=instrument,
            side=None,
            price=Decimal(price),
            quantity=Decimal("1"),
            trade_id=None,
            exchange_ts=None,
            received_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
    )


def _quote(bid: str, ask: str, instrument: InstrumentId) -> QuoteEvent:
    return QuoteEvent(
        event_id=EventId(f"q-{bid}-{ask}"),
        quote=Quote(
            instrument_id=instrument,
            side=OutcomeSide.YES,
            bid=OrderBookLevel(price=Decimal(bid), quantity=Decimal("100")),
            ask=OrderBookLevel(price=Decimal(ask), quantity=Decimal("100")),
            exchange_ts=datetime(2026, 1, 1, tzinfo=UTC),
            received_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        provenance=EventProvenance(
            source="fixture",
            channel="quote",
            source_sequence="1",
        ),
    )


def _book_event(
    bid: str,
    ask: str,
    bid_qty: str,
    ask_qty: str,
    instrument: InstrumentId | None = None,
) -> OrderBookEvent:
    instrument = instrument or InstrumentId(venue=Venue.KALSHI, market_id="DEMO-OBI")
    return OrderBookEvent(
        event_id=EventId(f"book-{bid}-{ask}-{bid_qty}-{ask_qty}"),
        book=OrderBook(
            instrument_id=instrument,
            yes_bids=(OrderBookLevel(price=Decimal(bid), quantity=Decimal(bid_qty)),),
            yes_asks=(OrderBookLevel(price=Decimal(ask), quantity=Decimal(ask_qty)),),
            no_bids=(),
            no_asks=(),
            exchange_ts=datetime(2026, 1, 1, tzinfo=UTC),
            received_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        provenance=EventProvenance(source="fixture", channel="book", source_sequence="1"),
    )


def test_strategy_registers_by_name() -> None:
    assert "example_threshold" in known()


def test_create_resolves_through_registry() -> None:
    strategy = create("example_threshold", _spec())
    assert strategy.spec.name == "example_threshold"


def test_runner_emits_place_order_below_threshold() -> None:
    spec = _spec()
    sleeve = _sleeve()
    strategy = create("example_threshold", spec)
    instrument = InstrumentId(venue=Venue.KALSHI, market_id="DEMO-1")
    events = (
        _quote("0.43", "0.44", instrument),
        _trade("0.60", instrument),
        _trade("0.45", instrument),
        _trade("0.30", instrument),
    )
    sink = InMemoryIntentSink()
    clock = InMemoryClock()
    ctx = InMemoryContext(
        strategy_id_value=spec.strategy_id,
        sleeve_id_value=sleeve.sleeve_id,
        clock_now=clock.now(),
    )
    runner = StrategyRunner(
        spec=spec,
        sleeve=sleeve,
        strategy=strategy,
        events=InMemoryEventSource(events=events),
        sink=sink,
        risk=AllowAllRiskGate(),
        clock=clock,
        context_provider=StaticContextProvider(ctx=ctx),
    )

    summary = runner.run()

    assert summary.events_processed == 4
    assert summary.decisions_emitted == 4
    assert summary.intents_dispatched == 4
    assert summary.intents_rejected == 0
    assert summary.end_state is StrategyState.DISPOSED

    place_orders = [e for e in sink.emitted if isinstance(e.decision, PlaceOrder)]
    assert len(place_orders) == 2
    assert all(
        isinstance(po.decision, PlaceOrder) and po.decision.outcome_side is OutcomeSide.YES for po in place_orders
    )


def test_runner_records_rejection_reasons() -> None:
    class RejectAll:
        def evaluate(self, envelope, ctx):  # type: ignore[no-untyped-def]
            from eventcontracts.runner.ports import RiskDecision

            return RiskDecision(allowed=False, reasons=("test-reject",))

    spec = _spec()
    sleeve = _sleeve()
    strategy = create("example_threshold", spec)
    instrument = InstrumentId(venue=Venue.KALSHI, market_id="DEMO-1")
    sink = InMemoryIntentSink()
    clock = InMemoryClock()
    ctx = InMemoryContext(
        strategy_id_value=spec.strategy_id,
        sleeve_id_value=sleeve.sleeve_id,
        clock_now=clock.now(),
    )
    runner = StrategyRunner(
        spec=spec,
        sleeve=sleeve,
        strategy=strategy,
        events=InMemoryEventSource(events=(_quote("0.09", "0.10", instrument), _trade("0.10", instrument))),
        sink=sink,
        risk=RejectAll(),
        clock=clock,
        context_provider=StaticContextProvider(ctx=ctx),
    )

    summary = runner.run()

    assert summary.intents_dispatched == 0
    assert summary.intents_rejected == 2
    assert summary.rejection_reasons == {"test-reject": 2}
    assert sink.emitted == []


def test_runner_emits_intent_rejected_feedback_to_strategy() -> None:
    class OneOrderStrategy(StrategyBase):
        def __init__(self, spec: StrategySpec) -> None:
            super().__init__(spec)
            self.feedback: list[StrategyFeedback] = []

        def on_event(self, event, ctx):  # type: ignore[no-untyped-def]
            del event, ctx
            return (_place(InstrumentId(venue=Venue.KALSHI, market_id="DEMO-1"), "feedback"),)

        def on_feedback(self, feedback: StrategyFeedback, ctx) -> None:  # type: ignore[no-untyped-def]
            del ctx
            self.feedback.append(feedback)

    class RejectAll:
        def evaluate(self, envelope, ctx):  # type: ignore[no-untyped-def]
            from eventcontracts.runner.ports import RiskDecision

            return RiskDecision(allowed=False, reasons=("test-reject",))

    spec = _spec()
    sleeve = _sleeve()
    strategy = OneOrderStrategy(spec)
    clock = InMemoryClock()
    ctx = InMemoryContext(
        strategy_id_value=spec.strategy_id,
        sleeve_id_value=sleeve.sleeve_id,
        clock_now=clock.now(),
    )
    runner = StrategyRunner(
        spec=spec,
        sleeve=sleeve,
        strategy=strategy,
        events=InMemoryEventSource(events=(_trade("0.10", InstrumentId(venue=Venue.KALSHI, market_id="DEMO-1")),)),
        sink=InMemoryIntentSink(),
        risk=RejectAll(),
        clock=clock,
        context_provider=StaticContextProvider(ctx=ctx),
    )

    runner.run()

    assert len(strategy.feedback) == 1
    assert strategy.feedback[0].kind == "IntentRejected"
    assert strategy.feedback[0].reasons == ("test-reject",)
    assert strategy.feedback[0].client_order_id == ClientOrderId("co-feedback")


def test_weather_arb_releases_notional_on_risk_reject() -> None:
    class RejectAll:
        def evaluate(self, envelope, ctx):  # type: ignore[no-untyped-def]
            from eventcontracts.runner.ports import RiskDecision

            return RiskDecision(allowed=False, reasons=("forced_reject",))

    spec = replace(
        _spec(),
        strategy_id=StrategyId("weather-arb-v1"),
        name="weather_temperature_arbitrage",
        parameters={
            "signal_source": "fixture-weather",
            "execution_mode": "taker_if_edge",
            "min_edge_bps": "10",
            "max_size": "10",
            "capital_source": "context_cash",
            "max_trade_capital_fraction": "1",
        },
    )
    sleeve = _sleeve()
    strategy = WeatherTemperatureArbitrageStrategy(spec)
    instrument = InstrumentId(venue=Venue.KALSHI, market_id="DEMO-1")
    clock = InMemoryClock()
    ctx = InMemoryContext(
        strategy_id_value=spec.strategy_id,
        sleeve_id_value=sleeve.sleeve_id,
        clock_now=clock.now(),
        cash_by_ccy={
            "USD": CashBalance(
                currency="USD",
                total=Decimal("1000"),
                available=Decimal("1000"),
                held_for_orders=Decimal("0"),
                settling=Decimal("0"),
                updated_at=clock.now(),
            )
        },
    )
    signal = ExternalSignalEvent(
        event_id=EventId("weather-signal"),
        source="fixture-weather",
        exchange_ts=clock.now(),
        received_at=clock.now(),
        schema_version="weather-prob-v1",
        payload={
            "implied_prob": "0.75",
            "instrument_id": {
                "venue": "kalshi",
                "market_id": instrument.market_id,
            },
        },
        provenance=EventProvenance(source="fixture", channel="external"),
    )
    runner = StrategyRunner(
        spec=spec,
        sleeve=sleeve,
        strategy=strategy,
        events=InMemoryEventSource(events=(_quote("0.50", "0.52", instrument), signal)),
        sink=InMemoryIntentSink(),
        risk=RejectAll(),
        clock=clock,
        context_provider=StaticContextProvider(ctx=ctx),
    )

    runner.run()

    assert strategy._active_notional == Decimal("0")
    assert strategy._pending_notional_by_client_order_id == {}


def test_weather_arb_censors_non_zero_lead_signal() -> None:
    """Defense in depth: a signal tagged with a non-zero lead must be refused
    outright and must not be cached for later quote/book re-fires. Signals without
    a `lead_days` tag (older producers) are unaffected."""
    spec = replace(
        _spec(),
        strategy_id=StrategyId("weather-arb-v1"),
        name="weather_temperature_arbitrage",
        parameters={
            "signal_source": "fixture-weather",
            "execution_mode": "taker_if_edge",
            "min_edge_bps": "10",
        },
    )
    sleeve = _sleeve()
    strategy = WeatherTemperatureArbitrageStrategy(spec)
    instrument = InstrumentId(venue=Venue.KALSHI, market_id="DEMO-1")
    clock = InMemoryClock()
    ctx = InMemoryContext(
        strategy_id_value=spec.strategy_id,
        sleeve_id_value=sleeve.sleeve_id,
        clock_now=clock.now(),
        cash_by_ccy={
            "USD": CashBalance(
                currency="USD",
                total=Decimal("1000"),
                available=Decimal("1000"),
                held_for_orders=Decimal("0"),
                settling=Decimal("0"),
                updated_at=clock.now(),
            )
        },
    )
    signal = ExternalSignalEvent(
        event_id=EventId("weather-signal-lead1"),
        source="fixture-weather",
        exchange_ts=clock.now(),
        received_at=clock.now(),
        schema_version="weather-prob-v1",
        payload={
            "implied_prob": "0.75",
            "lead_days": 1,
            "instrument_id": {
                "venue": "kalshi",
                "market_id": instrument.market_id,
            },
        },
        provenance=EventProvenance(source="fixture", channel="external"),
    )

    decisions = strategy.on_event(signal, ctx)

    assert len(decisions) == 1
    assert decisions[0].reason.startswith("censored:lead_days")
    assert strategy._latest_signal_by_instrument == {}


def test_weather_arb_censors_signal_within_close_buffer() -> None:
    """With min_seconds_to_close set, a signal whose market closes inside the buffer
    is refused (recomputed against ctx.now), while a far-from-close signal is not."""
    spec = replace(
        _spec(),
        strategy_id=StrategyId("weather-arb-v1"),
        name="weather_temperature_arbitrage",
        parameters={
            "signal_source": "fixture-weather",
            "execution_mode": "taker_if_edge",
            "min_edge_bps": "10",
            "min_seconds_to_close": "300",
        },
    )
    sleeve = _sleeve()
    strategy = WeatherTemperatureArbitrageStrategy(spec)
    instrument = InstrumentId(venue=Venue.KALSHI, market_id="DEMO-1")
    clock = InMemoryClock()
    ctx = InMemoryContext(
        strategy_id_value=spec.strategy_id,
        sleeve_id_value=sleeve.sleeve_id,
        clock_now=clock.now(),
        cash_by_ccy={
            "USD": CashBalance(
                currency="USD",
                total=Decimal("1000"),
                available=Decimal("1000"),
                held_for_orders=Decimal("0"),
                settling=Decimal("0"),
                updated_at=clock.now(),
            )
        },
    )

    def _signal(close_dt: datetime) -> ExternalSignalEvent:
        return ExternalSignalEvent(
            event_id=EventId(f"weather-signal-{close_dt.isoformat()}"),
            source="fixture-weather",
            exchange_ts=clock.now(),
            received_at=clock.now(),
            schema_version="weather-prob-v1",
            payload={
                "implied_prob": "0.75",
                "close_time": close_dt.isoformat(),
                "instrument_id": {"venue": "kalshi", "market_id": instrument.market_id},
            },
            provenance=EventProvenance(source="fixture", channel="external"),
        )

    near = strategy.on_event(_signal(clock.now() + timedelta(seconds=100)), ctx)
    assert len(near) == 1
    assert near[0].reason.startswith("censored:within_300s_of_close")

    # Far from close: the near-close gate must not fire (warmup:no_mid_yet means it
    # passed the gate and reached normal pricing without a quote yet).
    far = strategy.on_event(_signal(clock.now() + timedelta(seconds=10_000)), ctx)
    assert len(far) == 1
    assert not far[0].reason.startswith("censored:within_")


def test_weather_arb_can_trigger_from_quote_tick_after_signal() -> None:
    spec = replace(
        _spec(),
        strategy_id=StrategyId("weather-arb-v1"),
        name="weather_temperature_arbitrage",
        parameters={
            "signal_source": "fixture-weather",
            "execution_mode": "taker_if_edge",
            "quote_triggered_trading": "true",
            "max_signal_age_seconds": "60",
            "min_edge_bps": "10",
            "max_size": "10",
            "capital_source": "context_cash",
            "max_trade_capital_fraction": "1",
            "min_retrade_price_delta": "0.03",
            "min_retrade_probability_delta": "0.04",
        },
    )
    strategy = WeatherTemperatureArbitrageStrategy(spec)
    instrument = InstrumentId(venue=Venue.KALSHI, market_id="DEMO-1")
    now = datetime(2026, 1, 1, tzinfo=UTC)
    ctx = InMemoryContext(
        strategy_id_value=spec.strategy_id,
        sleeve_id_value=SleeveId("sleeve-a"),
        clock_now=now,
        cash_by_ccy={
            "USD": CashBalance(
                currency="USD",
                total=Decimal("1000"),
                available=Decimal("1000"),
                held_for_orders=Decimal("0"),
                settling=Decimal("0"),
                updated_at=now,
            )
        },
    )
    signal = ExternalSignalEvent(
        event_id=EventId("weather-signal"),
        source="fixture-weather",
        exchange_ts=now,
        received_at=now,
        schema_version="weather-prob-v1",
        payload={
            "implied_prob": "0.75",
            "target_time": now.isoformat(),
            "instrument_id": {
                "venue": "kalshi",
                "market_id": instrument.market_id,
            },
        },
        provenance=EventProvenance(source="fixture", channel="external"),
    )

    assert list(strategy.on_event(_quote("0.70", "0.75", instrument), ctx)) == [
        NoAction(reason="quote_mid_updated:no_signal")
    ]
    assert list(strategy.on_event(signal, ctx)) == [
        NoAction(reason="edge_below_executable_threshold")
    ]

    tick_decisions = list(strategy.on_event(_quote("0.50", "0.52", instrument), ctx))
    orders = [decision for decision in tick_decisions if isinstance(decision, PlaceOrder)]
    assert len(orders) == 1
    assert orders[0].outcome_side is OutcomeSide.YES
    assert orders[0].price == Decimal("0.52")

    duplicate = list(strategy.on_event(_quote("0.50", "0.52", instrument), ctx))
    assert duplicate == [NoAction(reason="edge_below_executable_threshold:quote_tick")]


def test_weather_arb_can_trigger_from_book_tick_after_signal() -> None:
    spec = replace(
        _spec(),
        strategy_id=StrategyId("weather-arb-v1"),
        name="weather_temperature_arbitrage",
        parameters={
            "signal_source": "fixture-weather",
            "execution_mode": "taker_if_edge",
            "quote_triggered_trading": "true",
            "max_signal_age_seconds": "60",
            "min_edge_bps": "10",
            "max_size": "10",
            "capital_source": "context_cash",
            "max_trade_capital_fraction": "1",
            "min_retrade_price_delta": "0.03",
            "min_retrade_probability_delta": "0.04",
        },
    )
    strategy = WeatherTemperatureArbitrageStrategy(spec)
    instrument = InstrumentId(venue=Venue.KALSHI, market_id="DEMO-1")
    now = datetime(2026, 1, 1, tzinfo=UTC)
    ctx = InMemoryContext(
        strategy_id_value=spec.strategy_id,
        sleeve_id_value=SleeveId("sleeve-a"),
        clock_now=now,
        cash_by_ccy={
            "USD": CashBalance(
                currency="USD",
                total=Decimal("10"),
                available=Decimal("10"),
                held_for_orders=Decimal("0"),
                settling=Decimal("0"),
                updated_at=now,
            )
        },
    )
    signal = ExternalSignalEvent(
        event_id=EventId("weather-signal"),
        source="fixture-weather",
        exchange_ts=now,
        received_at=now,
        schema_version="weather-prob-v1",
        payload={
            "implied_prob": "0.75",
            "target_time": now.isoformat(),
            "instrument_id": {
                "venue": "kalshi",
                "market_id": instrument.market_id,
            },
        },
        provenance=EventProvenance(source="fixture", channel="external"),
    )

    assert list(strategy.on_event(_book_event("0.70", "0.75", "100", "100", instrument), ctx)) == [
        NoAction(reason="book_updated:no_signal")
    ]
    assert list(strategy.on_event(signal, ctx)) == [
        NoAction(reason="edge_below_executable_threshold")
    ]

    tick_decisions = list(strategy.on_event(_book_event("0.50", "0.52", "100", "100", instrument), ctx))
    orders = [decision for decision in tick_decisions if isinstance(decision, PlaceOrder)]
    assert len(orders) == 1
    assert orders[0].outcome_side is OutcomeSide.YES
    assert orders[0].price == Decimal("0.52")
    assert orders[0].quantity <= Decimal("10")


def test_obi_scalper_handles_concurrent_cancel_decisions() -> None:
    spec = replace(
        _spec(),
        strategy_id=StrategyId("obi-v1"),
        name="microstructure_obi_scalper",
        parameters={
            "imbalance_threshold": "0.70",
            "cancel_threshold": "0.30",
            "max_spread_bps": "1000",
        },
    )
    strategy = MicrostructureObiScalperStrategy(spec)
    ctx = InMemoryContext(
        strategy_id_value=spec.strategy_id,
        sleeve_id_value=_sleeve().sleeve_id,
        clock_now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    buy = strategy.on_event(_book_event("0.50", "0.51", "90", "10"), ctx)
    assert len(buy) == 1
    assert isinstance(buy[0], PlaceOrder)
    strategy.on_feedback(
        StrategyFeedback(
            kind="IntentAccepted",
            envelope=IntentEnvelope(
                decision=buy[0],
                strategy_id=spec.strategy_id,
                sleeve_id=_sleeve().sleeve_id,
                correlation_id=CorrelationId("obi-feedback"),
                emitted_at=ctx.now,
            ),
            client_order_id=buy[0].client_order_id,
        ),
        ctx,
    )

    first_cancel = strategy.on_event(_book_event("0.50", "0.51", "10", "90"), ctx)
    assert first_cancel[0].__class__.__name__ == "CancelOrder"
    strategy.on_feedback(
        StrategyFeedback(
            kind="IntentAccepted",
            envelope=IntentEnvelope(
                decision=first_cancel[0],
                strategy_id=spec.strategy_id,
                sleeve_id=_sleeve().sleeve_id,
                correlation_id=CorrelationId("obi-cancel-feedback"),
                emitted_at=ctx.now,
            ),
            client_order_id=buy[0].client_order_id,
        ),
        ctx,
    )
    second_cancel = strategy.on_event(_book_event("0.50", "0.51", "10", "90"), ctx)

    assert second_cancel[0].__class__.__name__ == "NoAction"


def test_obi_scalper_buy_crosses_the_spread_and_fills() -> None:
    # V6-T4: the buy must cross (price at the ask) so the IOC taker actually
    # fills, instead of resting-bound at the bid and cancelling with zero fill.
    from eventcontracts.adapters.venues.kalshi import KalshiFeeModel
    from eventcontracts.execution import (
        ConstantLatency,
        FractionalQueueEstimator,
        MarketPaperSimulator,
    )

    spec = replace(
        _spec(),
        strategy_id=StrategyId("obi-v1"),
        name="microstructure_obi_scalper",
        parameters={
            "imbalance_threshold": "0.70",
            "max_spread_bps": "1000",
            "clip_size": "5",
        },
    )
    strategy = MicrostructureObiScalperStrategy(spec)
    ctx = InMemoryContext(
        strategy_id_value=spec.strategy_id,
        sleeve_id_value=_sleeve().sleeve_id,
        clock_now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    book_event = _book_event("0.50", "0.51", "90", "10")
    decisions = strategy.on_event(book_event, ctx)
    place = decisions[0]
    assert isinstance(place, PlaceOrder)
    assert place.order_side is OrderSide.BUY
    assert place.price == Decimal("0.51")  # crosses the ask, not parked at the bid

    simulator = MarketPaperSimulator(
        fee_model=KalshiFeeModel(),
        latency=ConstantLatency(submit_ms=0.0),
        queue_estimator=FractionalQueueEstimator(fraction=Decimal("1.0")),
        strategy_id=spec.strategy_id,
        sleeve_id=_sleeve().sleeve_id,
    )
    simulator.on_event(book_event)
    envelope = IntentEnvelope(
        decision=place,
        strategy_id=spec.strategy_id,
        sleeve_id=_sleeve().sleeve_id,
        correlation_id=CorrelationId("obi-fill"),
        emitted_at=ctx.now,
    )
    fills = simulator.submit_envelope(envelope)
    assert len(fills) == 1
    assert fills[0].price == Decimal("0.51")
    assert fills[0].quantity == Decimal("5")


def test_runner_applies_strategy_default_execution_priority() -> None:
    priority = ExecutionPriority(
        tier=LatencyTier.FAST,
        max_delay_ms=100,
        expires_after_ms=250,
        allow_rate_limit_borrow=True,
        reason="latency-sensitive crypto edge",
    )
    spec = replace(_spec(), default_execution_priority=priority)
    sleeve = _sleeve()
    strategy = create("example_threshold", spec)
    instrument = InstrumentId(venue=Venue.KALSHI, market_id="DEMO-1")
    sink = InMemoryIntentSink()
    clock = InMemoryClock()
    ctx = InMemoryContext(
        strategy_id_value=spec.strategy_id,
        sleeve_id_value=sleeve.sleeve_id,
        clock_now=clock.now(),
    )
    runner = StrategyRunner(
        spec=spec,
        sleeve=sleeve,
        strategy=strategy,
        events=InMemoryEventSource(events=(_quote("0.09", "0.10", instrument), _trade("0.10", instrument))),
        sink=sink,
        risk=AllowAllRiskGate(),
        clock=clock,
        context_provider=StaticContextProvider(ctx=ctx),
    )

    summary = runner.run()

    place_orders = [e for e in sink.emitted if isinstance(e.decision, PlaceOrder)]
    assert summary.intents_dispatched == 2
    assert place_orders[0].priority == priority


def test_runner_attaches_snapshot_and_reserves_cash_between_decisions() -> None:
    class TwoOrderStrategy(StrategyBase):
        def on_event(self, event, ctx):  # type: ignore[no-untyped-def]
            del event, ctx
            instrument = InstrumentId(venue=Venue.KALSHI, market_id="DEMO-1")
            return (
                _place(instrument, "a"),
                _place(instrument, "b"),
            )

    spec = _spec()
    sleeve = _sleeve()
    strategy = TwoOrderStrategy(spec)
    instrument = InstrumentId(venue=Venue.KALSHI, market_id="DEMO-1")
    sink = InMemoryIntentSink()
    clock = InMemoryClock()
    provider = StatefulContextProvider(
        strategy_id=spec.strategy_id,
        sleeve_id=sleeve.sleeve_id,
        currency=sleeve.currency,
        starting_cash=Decimal("100"),
        clock=clock.now,
    )
    runner = StrategyRunner(
        spec=spec,
        sleeve=sleeve,
        strategy=strategy,
        events=InMemoryEventSource(events=(_quote("0.59", "0.60", instrument),)),
        sink=sink,
        risk=SleeveRiskGate(sleeve=sleeve),
        clock=clock,
        context_provider=provider,
    )

    summary = runner.run()

    assert summary.intents_dispatched == 1
    assert summary.intents_rejected == 1
    assert summary.rejection_reasons == {"available_cash": 1}
    assert isinstance(sink.emitted[0].decision, PlaceOrder)
    assert sink.emitted[0].decision.market_snapshot is not None
    assert provider.context().cash("USD").held_for_orders == Decimal("60.00")


def test_runner_supports_stepwise_live_adapter_flow() -> None:
    spec = _spec()
    sleeve = _sleeve()
    strategy = create("example_threshold", spec)
    instrument = InstrumentId(venue=Venue.KALSHI, market_id="DEMO-1")
    sink = InMemoryIntentSink()
    clock = InMemoryClock()
    ctx = InMemoryContext(
        strategy_id_value=spec.strategy_id,
        sleeve_id_value=sleeve.sleeve_id,
        clock_now=clock.now(),
    )
    runner = StrategyRunner(
        spec=spec,
        sleeve=sleeve,
        strategy=strategy,
        events=InMemoryEventSource(events=()),
        sink=sink,
        risk=AllowAllRiskGate(),
        clock=clock,
        context_provider=StaticContextProvider(ctx=ctx),
    )

    runner.start()
    runner.process_event(_quote("0.09", "0.10", instrument))
    result = runner.process_event(_trade("0.10", instrument))
    runner.stop()

    assert result.events_processed == 1
    assert result.decisions_emitted == 1
    assert result.intents_dispatched == 1
    assert len(sink.emitted) == 2
    assert runner.state is StrategyState.DISPOSED


def _place(instrument: InstrumentId, suffix: str) -> PlaceOrder:
    return PlaceOrder(
        client_order_id=ClientOrderId(f"co-{suffix}"),
        instrument_id=instrument,
        outcome_side=OutcomeSide.YES,
        order_side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTD,
        quantity=Decimal("100"),
        price=Decimal("0.60"),
        expires_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=1),
    )
