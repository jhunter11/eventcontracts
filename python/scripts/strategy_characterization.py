"""Characterize every strategy config: speed, how it reads, what it emits, and
the arb-vs-conviction classification.

Read-only and offline. For each `configs/strategies/*.toml` this:

* loads + instantiates the strategy (records failures, e.g. missing model bundle);
* feeds a synthetic event battery (a YES quote, then the strategy's primary
  trigger — external signal / trade / timer — with a superset payload; bracket
  strategies are driven with their own configured tickers so they actually fire);
* times the hot-path `on_event` (quote) over many iterations -> microseconds/event;
* records which decision kinds it emits and a behavior class.

It does NOT measure statistical out-of-sample accuracy — that needs labeled
historical replay (the promotion gate), not synthetic events. What it proves is:
instantiation, read surface, decision behavior, decision math correctness for the
deterministic runtimes, and decision-path latency.

Usage:  .venv/Scripts/python.exe python/scripts/strategy_characterization.py
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from eventcontracts.config import StrategySpecConfig, load_typed_toml
from eventcontracts.domain import (
    EventId,
    InstrumentId,
    OrderBookLevel,
    OutcomeSide,
    Quote,
    Trade,
    Venue,
)
from eventcontracts.domain.events import (
    ExternalSignalEvent,
    QuoteEvent,
    TimerEvent,
    TradeEvent,
)
from eventcontracts.strategy.registry import create_from_spec, load_entry_points

NOW = datetime.now(UTC)
CONFIG_DIR = Path("configs/strategies")
ITERS = 3000

# Superset external-signal payload: covers the keys the predictive runtimes read.
SUPERSET_PAYLOAD = {
    "probability": "0.62",
    "implied_prob": "0.62",
    "yes_probability": "0.62",
    "confidence": "0.95",
    "mean": "74",
    "sigma": "3",
    "dist": "normal",
    "seat_occupancy_pct": "0.85",
    "ticket_velocity_per_hour": "1200",
    "player_1_win_probability": "0.62",
}

# Edge-type taxonomy (brainstorm addendum) keyed by family/name.
EDGE_TYPE = {
    "weather": "better distribution",
    "macro": "better distribution / liquidity provision",
    "equity": "better distribution",
    "commodity": "better distribution",
    "entertainment": "better distribution (soft crowd)",
    "sports": "market-anchored residual",
    "arbitrage": "cross-market inconsistency",
    "microstructure": "adverse-selection avoidance",
    "politics": "resolution-rule / source lag",
}


class Ctx:
    @property
    def now(self) -> datetime:
        return NOW


def _quote(market_id: str, bid: str, ask: str, side: OutcomeSide = OutcomeSide.YES) -> QuoteEvent:
    inst = InstrumentId(venue=Venue("kalshi"), market_id=market_id)
    return QuoteEvent(
        event_id=EventId("q-" + market_id),
        quote=Quote(
            instrument_id=inst,
            side=side,
            bid=OrderBookLevel(price=Decimal(bid), quantity=Decimal("100")),
            ask=OrderBookLevel(price=Decimal(ask), quantity=Decimal("100")),
            exchange_ts=None,
            received_at=NOW,
        ),
    )


def _trade(market_id: str, price: str) -> TradeEvent:
    inst = InstrumentId(venue=Venue("kalshi"), market_id=market_id)
    return TradeEvent(
        event_id=EventId("t-" + market_id),
        trade=Trade(
            instrument_id=inst,
            side=None,
            price=Decimal(price),
            quantity=Decimal("1"),
            trade_id=None,
            exchange_ts=None,
            received_at=NOW,
        ),
    )


def _signal(source: str, market_id: str) -> ExternalSignalEvent:
    payload = dict(SUPERSET_PAYLOAD)
    payload["market_id"] = market_id
    return ExternalSignalEvent(
        event_id=EventId("sig-" + source),
        source=source,
        exchange_ts=NOW,
        received_at=NOW,
        schema_version="char-v1",
        payload=payload,
    )


def _timer() -> TimerEvent:
    return TimerEvent(event_id=EventId("timer-1"), timestamp=NOW, label="char")


def _parse_bracket_tickers(brackets: str) -> list[str]:
    out = []
    for chunk in brackets.split(";"):
        chunk = chunk.strip()
        if chunk:
            out.append(chunk.split(":")[0].strip())
    return out


def _kinds(decisions) -> set[str]:
    return {type(d).__name__ for d in decisions}


def _classify(name: str, family: str, emitted: set[str], tif: str | None, tier: str) -> str:
    if name in ("kalshi_noarb_scanner", "arbitrage_cross_venue") or family == "arbitrage":
        return "arb / no-arb lock (IOC, immediate)"
    if name.startswith("microstructure"):
        return "scalp (fast, inventory-bounded)"
    if "ReplaceOrder" in emitted or name in ("macro_nfp_absorber", "liquidity_tail_risk_insurance"):
        return "maker / liquidity provision"
    if "PlaceOrder" in emitted or family in EDGE_TYPE:
        hold = "to-settlement" if tif in (None, "Gtc") else f"TIF={tif}"
        return f"conviction hold (directional, {hold})"
    return "observe / needs-specific-signal"


def _emitted_tif(decisions) -> str | None:
    for d in decisions:
        tif = getattr(d, "time_in_force", None)
        if tif is not None:
            return getattr(tif, "name", str(tif)).title()
    return None


def characterize(path: Path) -> dict:
    cfg = load_typed_toml(path, StrategySpecConfig)
    spec = cfg.to_domain()
    row: dict = {
        "strategy_id": spec.strategy_id,
        "name": spec.name,
        "reads": ",".join(spec.subscription.event_kinds),
        "tier": (spec.default_execution_priority.tier if spec.default_execution_priority else "-"),
        "family": (spec.tags or {}).get("family", "-"),
    }
    try:
        strat = create_from_spec(spec)
    except Exception as exc:  # noqa: BLE001 - characterization records the reason
        row.update(instantiated=False, note=f"{type(exc).__name__}: {exc}"[:60],
                   hot_us="-", emits="-", behavior="instantiation-failed")
        return row

    ctx = Ctx()
    params = spec.parameters or {}
    source = str(params.get("signal_source") or
                 (spec.subscription.external_sources[0] if spec.subscription.external_sources else "external"))

    # Drive bracket strategies with their own tickers; others with a generic market.
    emitted: set[str] = set()
    bracket_raw = str(params.get("brackets", ""))
    if bracket_raw:
        tickers = _parse_bracket_tickers(bracket_raw)
        for tk in tickers:
            emitted |= _kinds(strat.on_event(_quote(tk, "0.08", "0.12"), ctx))
        # ladder_cdf wants a distribution signal; noarb is quote-only (already driven).
        emitted |= _kinds(strat.on_event(_signal(source, tickers[0]), ctx))
    else:
        mkt = "KXTEST-DUMMY"
        emitted |= _kinds(strat.on_event(_quote(mkt, "0.30", "0.34"), ctx))
        kinds = spec.subscription.event_kinds
        if "external" in kinds:
            emitted |= _kinds(strat.on_event(_signal(source, mkt), ctx))
        if "trade" in kinds:
            emitted |= _kinds(strat.on_event(_trade(mkt, "0.30"), ctx))
        if "timer" in kinds:
            emitted |= _kinds(strat.on_event(_timer(), ctx))

    # Hot-path speed: time the quote on_event (the most frequent event).
    hot_quote = _quote("KXSPEED-1", "0.30", "0.34")
    # warmup
    for _ in range(200):
        strat.on_event(hot_quote, ctx)
    t0 = time.perf_counter_ns()
    for _ in range(ITERS):
        strat.on_event(hot_quote, ctx)
    elapsed = time.perf_counter_ns() - t0
    hot_us = elapsed / ITERS / 1000.0

    tif = _emitted_tif(
        strat.on_event(_signal(source, "KXTEST-DUMMY"), ctx) if "external" in spec.subscription.event_kinds else []
    )
    row.update(
        instantiated=True,
        hot_us=round(hot_us, 2),
        emits=",".join(sorted(emitted)) or "NoAction",
        behavior=_classify(spec.name, row["family"], emitted, tif, row["tier"]),
        note="",
    )
    return row


def main() -> None:
    load_entry_points()
    rows = []
    for path in sorted(CONFIG_DIR.glob("*.toml")):
        try:
            rows.append(characterize(path))
        except Exception as exc:  # noqa: BLE001
            rows.append({"strategy_id": path.stem, "name": "?", "reads": "-", "tier": "-",
                         "family": "-", "instantiated": False, "hot_us": "-", "emits": "-",
                         "behavior": "config-error", "note": f"{type(exc).__name__}: {exc}"[:60]})

    hdr = ("strategy_id", "name", "reads", "tier", "hot_us", "emits", "behavior", "note")
    widths = {h: max(len(h), *(len(str(r.get(h, ""))) for r in rows)) for h in hdr}
    line = "  ".join(h.ljust(widths[h]) for h in hdr)
    print(line)
    print("-" * len(line))
    for r in rows:
        print("  ".join(str(r.get(h, "")).ljust(widths[h]) for h in hdr))

    ok = sum(1 for r in rows if r.get("instantiated"))
    emit = sum(1 for r in rows if r.get("emits") not in ("NoAction", "-", ""))
    hot = [r["hot_us"] for r in rows if isinstance(r.get("hot_us"), (int, float))]
    print(f"\nstrategies={len(rows)} instantiated={ok} emit_on_battery={emit}")
    if hot:
        print(f"hot-path on_event: min={min(hot)}us median={sorted(hot)[len(hot)//2]}us max={max(hot)}us "
              f"(network RTT dominates by ~10000x — see latency playbook)")


if __name__ == "__main__":
    main()
