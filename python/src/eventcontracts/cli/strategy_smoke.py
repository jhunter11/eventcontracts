"""V6-S2 no-trade smoke: prove a strategy produces >=1 risk-APPROVED intent.

The most dangerous silent failure for a promoted strategy is one that *runs* —
emits decisions, passes parity — yet dispatches **zero** intents because every
order is rejected by the risk gate (classically ``missing_market_snapshot`` when
the order fires on an External/Timer event and carries no executable BBO
evidence). Parity does not catch this: ``parity_check`` compares strategy
decisions, never the risk verdict.

This smoke replays the strategy's own parity-case events through the **real**
:class:`StrategyRunner` and :class:`SleeveRiskGate`, then asserts at least one
order-bearing intent was risk-APPROVED. If the strategy emits orders but all are
rejected, it fails with the dominant rejection reason — surfacing the bug before
real capital is committed.

The synthesized sleeve uses a deliberately permissive risk profile (generous
notionals, wide freshness window, GTC allowed): the gate question here is
*structural* ("can this strategy ever get an intent through?"), not capital
sizing or freshness tuning, which the per-sleeve config and other gates own.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from eventcontracts.config import load_strategy_spec
from eventcontracts.domain import (
    CashBalance,
    InstrumentId,
    OrderBookLevel,
    OutcomeSide,
    Quote,
    Venue,
)
from eventcontracts.domain.decisions import IntentEnvelope, PlaceOrder, ReplaceOrder
from eventcontracts.domain.events import (
    EventProvenance,
    ExternalSignalEvent,
    NormalizedEvent,
    QuoteEvent,
    TimerEvent,
)
from eventcontracts.domain.ids import EventId, SleeveId
from eventcontracts.domain.spec import RiskProfile, SleeveSpec
from eventcontracts.risk import DailyLossLedger, SleeveRiskGate
from eventcontracts.runner import StrategyRunner
from eventcontracts.runner.ports import RiskDecision
from eventcontracts.strategy import create_from_spec
from eventcontracts.testing import InMemoryClock, InMemoryContext, StaticContextProvider


@dataclass
class SmokeResult:
    orders_emitted: int = 0
    intents_approved: int = 0
    intents_rejected: int = 0
    rejection_reasons: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.orders_emitted > 0 and self.intents_approved > 0

    def dominant_reason(self) -> str | None:
        if not self.rejection_reasons:
            return None
        return max(self.rejection_reasons.items(), key=lambda kv: kv[1])[0]

    def summary(self) -> str:
        if self.orders_emitted == 0:
            return (
                "no order intents were emitted across the parity stream; cannot "
                "confirm the strategy produces a risk-approved intent"
            )
        if self.intents_approved > 0:
            return (
                f"{self.intents_approved} risk-approved intent(s), "
                f"{self.intents_rejected} rejected"
            )
        return (
            f"emitted {self.orders_emitted} order(s) but ALL were risk-rejected; "
            f"dominant reason: {self.dominant_reason()}"
        )


def _parse_dt(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _provenance_from_case(prov: dict[str, Any]) -> EventProvenance:
    venue = prov.get("venue")
    return EventProvenance(
        source=str(prov.get("source", "fixture")),
        channel=str(prov.get("channel", "fixture")),
        schema_version=str(prov.get("schema_version", "normalized-event-v1")),
        venue=Venue(venue) if isinstance(venue, str) and venue else None,
        source_sequence=(
            str(prov["source_sequence"]) if prov.get("source_sequence") is not None else None
        ),
        normalization_version=str(prov.get("normalization_version", "normalizer-v1")),
    )


def _instrument(raw: str) -> InstrumentId:
    venue_str, _, market_id = raw.partition(":")
    return InstrumentId(venue=Venue(venue_str), market_id=market_id)


def _event_from_case(case: dict[str, Any]) -> tuple[NormalizedEvent, datetime] | None:
    """Convert one parity case into (event, produced_at).

    Unsupported event kinds and malformed/placeholder cases (e.g. an empty ``{}``)
    return None so the smoke skips them rather than crashing — a strategy whose
    cases yield no runnable events simply produces zero intents and fails the gate
    on that basis, with a clear message, not a stack trace.
    """

    event = case.get("event")
    if not isinstance(event, dict):
        return None
    kind = event.get("event_kind")
    event_id = EventId(str(event.get("event_id", "smoke")))
    produced_at = (event.get("audit") or {}).get("produced_at")
    when = _parse_dt(produced_at) if isinstance(produced_at, str) else _now()
    provenance = _provenance_from_case(event.get("provenance", {}))
    try:
        payload = json.loads(event.get("payload_json", "{}"))
    except (json.JSONDecodeError, TypeError):
        return None

    built: NormalizedEvent | None = None
    if kind == "quote":
        built = QuoteEvent(
            event_id=event_id,
            quote=Quote(
                instrument_id=_instrument(str(payload["instrument"])),
                side=OutcomeSide.YES,
                bid=OrderBookLevel(price=Decimal(str(payload["bid"])), quantity=Decimal("100")),
                ask=OrderBookLevel(price=Decimal(str(payload["ask"])), quantity=Decimal("100")),
                exchange_ts=when,
                received_at=when,
            ),
            provenance=provenance,
        )
    elif kind == "external":
        built = ExternalSignalEvent(
            event_id=event_id,
            source=provenance.source,
            exchange_ts=when,
            received_at=when,
            schema_version=provenance.schema_version,
            payload=payload,
            provenance=provenance,
        )
    elif kind == "timer":
        built = TimerEvent(
            event_id=event_id,
            timestamp=when,
            label=str(payload.get("label", "timer")),
            provenance=provenance,
        )
    return (built, when) if built is not None else None


class _NullSink:
    def emit(self, envelope: IntentEnvelope) -> None:  # noqa: ARG002
        return None


class _NullSource:
    def stream(self) -> Any:
        return iter(())


def _permissive_sleeve(spec: Any) -> SleeveSpec:
    return SleeveSpec(
        sleeve_id=SleeveId("smoke-sleeve"),
        strategy_id=spec.strategy_id,
        strategy_version=spec.version,
        venue=Venue.KALSHI,
        capital_allocation=Decimal("1000000"),
        currency="USD",
        risk=RiskProfile(
            max_order_notional=Decimal("100000"),
            max_position_notional=Decimal("1000000"),
            max_daily_loss=Decimal("1000000"),
            max_open_orders=100000,
            max_gross_exposure=Decimal("100000000"),
            currency="USD",
            max_market_data_age_ms=86_400_000,
            max_spread=Decimal("1"),
            max_order_lifetime_ms=86_400_000,
            allow_unbounded_gtc=True,
        ),
    )


def run_no_trade_smoke(strategy_path: Path, parity_dir: Path) -> SmokeResult:
    """Replay the strategy's parity cases through the real runner + risk gate."""

    spec = load_strategy_spec(strategy_path)
    sleeve = _permissive_sleeve(spec)

    cases: list[dict[str, Any]] = []
    for case_path in sorted(parity_dir.glob("*.json")):
        cases.append(json.loads(case_path.read_text(encoding="utf-8")))

    built = [parsed for case in cases if (parsed := _event_from_case(case)) is not None]
    events = [event for event, _ in built]
    # Clock at the latest event's timestamp so attached snapshots read as fresh
    # (the parity fixtures are dated in the past; using wall-clock now would make
    # every snapshot look stale).
    clock_now = max((when for _, when in built), default=_now())

    result = SmokeResult()

    def verdict_sink(envelope: IntentEnvelope, verdict: RiskDecision) -> None:
        if not isinstance(envelope.decision, (PlaceOrder, ReplaceOrder)):
            return
        result.orders_emitted += 1
        if verdict.allowed:
            result.intents_approved += 1
        else:
            result.intents_rejected += 1
            for reason in verdict.reasons or ("unspecified",):
                result.rejection_reasons[reason] = result.rejection_reasons.get(reason, 0) + 1

    context = InMemoryContext(
        strategy_id_value=spec.strategy_id,
        sleeve_id_value=sleeve.sleeve_id,
        clock_now=clock_now,
        cash_by_ccy={
            "USD": CashBalance(
                currency="USD",
                total=Decimal("1000000"),
                available=Decimal("1000000"),
                held_for_orders=Decimal("0"),
                settling=Decimal("0"),
                updated_at=clock_now,
            )
        },
    )
    runner = StrategyRunner(
        spec=spec,
        sleeve=sleeve,
        strategy=create_from_spec(spec),
        events=_NullSource(),
        sink=_NullSink(),
        risk=SleeveRiskGate(sleeve=sleeve, daily_loss=DailyLossLedger()),
        clock=InMemoryClock(current=clock_now),
        context_provider=StaticContextProvider(ctx=context),
        verdict_sink=verdict_sink,
    )

    runner.start()
    try:
        for event in events:
            runner.process_event(event)
    finally:
        runner.stop()

    return result


def _now() -> datetime:
    return datetime.now(tz=UTC)
