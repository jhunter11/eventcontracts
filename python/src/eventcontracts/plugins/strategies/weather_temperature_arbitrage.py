"""Spatiotemporal temperature arbitrage strategy.

Hypothesis (per docs/strategy-specs.md #1): retail traders over-extrapolate
morning temperatures and ignore systemic afternoon cloud-cover dynamics that
cap daily highs. The strategy reads a point-in-time weather forecast
(Open-Meteo / NOAA HRRR ingested as `ExternalSignalEvent`) and compares the
forecast-implied probability of hitting a Kalshi temperature bracket against
the current market mid. When the edge exceeds `min_edge_bps`, it submits a
passive limit order.

Implementation note: the spec lists a "Spatiotemporal Transformer" as the
model. The model pipeline is still scaffolded in this repo, so this module
runs in **rules mode**: the `ExternalSignalEvent.payload["implied_prob"]`
field is treated as the forecast-implied probability directly. Once the model
runner is implemented, swap the rules-mode read for `ctx.predict(...)`. The
decision shape (PlaceOrder vs NoAction) is unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from uuid import uuid4

from eventcontracts.domain.decisions import (
    NoAction,
    PlaceOrder,
    StrategyDecision,
)
from eventcontracts.domain.events import (
    ExternalSignalEvent,
    NormalizedEvent,
    OrderBookEvent,
    QuoteEvent,
    SettlementResolvedEvent,
    market_snapshot_from_book_event,
    market_snapshot_from_quote_event,
)
from eventcontracts.domain.ids import ClientOrderId
from eventcontracts.domain.models import InstrumentId, MarketSnapshot, OrderBookLevel, OutcomeSide
from eventcontracts.domain.orders import OrderSide, OrderType, TimeInForce
from eventcontracts.domain.spec import StrategySpec
from eventcontracts.strategy.base import StrategyBase, StrategyFeedback
from eventcontracts.strategy.context import StrategyContext
from eventcontracts.strategy.pricing import buy_limit_from_fair
from eventcontracts.strategy.registry import register


def _fair_price_4dp(value: Decimal) -> str:
    """Cap a fair value at 4 decimal places (the 1e4 grid the risk gate, gateway
    `parse_fixed_4` and Kalshi's whole-cent settlement all require). A raw model
    probability like 0.967731 would otherwise stringify to >4 dp and be rejected
    as InvalidNumeric. Mirrors the Rust `format_decimal_ticks` fix and the tennis
    strategy's `_fair_price_4dp` for Python<->Rust parity.
    """
    quantized = value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    text = format(quantized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"



class WeatherTemperatureArbitrageStrategy(StrategyBase):
    """Edge-based passive limit order on Kalshi temperature brackets."""

    def __init__(self, spec: StrategySpec) -> None:
        super().__init__(spec)
        self.min_edge_bps = Decimal(str(spec.parameters.get("min_edge_bps", "75")))
        self.max_size = Decimal(str(spec.parameters.get("max_size", "10")))
        self.order_ttl_ms = int(spec.parameters.get("order_ttl_ms", 5000))
        self.kelly_fraction = Decimal(str(spec.parameters.get("kelly_fraction", "0.10")))
        self.capital_base = Decimal(str(spec.parameters.get("capital_base", "0")))
        self.capital_source = str(spec.parameters.get("capital_source", "context_cash")).lower()
        self.allow_static_capital_base = self.capital_source in {"static", "spec", "capital_base"} or _truthy(
            spec.parameters.get("allow_static_capital_base", "false")
        )
        self.max_trade_capital_fraction = Decimal(str(spec.parameters.get("max_trade_capital_fraction", "0")))
        self.max_ladder_capital_fraction = Decimal(str(spec.parameters.get("max_ladder_capital_fraction", "0")))
        self.max_active_capital_fraction = Decimal(str(spec.parameters.get("max_active_capital_fraction", "0")))
        self.signal_source = str(spec.parameters.get("signal_source", "open-meteo"))
        self.currency = str(spec.parameters.get("currency", "USD")).upper()
        self.execution_mode = str(spec.parameters.get("execution_mode", "passive_mid"))
        self.max_spread = Decimal(str(spec.parameters.get("max_spread", "1.00")))
        self.spread_edge_multiplier = Decimal(str(spec.parameters.get("spread_edge_multiplier", "0.00")))
        self.near_binary_price = Decimal(str(spec.parameters.get("near_binary_price", "1.00")))
        self.near_binary_min_edge_bps = Decimal(str(spec.parameters.get("near_binary_min_edge_bps", "0")))
        self.max_ladder_notional = Decimal(str(spec.parameters.get("max_ladder_notional", "1000000")))
        self.max_ladder_orders = int(spec.parameters.get("max_ladder_orders", 1_000_000))
        self.max_ticker_orders = int(spec.parameters.get("max_ticker_orders", 1_000_000))
        self.min_retrade_price_delta = Decimal(str(spec.parameters.get("min_retrade_price_delta", "0.00")))
        self.min_retrade_probability_delta = Decimal(str(spec.parameters.get("min_retrade_probability_delta", "0.00")))
        self.quote_triggered_trading = _truthy(spec.parameters.get("quote_triggered_trading", "false"))
        self.max_signal_age_seconds = int(spec.parameters.get("max_signal_age_seconds", 900))
        # Don't open new positions within this many seconds of market close (0 = off).
        # The producer carries the absolute `close_time` in the payload; remaining
        # time is recomputed against ctx.now on every decision, including re-fires.
        self.min_seconds_to_close = int(spec.parameters.get("min_seconds_to_close", 0))
        # Last known market mid per instrument; updated on QuoteEvent.
        self._mid_by_instrument: dict[InstrumentId, Decimal] = {}
        self._quote_by_instrument: dict[InstrumentId, tuple[Decimal, Decimal]] = {}
        self._snapshot_by_instrument: dict[InstrumentId, MarketSnapshot] = {}
        self._latest_signal_by_instrument: dict[InstrumentId, _LatestWeatherSignal] = {}
        self._submitted_notional_by_ladder: dict[str, Decimal] = {}
        self._submitted_notional_by_instrument: dict[InstrumentId, Decimal] = {}
        self._active_notional = Decimal("0")
        self._submitted_orders_by_ladder: dict[str, int] = {}
        self._submitted_orders_by_instrument: dict[InstrumentId, int] = {}
        self._last_trade_signal_by_instrument: dict[InstrumentId, tuple[Decimal, Decimal]] = {}
        self._pending_notional_by_client_order_id: dict[ClientOrderId, _PendingWeatherOrder] = {}
        self._accepted_notional_by_client_order_id: dict[ClientOrderId, _PendingWeatherOrder] = {}

    def on_event(self, event: NormalizedEvent, ctx: StrategyContext) -> Sequence[StrategyDecision]:
        if isinstance(event, SettlementResolvedEvent):
            released = self._release_settled_notional(event.settlement.instrument_id)
            return (NoAction(reason=f"settlement_released_notional_{released}"),)
        if isinstance(event, QuoteEvent):
            self._track_mid(event)
            if not self.quote_triggered_trading:
                return (NoAction(reason="quote_mid_updated"),)
            return self._quote_tick_decision(event, ctx)
        if isinstance(event, OrderBookEvent):
            self._track_book(event)
            if not self.quote_triggered_trading:
                return (NoAction(reason="book_updated"),)
            return self._book_tick_decision(event, ctx)
        if not isinstance(event, ExternalSignalEvent):
            return (NoAction(reason="ignored:not_external_signal"),)
        if event.source != self.signal_source:
            return (NoAction(reason=f"ignored:source!={self.signal_source}"),)

        # Defense in depth: never trade a signal that prices a non-current day. The
        # KXHIGH producer already suppresses lead!=0 (its calibration sigma is
        # nowcast-lead and over-confident for future days), but other producers
        # might not. When `lead_days` is absent the signal predates this contract
        # and is allowed through unchanged.
        lead_days = event.payload.get("lead_days")
        if lead_days is not None:
            try:
                lead_int = int(lead_days)
            except (TypeError, ValueError):
                return (NoAction(reason="censored:lead_days_unparsable"),)
            if lead_int != 0:
                return (NoAction(reason=f"censored:lead_days_{lead_int}"),)

        implied_prob = event.payload.get("implied_prob")
        instrument_payload = event.payload.get("instrument_id")
        if implied_prob is None or instrument_payload is None:
            return (NoAction(reason="censored:missing_forecast_or_instrument"),)

        try:
            forecast_prob = Decimal(str(implied_prob))
        except (ValueError, ArithmeticError):
            return (NoAction(reason="censored:forecast_unparsable"),)
        if not Decimal("0") <= forecast_prob <= Decimal("1"):
            return (NoAction(reason="censored:forecast_out_of_range"),)

        instrument_id = self._instrument_from_payload(instrument_payload)
        if instrument_id is None:
            return (NoAction(reason="censored:instrument_unresolved"),)

        self._latest_signal_by_instrument[instrument_id] = _LatestWeatherSignal(
            forecast_prob=forecast_prob,
            payload=event.payload,
            received_at=event.received_at,
        )

        return self._forecast_decision(
            instrument_id,
            forecast_prob,
            event.payload,
            ctx,
            no_action_suffix="",
        )

    def _quote_tick_decision(self, event: QuoteEvent, ctx: StrategyContext) -> Sequence[StrategyDecision]:
        return self._latest_tick_decision(
            event.quote.instrument_id,
            ctx,
            prefix="quote_mid_updated",
            no_action_suffix=":quote_tick",
        )

    def _book_tick_decision(self, event: OrderBookEvent, ctx: StrategyContext) -> Sequence[StrategyDecision]:
        return self._latest_tick_decision(
            event.book.instrument_id,
            ctx,
            prefix="book_updated",
            no_action_suffix=":book_tick",
        )

    def _latest_tick_decision(
        self,
        instrument_id: InstrumentId,
        ctx: StrategyContext,
        *,
        prefix: str,
        no_action_suffix: str,
    ) -> Sequence[StrategyDecision]:
        latest = self._latest_signal_by_instrument.get(instrument_id)
        if latest is None:
            return (NoAction(reason=f"{prefix}:no_signal"),)
        age = max((ctx.now - latest.received_at).total_seconds(), 0.0)
        if age > self.max_signal_age_seconds:
            return (NoAction(reason=f"{prefix}:signal_stale_{int(age)}s"),)
        return self._forecast_decision(
            instrument_id,
            latest.forecast_prob,
            latest.payload,
            ctx,
            no_action_suffix=no_action_suffix,
        )

    def _forecast_decision(
        self,
        instrument_id: InstrumentId,
        forecast_prob: Decimal,
        payload: Mapping[str, object],
        ctx: StrategyContext,
        *,
        no_action_suffix: str,
    ) -> Sequence[StrategyDecision]:
        if self.min_seconds_to_close > 0:
            seconds_left = _seconds_until(payload.get("close_time"), ctx.now)
            if seconds_left is not None and seconds_left < self.min_seconds_to_close:
                return (
                    NoAction(reason=f"censored:within_{self.min_seconds_to_close}s_of_close{no_action_suffix}"),
                )
        mid = self._mid_by_instrument.get(instrument_id)
        if mid is None:
            return (NoAction(reason="warmup:no_mid_yet"),)

        capital_base = self._available_capital(ctx)
        if capital_base <= 0:
            return (NoAction(reason="censored:no_available_cash"),)

        if self.execution_mode == "taker_if_edge":
            decision = self._taker_decision(
                instrument_id,
                forecast_prob,
                payload,
                capital_base,
                now=ctx.now,
            )
            if decision is None:
                return (NoAction(reason=f"edge_below_executable_threshold{no_action_suffix}"),)
            return (decision,)

        edge_bps = (forecast_prob - mid) * Decimal("10000")
        if abs(edge_bps) < self.min_edge_bps:
            return (NoAction(reason=f"edge_below_threshold{no_action_suffix}"),)

        side = OutcomeSide.YES if edge_bps > 0 else OutcomeSide.NO
        order_side = OrderSide.BUY
        # V6-C3: floor the per-side BUY limit to the venue cent (edge-preserving,
        # venue-valid). Raw mid can be a half-cent on an odd tick-sum book.
        limit_price = buy_limit_from_fair(mid if side is OutcomeSide.YES else Decimal("1") - mid)
        size = self._size_for_edge(edge_bps=edge_bps, price=limit_price, capital_base=capital_base)
        if size <= 0:
            return (NoAction(reason="sizing_zero"),)

        return (
            PlaceOrder(
                client_order_id=ClientOrderId(uuid4().hex),
                instrument_id=instrument_id,
                outcome_side=side,
                order_side=order_side,
                order_type=OrderType.LIMIT,
                time_in_force=TimeInForce.GTD,
                quantity=size,
                price=limit_price,
                expires_at=ctx.now + timedelta(milliseconds=self.order_ttl_ms),
                market_snapshot=_snapshot_for_side(
                    self._snapshot_by_instrument.get(instrument_id),
                    side,
                ),
                reason=f"edge_{edge_bps:+.0f}bps_vs_mid_{mid}",
                expected_edge_bps=edge_bps,
            ),
        )

    def _track_mid(self, event: QuoteEvent) -> None:
        quote = event.quote
        if quote.bid is None or quote.ask is None:
            return
        mid = (quote.bid.price + quote.ask.price) / Decimal("2")
        self._mid_by_instrument[quote.instrument_id] = mid
        self._quote_by_instrument[quote.instrument_id] = (quote.bid.price, quote.ask.price)
        self._snapshot_by_instrument[quote.instrument_id] = market_snapshot_from_quote_event(event)

    def _track_book(self, event: OrderBookEvent) -> None:
        snapshot = market_snapshot_from_book_event(event, side=OutcomeSide.YES)
        if snapshot.bid is None or snapshot.ask is None:
            return
        mid = (snapshot.bid.price + snapshot.ask.price) / Decimal("2")
        self._mid_by_instrument[event.book.instrument_id] = mid
        self._quote_by_instrument[event.book.instrument_id] = (snapshot.bid.price, snapshot.ask.price)
        self._snapshot_by_instrument[event.book.instrument_id] = snapshot

    def _taker_decision(
        self,
        instrument_id: InstrumentId,
        forecast_prob: Decimal,
        payload: Mapping[str, object],
        capital_base: Decimal,
        now: datetime,
    ) -> PlaceOrder | None:
        quote = self._quote_by_instrument.get(instrument_id)
        if quote is None:
            return None
        yes_bid, yes_ask = quote
        spread = yes_ask - yes_bid
        if spread > self.max_spread:
            return None
        yes_edge_bps = (forecast_prob - yes_ask) * Decimal("10000")
        no_ask = Decimal("1") - yes_bid
        no_edge_bps = ((Decimal("1") - forecast_prob) - no_ask) * Decimal("10000")
        required_edge_bps = self._required_edge_bps(spread)
        if yes_ask >= self.near_binary_price:
            yes_edge_bps -= self.near_binary_min_edge_bps
        if no_ask >= self.near_binary_price:
            no_edge_bps -= self.near_binary_min_edge_bps
        if yes_edge_bps < required_edge_bps and no_edge_bps < required_edge_bps:
            return None
        if yes_edge_bps >= no_edge_bps:
            return self._place_order(
                instrument_id=instrument_id,
                side=OutcomeSide.YES,
                price=yes_ask,
                edge_bps=yes_edge_bps,
                reason_price=yes_ask,
                forecast_prob=forecast_prob,
                ladder_key=self._ladder_key(instrument_id, payload),
                capital_base=capital_base,
                now=now,
            )
        return self._place_order(
            instrument_id=instrument_id,
            side=OutcomeSide.NO,
            price=no_ask,
            edge_bps=no_edge_bps,
            reason_price=no_ask,
            forecast_prob=forecast_prob,
            ladder_key=self._ladder_key(instrument_id, payload),
            capital_base=capital_base,
            now=now,
        )

    def _place_order(
        self,
        *,
        instrument_id: InstrumentId,
        side: OutcomeSide,
        price: Decimal,
        edge_bps: Decimal,
        reason_price: Decimal,
        forecast_prob: Decimal,
        ladder_key: str,
        capital_base: Decimal,
        now: datetime,
    ) -> PlaceOrder | None:
        if not self._can_retrade(instrument_id, price, forecast_prob):
            return None
        size = self._size_for_edge(edge_bps=edge_bps, price=price, capital_base=capital_base)
        if size <= 0:
            return None
        notional = price * size
        if not self._can_add_ladder_exposure(ladder_key, instrument_id, notional, capital_base):
            return None
        if not self._can_add_active_exposure(notional, capital_base):
            return None
        client_order_id = ClientOrderId(uuid4().hex)
        fair_price = forecast_prob if side is OutcomeSide.YES else Decimal("1") - forecast_prob
        self._pending_notional_by_client_order_id[client_order_id] = _PendingWeatherOrder(
            ladder_key=ladder_key,
            instrument_id=instrument_id,
            notional=notional,
            price=price,
            forecast_prob=forecast_prob,
        )
        self._last_trade_signal_by_instrument[instrument_id] = (
            price,
            forecast_prob,
        )
        return PlaceOrder(
            client_order_id=client_order_id,
            instrument_id=instrument_id,
            outcome_side=side,
            order_side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.IOC,
            quantity=size,
            price=price,
            expires_at=now + timedelta(milliseconds=self.order_ttl_ms),
            market_snapshot=_snapshot_for_side(
                self._snapshot_by_instrument.get(instrument_id),
                side,
            ),
            reason=f"executable_edge_{edge_bps:+.0f}bps_vs_ask_{reason_price}",
            expected_edge_bps=edge_bps,
            metadata={
                "ladder_key": ladder_key,
                "fair_price": _fair_price_4dp(fair_price),
                "min_executable_edge_ticks": str(int(self.min_edge_bps)),
                "fee_rate_bps": "700",
            },
        )

    def on_feedback(self, feedback: StrategyFeedback, ctx: StrategyContext) -> None:
        del ctx
        if feedback.client_order_id is None:
            return
        if feedback.kind == "IntentAccepted":
            pending = self._pending_notional_by_client_order_id.pop(
                feedback.client_order_id, None
            )
            if pending is not None:
                self._record_accepted_order(feedback.client_order_id, pending)
            return
        if feedback.kind not in {"IntentRejected", "VenueRejected", "VenueTerminal"}:
            return
        pending = self._pending_notional_by_client_order_id.pop(
            feedback.client_order_id, None
        )
        if pending is not None:
            return
        accepted = self._accepted_notional_by_client_order_id.pop(
            feedback.client_order_id, None
        )
        if accepted is not None:
            self._release_accepted_order(accepted)

    def _record_accepted_order(
        self, client_order_id: ClientOrderId, pending: _PendingWeatherOrder
    ) -> None:
        self._accepted_notional_by_client_order_id[client_order_id] = pending
        self._submitted_notional_by_ladder[pending.ladder_key] = (
            self._submitted_notional_by_ladder.get(pending.ladder_key, Decimal("0"))
            + pending.notional
        )
        self._submitted_notional_by_instrument[pending.instrument_id] = (
            self._submitted_notional_by_instrument.get(
                pending.instrument_id, Decimal("0")
            )
            + pending.notional
        )
        self._active_notional += pending.notional
        self._submitted_orders_by_ladder[pending.ladder_key] = (
            self._submitted_orders_by_ladder.get(pending.ladder_key, 0) + 1
        )
        self._submitted_orders_by_instrument[pending.instrument_id] = (
            self._submitted_orders_by_instrument.get(pending.instrument_id, 0) + 1
        )
        self._last_trade_signal_by_instrument[pending.instrument_id] = (
            pending.price,
            pending.forecast_prob,
        )

    def _release_accepted_order(self, accepted: _PendingWeatherOrder) -> None:
        self._submitted_notional_by_ladder[accepted.ladder_key] = max(
            self._submitted_notional_by_ladder.get(accepted.ladder_key, Decimal("0"))
            - accepted.notional,
            Decimal("0"),
        )
        self._submitted_notional_by_instrument[accepted.instrument_id] = max(
            self._submitted_notional_by_instrument.get(
                accepted.instrument_id, Decimal("0")
            )
            - accepted.notional,
            Decimal("0"),
        )
        self._active_notional = max(
            self._active_notional - accepted.notional, Decimal("0")
        )
        self._submitted_orders_by_ladder[accepted.ladder_key] = max(
            self._submitted_orders_by_ladder.get(accepted.ladder_key, 0) - 1,
            0,
        )
        self._submitted_orders_by_instrument[accepted.instrument_id] = max(
            self._submitted_orders_by_instrument.get(accepted.instrument_id, 0) - 1,
            0,
        )

    def _required_edge_bps(self, spread: Decimal) -> Decimal:
        return self.min_edge_bps + (spread * Decimal("10000") * self.spread_edge_multiplier)

    def _size_for_edge(self, *, edge_bps: Decimal, price: Decimal, capital_base: Decimal) -> Decimal:
        if price <= 0 or capital_base <= 0:
            return Decimal("0")
        edge_fraction = min(abs(edge_bps) / Decimal("10000"), Decimal("1"))
        max_trade_notional = self._max_trade_notional(capital_base)
        if max_trade_notional is not None:
            target_notional = max_trade_notional * edge_fraction * self.kelly_fraction
            raw_size = (target_notional / price).to_integral_value(rounding=ROUND_FLOOR)
        else:
            raw_size = (edge_fraction * self.kelly_fraction * self.max_size).to_integral_value(rounding=ROUND_FLOOR)
        size = max(raw_size, Decimal("1"))
        size = min(size, self.max_size)
        return self._cap_size_by_trade_notional(size=size, price=price, capital_base=capital_base)

    def _cap_size_by_trade_notional(self, *, size: Decimal, price: Decimal, capital_base: Decimal) -> Decimal:
        if price <= 0:
            return Decimal("0")
        notional_cap = capital_base
        max_trade_notional = self._max_trade_notional(capital_base)
        if max_trade_notional is not None:
            notional_cap = min(notional_cap, max_trade_notional)
        max_size = (notional_cap / price).to_integral_value(rounding=ROUND_FLOOR)
        return min(size, max_size)

    def _available_capital(self, ctx: StrategyContext) -> Decimal:
        balance = ctx.cash(self.currency)
        if balance.available > 0:
            return balance.available
        if self.allow_static_capital_base and self.capital_base > 0:
            return self.capital_base
        return Decimal("0")

    def _max_trade_notional(self, capital_base: Decimal) -> Decimal | None:
        if capital_base <= 0 or self.max_trade_capital_fraction <= 0:
            return None
        return capital_base * self.max_trade_capital_fraction

    def _max_ladder_notional(self, capital_base: Decimal) -> Decimal:
        limit = self.max_ladder_notional
        if capital_base > 0 and self.max_ladder_capital_fraction > 0:
            limit = min(limit, capital_base * self.max_ladder_capital_fraction)
        return limit

    def _max_active_notional(self, capital_base: Decimal) -> Decimal | None:
        if capital_base <= 0 or self.max_active_capital_fraction <= 0:
            return None
        return capital_base * self.max_active_capital_fraction

    def _can_add_ladder_exposure(
        self,
        ladder_key: str,
        instrument_id: InstrumentId,
        notional: Decimal,
        capital_base: Decimal,
    ) -> bool:
        if self._submitted_orders_by_instrument.get(instrument_id, 0) >= self.max_ticker_orders:
            return False
        if self._submitted_orders_by_ladder.get(ladder_key, 0) >= self.max_ladder_orders:
            return False
        current = self._submitted_notional_by_ladder.get(ladder_key, Decimal("0"))
        return current + notional <= self._max_ladder_notional(capital_base)

    def _can_add_active_exposure(self, notional: Decimal, capital_base: Decimal) -> bool:
        max_active_notional = self._max_active_notional(capital_base)
        if max_active_notional is None:
            return True
        return self._active_notional + notional <= max_active_notional

    def _release_settled_notional(self, instrument_id: InstrumentId) -> Decimal:
        released = self._submitted_notional_by_instrument.pop(instrument_id, Decimal("0"))
        self._active_notional = max(self._active_notional - released, Decimal("0"))
        return released

    def _can_retrade(self, instrument_id: InstrumentId, price: Decimal, forecast_prob: Decimal) -> bool:
        previous = self._last_trade_signal_by_instrument.get(instrument_id)
        if previous is None:
            return True
        previous_price, previous_prob = previous
        return (
            abs(price - previous_price) >= self.min_retrade_price_delta
            or abs(forecast_prob - previous_prob) >= self.min_retrade_probability_delta
        )

    @staticmethod
    def _ladder_key(instrument_id: InstrumentId, payload: Mapping[str, object]) -> str:
        target_time = payload.get("target_time")
        ticker_root = instrument_id.market_id.split("-T", maxsplit=1)[0]
        return f"{ticker_root}:{target_time}" if isinstance(target_time, str) else ticker_root

    @staticmethod
    def _instrument_from_payload(payload: object) -> InstrumentId | None:
        if not isinstance(payload, Mapping):
            return None
        venue = payload.get("venue")
        market_id = payload.get("market_id")
        if not isinstance(venue, str) or not isinstance(market_id, str):
            return None
        from eventcontracts.domain.models import Venue

        try:
            venue_enum = Venue(venue)
        except ValueError:
            return None
        outcome_id = payload.get("outcome_id")
        outcome_str = outcome_id if isinstance(outcome_id, str) else None
        return InstrumentId(
            venue=venue_enum,
            market_id=market_id,
            outcome_id=outcome_str,
        )


@register("weather_temperature_arbitrage")
def factory(spec: StrategySpec) -> WeatherTemperatureArbitrageStrategy:
    return WeatherTemperatureArbitrageStrategy(spec)


def _truthy(value: object) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _seconds_until(close_value: object, now: datetime) -> float | None:
    """Seconds from `now` until an ISO-8601 close time, or None if unparsable/absent."""
    if not isinstance(close_value, str) or not close_value:
        return None
    try:
        close_dt = datetime.fromisoformat(close_value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if close_dt.tzinfo is None:
        close_dt = close_dt.replace(tzinfo=UTC)
    return (close_dt - now).total_seconds()


def _snapshot_for_side(
    snapshot: MarketSnapshot | None,
    side: OutcomeSide,
) -> MarketSnapshot | None:
    if snapshot is None or snapshot.side is side:
        return snapshot
    return MarketSnapshot(
        instrument_id=snapshot.instrument_id,
        side=side,
        bid=(
            OrderBookLevel(price=Decimal("1") - snapshot.ask.price, quantity=snapshot.ask.quantity)
            if snapshot.ask is not None
            else None
        ),
        ask=(
            OrderBookLevel(price=Decimal("1") - snapshot.bid.price, quantity=snapshot.bid.quantity)
            if snapshot.bid is not None
            else None
        ),
        exchange_ts=snapshot.exchange_ts,
        received_at=snapshot.received_at,
        source=snapshot.source,
        source_sequence=snapshot.source_sequence,
        sequence_gap=snapshot.sequence_gap,
        metadata=snapshot.metadata,
    )


@dataclass(frozen=True)
class _PendingWeatherOrder:
    ladder_key: str
    instrument_id: InstrumentId
    notional: Decimal
    price: Decimal
    forecast_prob: Decimal


@dataclass(frozen=True)
class _LatestWeatherSignal:
    forecast_prob: Decimal
    payload: Mapping[str, object]
    received_at: datetime
