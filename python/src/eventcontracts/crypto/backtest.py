"""Real-data settlement PnL backtest for the crypto signal ensemble.

This module is the **only** place strategy decisions get translated
into realized PnL. It uses *exclusively* real Kalshi market data:

* every order fills against the Kalshi bid/ask observed at the
  decision timestamp (taker fill — pay the ask for BUY YES, pay
  ``1 - bid`` for BUY NO);
* fees use the :class:`KalshiFeeModel` taker rate;
* settlement is the venue's own ``result`` field on the bracket
  market — no synthetic settle prices, no Deribit-derived outcomes.

A bracket's PnL per contract::

    payout = 1 if (market.result == outcome_side.value) else 0
    pnl = payout - fill_price - fee_amount

Aggregating across many cohorts answers "does any edge survive
fees on real Kalshi BTC hourly markets?" honestly.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_DOWN, Decimal

from eventcontracts.adapters.venues.kalshi import KalshiFeeModel
from eventcontracts.crypto.historical import (
    CohortSettlement,
    HistoricalStream,
)
from eventcontracts.domain.decisions import (
    Alert,
    NoAction,
    PlaceOrder,
    StrategyDecision,
)
from eventcontracts.domain.events import (
    ExternalSignalEvent,
    NormalizedEvent,
    QuoteEvent,
)
from eventcontracts.domain.fees import FillContext
from eventcontracts.domain.models import OutcomeSide
from eventcontracts.strategy.base import Strategy
from eventcontracts.strategy.context import StrategyContext


@dataclass(frozen=True)
class SizingPolicy:
    """How many contracts each PlaceOrder turns into when it fills.

    * ``mode="flat_contracts"`` — qty is exactly ``params["size"]``.
      Maximum loss per fill is ``size * fill_price``, which varies
      with strike. Useful for parity arbitrage where every leg must
      carry the same contract count, harmful for a directional
      strategy because ATM losers dominate the loss distribution.

    * ``mode="fixed_premium"`` — qty is sized so every fill risks at
      most ``params["dollars"]``. ``qty = floor(dollars / fill_price)``
      with a floor of 1 contract. Equalizes the worst-case dollar
      loss across strikes and is the standard sizing approach for
      retail binary markets.

    * ``mode="fixed_payout"`` — qty sized so every *winning* fill
      pays ``params["dollars"]`` net of premium. ``qty = floor(
      dollars / (1 - fill_price))`` with a floor of 1 contract.
      Equalizes the upside per trade, leaving the downside larger
      when fill prices are close to 1.
    """

    mode: str
    params: Mapping[str, Decimal]

    def quantity_for(self, fill_price: Decimal, intent_quantity: Decimal) -> Decimal:
        if self.mode == "flat_contracts":
            return intent_quantity
        if self.mode == "fixed_premium":
            dollars = self.params.get("dollars", Decimal("1"))
            if fill_price <= 0:
                return Decimal("1")
            qty = (dollars / fill_price).to_integral_value(rounding=ROUND_DOWN)
            return max(Decimal("1"), qty)
        if self.mode == "fixed_payout":
            dollars = self.params.get("dollars", Decimal("1"))
            denom = Decimal("1") - fill_price
            if denom <= 0:
                return Decimal("1")
            qty = (dollars / denom).to_integral_value(rounding=ROUND_DOWN)
            return max(Decimal("1"), qty)
        raise ValueError(f"unknown sizing mode: {self.mode}")


@dataclass
class FilledTrade:
    """One realized fill produced by the backtester."""

    market_id: str
    outcome_side: OutcomeSide
    fill_price: Decimal
    quantity: Decimal
    fee_amount: Decimal
    fee_currency: str
    payout_per_contract: Decimal  # 1 if settles in our favor else 0
    pnl_per_contract: Decimal     # payout - fill_price - per_contract_fee
    pnl_total: Decimal            # pnl_per_contract * quantity
    sources: tuple[str, ...]      # contributing source names from the verdict
    decision_at: datetime
    settled: bool                 # False when the bracket has no ``result``


@dataclass
class CohortBacktestResult:
    """Aggregate result for one cohort's ensemble run + settlement."""

    expiry_at: datetime
    yes_market_ticker: str | None
    settlement_price: Decimal | None
    fills: list[FilledTrade] = field(default_factory=list)
    decisions_counter: Counter[str] = field(default_factory=Counter)
    skipped_no_quote: int = 0
    skipped_unsettled: int = 0

    @property
    def total_pnl(self) -> Decimal:
        return sum((f.pnl_total for f in self.fills), Decimal("0"))

    @property
    def total_fees(self) -> Decimal:
        return sum((f.fee_amount for f in self.fills), Decimal("0"))

    @property
    def settled_fills(self) -> list[FilledTrade]:
        return [f for f in self.fills if f.settled]

    @property
    def win_rate(self) -> float:
        settled = self.settled_fills
        if not settled:
            return 0.0
        wins = sum(1 for f in settled if f.pnl_total > 0)
        return wins / len(settled)


def run_cohort_backtest(
    *,
    stream: HistoricalStream,
    settlement: CohortSettlement,
    strategy: Strategy,
    ctx: StrategyContext,
    fee_model: KalshiFeeModel | None = None,
    apply_clock_to_ctx: bool = True,
    sizing: SizingPolicy | None = None,
    one_fill_per_market: bool = False,
) -> CohortBacktestResult:
    """Run a strategy through one cohort's events and settle the resulting fills.

    The function is deliberately strict about realism:

    * Only ``PlaceOrder`` decisions targeting a Kalshi market the
      cohort actually lists become fills. A decision pointing at a
      market with no quote at the decision timestamp is skipped
      (``skipped_no_quote``).
    * Each fill uses the *current* Kalshi best ask / 1 - best bid
      observed when the decision was emitted — never a synthesized
      mid or implied price.
    * Settlement consults ``settlement.bracket_results`` only.
      Decisions on a bracket missing from the result map are dropped
      from PnL accounting (``skipped_unsettled``).
    """

    fee_model = fee_model or KalshiFeeModel()
    sizing = sizing or SizingPolicy(mode="flat_contracts", params={})
    result = CohortBacktestResult(
        expiry_at=stream.expiry_at,  # type: ignore[arg-type]
        yes_market_ticker=settlement.yes_market_ticker,
        settlement_price=settlement.settlement_price,
    )

    # Quote cache so we can look up the bid/ask at decision time.
    latest_quote: dict[str, tuple[Decimal, Decimal, datetime]] = {}

    # Helper: classify a decision into a counter bucket.
    def _bump(d: StrategyDecision) -> None:
        if isinstance(d, PlaceOrder):
            result.decisions_counter[f"place_{d.outcome_side.value}"] += 1
            result.decisions_counter["place_total"] += 1
        elif isinstance(d, Alert):
            result.decisions_counter["alert"] += 1
        elif isinstance(d, NoAction):
            result.decisions_counter["no_action"] += 1

    pending_sources: dict[str, tuple[str, ...]] = {}
    already_filled: set[str] = set()
    for event in stream.events:
        ts = _event_time(event)
        if apply_clock_to_ctx and ts is not None and hasattr(ctx, "clock_now"):
            ctx.clock_now = ts
        if isinstance(event, QuoteEvent):
            quote = event.quote
            if quote.bid is not None and quote.ask is not None:
                latest_quote[quote.instrument_id.market_id] = (
                    quote.bid.price,
                    quote.ask.price,
                    quote.received_at,
                )

        decisions = strategy.on_event(event, ctx)
        # Track the latest Alert per (market, source list) so the
        # next PlaceOrder picks up the breakdown for attribution.
        last_alert_sources: tuple[str, ...] = ()
        for d in decisions:
            _bump(d)
            if isinstance(d, Alert):
                last_alert_sources = tuple(
                    s.strip() for s in d.tags.get("sources", "").split(",") if s.strip()
                )
            if isinstance(d, PlaceOrder):
                if one_fill_per_market and d.instrument_id.market_id in already_filled:
                    continue
                fill = _fill_decision(
                    decision=d,
                    decision_at=ts or stream.expiry_at,  # type: ignore[arg-type]
                    latest_quote=latest_quote,
                    settlement=settlement,
                    fee_model=fee_model,
                    sources=last_alert_sources or pending_sources.get(d.instrument_id.market_id, ()),
                    sizing=sizing,
                )
                if fill is None:
                    result.skipped_no_quote += 1
                    continue
                if not fill.settled:
                    result.skipped_unsettled += 1
                result.fills.append(fill)
                if one_fill_per_market:
                    already_filled.add(d.instrument_id.market_id)

    return result


def _event_time(event: NormalizedEvent) -> datetime | None:
    if isinstance(event, ExternalSignalEvent):
        return event.received_at
    if isinstance(event, QuoteEvent):
        return event.quote.received_at
    return None


def _fill_decision(
    *,
    decision: PlaceOrder,
    decision_at: datetime,
    latest_quote: dict[str, tuple[Decimal, Decimal, datetime]],
    settlement: CohortSettlement,
    fee_model: KalshiFeeModel,
    sources: tuple[str, ...],
    sizing: SizingPolicy,
) -> FilledTrade | None:
    market_id = decision.instrument_id.market_id
    quote = latest_quote.get(market_id)
    if quote is None:
        return None
    bid, ask, _quote_at = quote

    # Taker fill: BUY YES pays the ask; BUY NO pays (1 - bid).
    if decision.outcome_side is OutcomeSide.YES:
        if ask <= 0 or ask >= Decimal("1"):
            return None
        fill_price = ask
    else:
        if bid < 0 or bid >= Decimal("1"):
            return None
        fill_price = Decimal("1") - bid
        if fill_price <= 0:
            return None

    quantity = sizing.quantity_for(fill_price, decision.quantity)
    fee_ctx = FillContext(
        instrument_id=decision.instrument_id,
        side=decision.outcome_side,
        price=fill_price,
        quantity=quantity,
        liquidity="taker",
    )
    fee_est = fee_model.estimate(fee_ctx)
    fee_per_contract = (
        fee_est.amount / quantity if quantity > 0 else Decimal("0")
    )

    venue_result = settlement.bracket_results.get(market_id)
    settled = venue_result is not None
    if settled:
        won = venue_result == decision.outcome_side.value
        payout = Decimal("1") if won else Decimal("0")
        pnl_per_contract = payout - fill_price - fee_per_contract
    else:
        payout = Decimal("0")
        pnl_per_contract = Decimal("0")

    return FilledTrade(
        market_id=market_id,
        outcome_side=decision.outcome_side,
        fill_price=fill_price,
        quantity=quantity,
        fee_amount=fee_est.amount,
        fee_currency=fee_est.currency,
        payout_per_contract=payout,
        pnl_per_contract=pnl_per_contract,
        pnl_total=pnl_per_contract * quantity,
        sources=sources,
        decision_at=decision_at,
        settled=settled,
    )


@dataclass
class WalkForwardReport:
    """Aggregate metrics across a sequence of cohort backtests."""

    cohorts: list[CohortBacktestResult] = field(default_factory=list)

    @property
    def total_pnl(self) -> Decimal:
        return sum((c.total_pnl for c in self.cohorts), Decimal("0"))

    @property
    def total_fees(self) -> Decimal:
        return sum((c.total_fees for c in self.cohorts), Decimal("0"))

    @property
    def total_fills(self) -> int:
        return sum(len(c.fills) for c in self.cohorts)

    @property
    def total_settled_fills(self) -> int:
        return sum(len(c.settled_fills) for c in self.cohorts)

    @property
    def win_rate(self) -> float:
        settled = [f for c in self.cohorts for f in c.settled_fills]
        if not settled:
            return 0.0
        return sum(1 for f in settled if f.pnl_total > 0) / len(settled)

    @property
    def per_source_pnl(self) -> dict[str, Decimal]:
        agg: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        for cohort in self.cohorts:
            for fill in cohort.settled_fills:
                if not fill.sources:
                    agg["unattributed"] += fill.pnl_total
                else:
                    for source in fill.sources:
                        agg[source] += fill.pnl_total / Decimal(len(fill.sources))
        return dict(agg)
