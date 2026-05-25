"""Smoke coverage for the strategy specs described in docs/strategy-specs.md."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from eventcontracts.config import load_sleeve_spec, load_strategy_spec
from eventcontracts.domain import (
    CancelOrder,
    ClientOrderId,
    CorrelationId,
    EventId,
    ExternalSignalEvent,
    InstrumentId,
    LifecycleEvent,
    MarketLifecycleEvent,
    MarketLifecycleKind,
    NoAction,
    NormalizedEvent,
    Order,
    OrderBook,
    OrderBookEvent,
    OrderBookLevel,
    OrderSide,
    OrderStatus,
    OrderType,
    OutcomeSide,
    OwnOrderUpdateEvent,
    PlaceOrder,
    Quote,
    QuoteEvent,
    ReplaceOrder,
    SettlementEvent,
    SettlementResolvedEvent,
    SleeveId,
    StrategyDecision,
    StrategyId,
    TimeInForce,
    TimerEvent,
    Trade,
    TradeEvent,
    Venue,
)
from eventcontracts.strategy import create_from_spec
from eventcontracts.strategy.base import Strategy
from eventcontracts.testing.doubles import InMemoryContext
from tests.conftest import REPO_ROOT

NOW = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)
CONFIGS = REPO_ROOT / "configs"

STRATEGY_CONFIGS: tuple[str, ...] = (
    "weather-temperature-arbitrage.toml",
    "microstructure-obi-scalper.toml",
    "macro-nfp-absorber.toml",
    "arbitrage-cross-venue.toml",
    "macro-fed-gnn.toml",
    "aerospace-launch-delay.toml",
    "entertainment-box-office.toml",
    "politics-primary-momentum.toml",
    "politics-legislative-cascade.toml",
    "microstructure-queue-evader.toml",
    "macro-cpi-predictor.toml",
    "sports-player-cut-lgbm.toml",
    "sports-cut-line-shifter.toml",
    "sports-frl-weather-arb.toml",
    "sports-hole-by-hole-pin.toml",
    "crypto-bracket-parity-arb.toml",
    "crypto-vol-surface-mispricer.toml",
    "crypto-terminal-drift-tracker.toml",
    "crypto-realized-vol-regime.toml",
    "crypto-cross-strike-skew-arb.toml",
)

SLEEVE_CONFIGS: tuple[str, ...] = (
    "weather-kalshi-paper-a.toml",
    "microstructure-obi-paper-a.toml",
    "macro-nfp-kalshi-paper-a.toml",
    "arbitrage-cross-venue-kalshi-paper-a.toml",
    "macro-fed-kalshi-paper-a.toml",
    "aerospace-launch-kalshi-paper-a.toml",
    "entertainment-box-office-kalshi-paper-a.toml",
    "politics-primary-kalshi-paper-a.toml",
    "politics-legislative-kalshi-paper-a.toml",
    "microstructure-queue-kalshi-paper-a.toml",
    "macro-cpi-kalshi-paper-a.toml",
    "sports-kalshi-paper-a.toml",
    "sports-cut-line-kalshi-paper-a.toml",
    "sports-polymarket-paper-a.toml",
    "sports-hole-by-hole-polymarket-paper-a.toml",
    "crypto-bracket-parity-kalshi-paper-a.toml",
    "crypto-vol-surface-kalshi-paper-a.toml",
    "crypto-terminal-drift-kalshi-paper-a.toml",
    "crypto-realized-vol-kalshi-paper-a.toml",
    "crypto-skew-arb-kalshi-paper-a.toml",
)


def test_all_strategy_configs_load_and_create_registered_strategies() -> None:
    for config_name in STRATEGY_CONFIGS:
        spec = load_strategy_spec(CONFIGS / "strategies" / config_name)

        strategy = create_from_spec(spec)

        assert strategy.spec == spec


def test_all_sleeve_configs_load_and_reference_strategy_configs() -> None:
    strategy_ids = {
        load_strategy_spec(CONFIGS / "strategies" / config_name).strategy_id
        for config_name in STRATEGY_CONFIGS
    }

    for config_name in SLEEVE_CONFIGS:
        sleeve = load_sleeve_spec(CONFIGS / "sleeves" / config_name)

        assert sleeve.strategy_id in strategy_ids
        assert sleeve.currency == "USD"
        assert sleeve.risk.currency == "USD"


@pytest.mark.parametrize(
    ("config_name", "scenario"),
    (
        ("weather-temperature-arbitrage.toml", lambda strategy, ctx: _weather(strategy, ctx)),
        ("microstructure-obi-scalper.toml", lambda strategy, ctx: _obi(strategy, ctx)),
        ("macro-nfp-absorber.toml", lambda strategy, ctx: _nfp(strategy, ctx)),
        ("arbitrage-cross-venue.toml", lambda strategy, ctx: _cross_venue(strategy, ctx)),
        ("macro-fed-gnn.toml", lambda strategy, ctx: _fed(strategy, ctx)),
        ("aerospace-launch-delay.toml", lambda strategy, ctx: _launch(strategy, ctx)),
        ("entertainment-box-office.toml", lambda strategy, ctx: _box_office(strategy, ctx)),
        ("politics-primary-momentum.toml", lambda strategy, ctx: _primary(strategy, ctx)),
        ("politics-legislative-cascade.toml", lambda strategy, ctx: _legislative(strategy, ctx)),
        ("microstructure-queue-evader.toml", lambda strategy, ctx: _queue_evader(strategy, ctx)),
        ("macro-cpi-predictor.toml", lambda strategy, ctx: _cpi(strategy, ctx)),
    ),
)
def test_strategy_specs_emit_smoke_decisions(
    config_name: str,
    scenario: Callable[[Strategy, InMemoryContext], Sequence[StrategyDecision]],
) -> None:
    spec = load_strategy_spec(CONFIGS / "strategies" / config_name)
    strategy = create_from_spec(spec)
    ctx = InMemoryContext(
        strategy_id_value=spec.strategy_id,
        sleeve_id_value=SleeveId("test-sleeve"),
        clock_now=NOW,
    )

    decisions = scenario(strategy, ctx)

    assert decisions
    assert not all(isinstance(decision, NoAction) for decision in decisions)


def _weather(strategy: Strategy, ctx: InMemoryContext) -> Sequence[StrategyDecision]:
    instrument = _instrument("KXHIGHNY")
    _dispatch(
        strategy,
        _quote_event(instrument, bid="0.40", ask="0.42"),
        ctx,
    )
    return _dispatch(
        strategy,
        ExternalSignalEvent(
            event_id=EventId("weather-signal"),
            source="open-meteo",
            exchange_ts=NOW,
            received_at=NOW,
            schema_version="weather-temperature-v1",
            payload={
                "implied_prob": "0.60",
                "instrument_id": {
                    "venue": "kalshi",
                    "market_id": "KXHIGHNY",
                },
            },
        ),
        ctx,
    )


def _obi(strategy: Strategy, ctx: InMemoryContext) -> Sequence[StrategyDecision]:
    return _dispatch(
        strategy,
        _book_event("obi-book", _instrument("KXOBI"), bid="0.500", ask="0.505", bid_qty="90", ask_qty="10"),
        ctx,
    )


def _nfp(strategy: Strategy, ctx: InMemoryContext) -> Sequence[StrategyDecision]:
    decisions = _dispatch(
        strategy,
        TimerEvent(event_id=EventId("nfp-widen"), timestamp=NOW, label="nfp_widen"),
        ctx,
    )
    assert all(isinstance(decision, ReplaceOrder) for decision in decisions)
    return decisions


def _cross_venue(strategy: Strategy, ctx: InMemoryContext) -> Sequence[StrategyDecision]:
    market_id = "KXARB"
    _dispatch(
        strategy,
        _quote_event(
            InstrumentId(venue=Venue.POLYMARKET_GLOBAL, market_id=market_id),
            bid="0.80",
            ask="0.82",
            event_id="poly-quote",
        ),
        ctx,
    )
    return _dispatch(
        strategy,
        _quote_event(_instrument(market_id), bid="0.38", ask="0.40", event_id="kalshi-quote"),
        ctx,
    )


def _fed(strategy: Strategy, ctx: InMemoryContext) -> Sequence[StrategyDecision]:
    _dispatch(
        strategy,
        _quote_event(_instrument("KXFED-BACK"), bid="0.50", ask="0.52", event_id="fed-back-quote"),
        ctx,
    )
    _dispatch(
        strategy,
        _trade_event(_instrument("KXFED-FRONT"), price="0.50", quantity="10", event_id="fed-anchor"),
        ctx,
    )
    return _dispatch(
        strategy,
        _trade_event(_instrument("KXFED-FRONT"), price="0.55", quantity="10", event_id="fed-move"),
        ctx,
    )


def _launch(strategy: Strategy, ctx: InMemoryContext) -> Sequence[StrategyDecision]:
    _dispatch(
        strategy,
        ExternalSignalEvent(
            event_id=EventId("launch-signal"),
            source="space-devs",
            exchange_ts=NOW,
            received_at=NOW,
            schema_version="launch-state-v1",
            payload={
                "launch_market_id": "KXLAUNCH-DELAY",
                "time_to_window_close_mins": "30",
                "fueling_confirmed": False,
            },
        ),
        ctx,
    )
    _dispatch(
        strategy,
        LifecycleEvent(
            event_id=EventId("launch-open"),
            lifecycle=MarketLifecycleEvent(
                instrument_id=_instrument("KXLAUNCH-DELAY"),
                kind=MarketLifecycleKind.OPENED,
                exchange_ts=NOW,
                received_at=NOW,
            ),
        ),
        ctx,
    )
    return _dispatch(
        strategy,
        TimerEvent(event_id=EventId("launch-check"), timestamp=NOW, label="launch_check"),
        ctx,
    )


def _box_office(strategy: Strategy, ctx: InMemoryContext) -> Sequence[StrategyDecision]:
    _dispatch(
        strategy,
        _quote_event(_instrument("KXBOXOFFICE-WEEKEND"), bid="0.20", ask="0.22", event_id="box-quote"),
        ctx,
    )
    _dispatch(
        strategy,
        ExternalSignalEvent(
            event_id=EventId("box-signal"),
            source="apify-fandango",
            exchange_ts=NOW,
            received_at=NOW,
            schema_version="box-office-v1",
            payload={
                "seat_occupancy_pct": "1.0",
                "ticket_velocity_per_hour": "1000000",
                "confidence": "0.90",
            },
        ),
        ctx,
    )
    return _dispatch(
        strategy,
        TimerEvent(event_id=EventId("box-timer"), timestamp=NOW, label="friday_8pm_et"),
        ctx,
    )


def _primary(strategy: Strategy, ctx: InMemoryContext) -> Sequence[StrategyDecision]:
    _dispatch(
        strategy,
        _quote_event(_instrument("KXPRIMARY-NH"), bid="0.40", ask="0.42", event_id="primary-b-quote"),
        ctx,
    )
    return _dispatch(
        strategy,
        SettlementResolvedEvent(
            event_id=EventId("primary-a-settled"),
            settlement=SettlementEvent(
                instrument_id=_instrument("KXPRIMARY-IA"),
                resolved_side=OutcomeSide.YES,
                payout_per_contract=Decimal("1"),
                currency="USD",
                settled_at=NOW,
                source="kalshi",
            ),
        ),
        ctx,
    )


def _legislative(strategy: Strategy, ctx: InMemoryContext) -> Sequence[StrategyDecision]:
    decisions = _dispatch(
        strategy,
        ExternalSignalEvent(
            event_id=EventId("leg-signal"),
            source="gdelt-roberta",
            exchange_ts=NOW,
            received_at=NOW,
            schema_version="legislative-sentiment-v1",
            payload={
                "bill_market_id": "KXBILL-PASSAGE",
                "senator_id": "swing-1",
                "sentiment_score": "-2.0",
                "whip_leverage_factor": "1.0",
            },
        ),
        ctx,
    )
    assert any(isinstance(decision, CancelOrder) for decision in decisions)
    assert any(isinstance(decision, PlaceOrder) for decision in decisions)
    return decisions


def _queue_evader(strategy: Strategy, ctx: InMemoryContext) -> Sequence[StrategyDecision]:
    instrument = _instrument("KXQUEUE")
    _dispatch(
        strategy,
        OwnOrderUpdateEvent(
            event_id=EventId("queue-own-order"),
            order=Order(
                client_order_id=ClientOrderId("queue-order"),
                venue_order_id=None,
                instrument_id=instrument,
                outcome_side=OutcomeSide.YES,
                order_side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                time_in_force=TimeInForce.GTC,
                price=Decimal("0.50"),
                quantity=Decimal("10"),
                filled_quantity=Decimal("0"),
                status=OrderStatus.OPEN,
                created_at=NOW,
                updated_at=NOW,
                correlation_id=CorrelationId("queue-correlation"),
                strategy_id=StrategyId("microstructure-queue_evader-v1"),
                sleeve_id=SleeveId("test-sleeve"),
            ),
        ),
        ctx,
    )
    _dispatch(
        strategy,
        _book_event("queue-book", instrument, bid="0.50", ask="0.51", bid_qty="3", ask_qty="10"),
        ctx,
    )
    return _dispatch(
        strategy,
        _trade_event(instrument, price="0.50", quantity="50", event_id="queue-trade"),
        ctx,
    )


def _cpi(strategy: Strategy, ctx: InMemoryContext) -> Sequence[StrategyDecision]:
    _dispatch(
        strategy,
        _quote_event(_instrument("KXCPI-HIGH"), bid="0.30", ask="0.32", event_id="cpi-high-quote"),
        ctx,
    )
    _dispatch(
        strategy,
        ExternalSignalEvent(
            event_id=EventId("cpi-signal"),
            source="truflation",
            exchange_ts=NOW,
            received_at=NOW,
            schema_version="cpi-nowcast-v1",
            payload={
                "alt_data_mom_inflation": "0.012",
                "cleveland_fed_nowcast": "0.012",
                "kalshi_implied_mean": "0.002",
            },
        ),
        ctx,
    )
    return _dispatch(
        strategy,
        TimerEvent(event_id=EventId("cpi-timer"), timestamp=NOW, label="cpi_recompute"),
        ctx,
    )


_GolfScenarioFn = Callable[[Strategy, InMemoryContext], Sequence[StrategyDecision]]
_GolfScenario = tuple[str, dict[str, str], _GolfScenarioFn]


GOLF_SCENARIOS: tuple[_GolfScenario, ...] = (
    (
        "sports-player-cut-lgbm.toml",
        {"player_market_map": "scheffler:KX-PGA-SCHEFFLER-CUT"},
        lambda strategy, ctx: _player_cut(strategy, ctx),
    ),
    (
        "sports-cut-line-shifter.toml",
        {
            "bracket_market_map": (
                "-2:KX-PGA-CUT-NEG2;-1:KX-PGA-CUT-NEG1;0:KX-PGA-CUT-ZERO"
            ),
        },
        lambda strategy, ctx: _cut_line(strategy, ctx),
    ),
    (
        "sports-frl-weather-arb.toml",
        {"player_market_map": "scheffler:POLY-FRL-SCHEFFLER"},
        lambda strategy, ctx: _frl_weather(strategy, ctx),
    ),
    (
        "sports-hole-by-hole-pin.toml",
        {},
        lambda strategy, ctx: _hole_by_hole(strategy, ctx),
    ),
)


CRYPTO_SCENARIOS: tuple[_GolfScenario, ...] = (
    (
        "crypto-bracket-parity-arb.toml",
        {
            "bracket_market_ids": (
                "BTCD-LO:-inf:100000;BTCD-MID:100000:105000;BTCD-HI:105000:inf"
            ),
            # Mids that sum to 1.05 → triggers a sell-all action.
            "min_parity_edge": "0.03",
            "max_spread_bps": "1000",
        },
        lambda strategy, ctx: _crypto_parity(strategy, ctx),
    ),
    (
        "crypto-vol-surface-mispricer.toml",
        {
            "strike_market_map": "BTCD-K100K:100000",
            "twap_window_seconds": "60",
            "min_edge_bps": "100",
        },
        lambda strategy, ctx: _crypto_vol_surface(strategy, ctx),
    ),
    (
        "crypto-terminal-drift-tracker.toml",
        {
            "strike_market_map": "BTCD-K100K:100000",
            "terminal_window_seconds": "120",
            "min_realized_samples": "5",
            "min_terminal_edge": "0.05",
        },
        lambda strategy, ctx: _crypto_terminal(strategy, ctx),
    ),
    (
        "crypto-realized-vol-regime.toml",
        {
            "tail_market_map": "BTCD-K105K:105000;BTCD-K95K:95000",
            "atm_market_id": "BTCD-K100K",
            "atm_strike": "100000",
            "rv_window_samples": "5",
            "min_vol_edge": "0.05",
        },
        lambda strategy, ctx: _crypto_realized_vol(strategy, ctx),
    ),
    (
        "crypto-cross-strike-skew-arb.toml",
        {
            "strike_market_map": "BTCD-K100K:100000;BTCD-K101K:101000",
            "min_skew_edge": "0.01",
        },
        lambda strategy, ctx: _crypto_skew(strategy, ctx),
    ),
)


@pytest.mark.parametrize(
    ("config_name", "param_overrides", "scenario"),
    CRYPTO_SCENARIOS,
    ids=[config_name for config_name, _, _ in CRYPTO_SCENARIOS],
)
def test_crypto_strategy_specs_emit_smoke_decisions(
    config_name: str,
    param_overrides: dict[str, str],
    scenario: Callable[[Strategy, InMemoryContext], Sequence[StrategyDecision]],
) -> None:
    """Crypto strategies ship with empty operator-supplied roster strings;
    each scenario injects a minimal roster + tightened thresholds so the
    smoke path actually fires a decision."""

    import dataclasses

    spec = load_strategy_spec(CONFIGS / "strategies" / config_name)
    merged = dict(spec.parameters)
    merged.update(param_overrides)
    spec = dataclasses.replace(spec, parameters=merged)
    strategy = create_from_spec(spec)
    ctx = InMemoryContext(
        strategy_id_value=spec.strategy_id,
        sleeve_id_value=SleeveId("test-sleeve"),
        clock_now=NOW,
    )

    decisions = scenario(strategy, ctx)

    assert decisions
    assert not all(isinstance(decision, NoAction) for decision in decisions)


@pytest.mark.parametrize(
    ("config_name", "param_overrides", "scenario"),
    GOLF_SCENARIOS,
    ids=[config_name for config_name, _, _ in GOLF_SCENARIOS],
)
def test_golf_strategy_specs_emit_smoke_decisions(
    config_name: str,
    param_overrides: dict[str, str],
    scenario: Callable[[Strategy, InMemoryContext], Sequence[StrategyDecision]],
) -> None:
    """Golf strategies ship with empty operator-supplied maps; we inject
    a minimal roster here so the smoke scenario can fire."""

    import dataclasses

    spec = load_strategy_spec(CONFIGS / "strategies" / config_name)
    if param_overrides:
        merged = dict(spec.parameters)
        merged.update(param_overrides)
        spec = dataclasses.replace(spec, parameters=merged)
    strategy = create_from_spec(spec)
    ctx = InMemoryContext(
        strategy_id_value=spec.strategy_id,
        sleeve_id_value=SleeveId("test-sleeve"),
        clock_now=NOW,
    )

    decisions = scenario(strategy, ctx)

    assert decisions
    assert not all(isinstance(decision, NoAction) for decision in decisions)


def _player_cut(strategy: Strategy, ctx: InMemoryContext) -> Sequence[StrategyDecision]:
    market_id = "KX-PGA-SCHEFFLER-CUT"
    _dispatch(strategy, _quote_event(_instrument(market_id), bid="0.40", ask="0.42"), ctx)
    _dispatch(
        strategy,
        ExternalSignalEvent(
            event_id=EventId("cut-signal"),
            source="datagolf",
            exchange_ts=NOW,
            received_at=NOW,
            schema_version="datagolf-sg-v1",
            payload={
                "player_id": "scheffler",
                "strokes_to_cut": "-1",
                "sg_approach": "2.0",
                "sg_putting": "-1.5",
                "wave_weather_delta": "0.1",
            },
        ),
        ctx,
    )
    return _dispatch(
        strategy,
        TimerEvent(
            event_id=EventId("cut-timer"),
            timestamp=NOW,
            label="player_round_complete",
        ),
        ctx,
    )


def _cut_line(strategy: Strategy, ctx: InMemoryContext) -> Sequence[StrategyDecision]:
    for market_id, bid, ask in (
        ("KX-PGA-CUT-NEG2", "0.05", "0.07"),
        ("KX-PGA-CUT-NEG1", "0.10", "0.12"),
        ("KX-PGA-CUT-ZERO", "0.15", "0.17"),
    ):
        _dispatch(
            strategy,
            _quote_event(_instrument(market_id), bid=bid, ask=ask, event_id=f"q-{market_id}"),
            ctx,
        )
    _dispatch(
        strategy,
        ExternalSignalEvent(
            event_id=EventId("cut-line-signal"),
            source="pga-tour",
            exchange_ts=NOW,
            received_at=NOW,
            schema_version="pga-aggregate-v1",
            payload={
                "field_scoring_avg_delta_vs_par": "1.0",
                "afternoon_wind_forecast_mph": "18",
                "top_65_current_score": "-2",
            },
        ),
        ctx,
    )
    return _dispatch(
        strategy,
        TimerEvent(
            event_id=EventId("cut-line-timer"),
            timestamp=NOW,
            label="cut_line_recompute",
        ),
        ctx,
    )


def _frl_weather(strategy: Strategy, ctx: InMemoryContext) -> Sequence[StrategyDecision]:
    market_id = "POLY-FRL-SCHEFFLER"
    _dispatch(
        strategy,
        QuoteEvent(
            event_id=EventId("frl-quote"),
            quote=Quote(
                instrument_id=InstrumentId(venue=Venue.POLYMARKET_GLOBAL, market_id=market_id),
                side=OutcomeSide.YES,
                bid=OrderBookLevel(price=Decimal("0.005"), quantity=Decimal("100")),
                ask=OrderBookLevel(price=Decimal("0.007"), quantity=Decimal("100")),
                exchange_ts=NOW,
                received_at=NOW,
            ),
        ),
        ctx,
    )
    return _dispatch(
        strategy,
        ExternalSignalEvent(
            event_id=EventId("frl-signal"),
            source="weather_tee_combined",
            exchange_ts=NOW,
            received_at=NOW,
            schema_version="frl-wave-v1",
            payload={
                "am_wave_wind_mph": "5",
                "pm_wave_wind_mph": "25",
                "players": [
                    {
                        "player_id": "scheffler",
                        "market_id": market_id,
                        "wave": "am",
                        "wind_sg_baseline": "1.2",
                    }
                ],
            },
        ),
        ctx,
    )


def _hole_by_hole(strategy: Strategy, ctx: InMemoryContext) -> Sequence[StrategyDecision]:
    market_id = "POLY-BIRDIE-HOLE7-MORIKAWA"
    _dispatch(
        strategy,
        QuoteEvent(
            event_id=EventId("hbh-quote"),
            quote=Quote(
                instrument_id=InstrumentId(venue=Venue.POLYMARKET_GLOBAL, market_id=market_id),
                side=OutcomeSide.YES,
                bid=OrderBookLevel(price=Decimal("0.10"), quantity=Decimal("100")),
                ask=OrderBookLevel(price=Decimal("0.12"), quantity=Decimal("100")),
                exchange_ts=NOW,
                received_at=NOW,
            ),
        ),
        ctx,
    )
    return _dispatch(
        strategy,
        ExternalSignalEvent(
            event_id=EventId("shotlink-drive"),
            source="shotlink",
            exchange_ts=NOW,
            received_at=NOW,
            schema_version="shotlink-shot-v1",
            payload={
                "player_id": "morikawa",
                "market_id": market_id,
                "hole_event_token": "shot-12345",
                "distance_to_pin_yards": 110,
                "pin_difficulty_index": 0.35,
                "player_sg_approach_from_distance": 1.5,
                "lie": "fairway",
            },
        ),
        ctx,
    )


def _crypto_parity(strategy: Strategy, ctx: InMemoryContext) -> Sequence[StrategyDecision]:
    """Quotes summing to 1.05 should force a sell-all (buy NO on each bracket)."""

    _dispatch(strategy, _quote_event(_instrument("BTCD-LO"), bid="0.34", ask="0.36", event_id="lo"), ctx)
    _dispatch(strategy, _quote_event(_instrument("BTCD-MID"), bid="0.34", ask="0.36", event_id="mid"), ctx)
    # Final quote brings sum mid to 0.35 + 0.35 + 0.35 = 1.05 → +0.05 parity dev.
    return _dispatch(
        strategy,
        _quote_event(_instrument("BTCD-HI"), bid="0.34", ask="0.36", event_id="hi"),
        ctx,
    )


def _crypto_vol_surface(strategy: Strategy, ctx: InMemoryContext) -> Sequence[StrategyDecision]:
    """Deribit IV of 60% on a 15-min ATM strike implies ~50% — well above a 30% Kalshi mid."""

    from datetime import timedelta

    expiry = NOW + timedelta(minutes=15)
    _dispatch(
        strategy,
        ExternalSignalEvent(
            event_id=EventId("crypto-spot"),
            source="binance",
            exchange_ts=NOW,
            received_at=NOW,
            schema_version="binance-spot-v1",
            payload={"last_price": "100000"},
        ),
        ctx,
    )
    _dispatch(
        strategy,
        ExternalSignalEvent(
            event_id=EventId("crypto-vol"),
            source="deribit",
            exchange_ts=NOW,
            received_at=NOW,
            schema_version="deribit-iv-v1",
            payload={
                "atm_iv": "0.60",
                "expiry_iso": expiry.isoformat(),
            },
        ),
        ctx,
    )
    return _dispatch(
        strategy,
        _quote_event(_instrument("BTCD-K100K"), bid="0.28", ask="0.32", event_id="bs-quote"),
        ctx,
    )


def _crypto_terminal(strategy: Strategy, ctx: InMemoryContext) -> Sequence[StrategyDecision]:
    """Inside the terminal window with spot well above strike → fire YES."""

    from datetime import timedelta

    expiry = NOW + timedelta(seconds=60)
    # Feed 8 spot ticks well above strike to build a realized-vol history.
    for i, price in enumerate(("101000", "101010", "101020", "101030", "101015", "101020", "101025", "101030")):
        _dispatch(
            strategy,
            ExternalSignalEvent(
                event_id=EventId(f"crypto-spot-{i}"),
                source="binance",
                exchange_ts=NOW,
                received_at=NOW,
                schema_version="binance-spot-v1",
                payload={
                    "last_price": price,
                    "expiry_iso": expiry.isoformat(),
                },
            ),
            ctx,
        )
    # Kalshi mid at 0.50 even though spot is already $1k above strike.
    _dispatch(
        strategy,
        _quote_event(_instrument("BTCD-K100K"), bid="0.48", ask="0.52", event_id="terminal-quote"),
        ctx,
    )
    return _dispatch(
        strategy,
        TimerEvent(event_id=EventId("crypto-terminal-timer"), timestamp=NOW, label="crypto_terminal_check"),
        ctx,
    )


def _crypto_realized_vol(strategy: Strategy, ctx: InMemoryContext) -> Sequence[StrategyDecision]:
    """Spot history with a large jump → high realized vol vs a calm Kalshi ATM mid."""

    from datetime import timedelta

    expiry = NOW + timedelta(minutes=15)
    # Five ticks with a big jump → realized vol spikes well above any Kalshi-implied vol.
    for i, price in enumerate(("100000", "100500", "99500", "100400", "99600")):
        _dispatch(
            strategy,
            ExternalSignalEvent(
                event_id=EventId(f"vol-spot-{i}"),
                source="binance",
                exchange_ts=NOW,
                received_at=NOW,
                schema_version="binance-spot-v1",
                payload={
                    "last_price": price,
                    "expiry_iso": expiry.isoformat(),
                },
            ),
            ctx,
        )
    # ATM bracket is calm (mid ~0.50), tail brackets are quoted.
    _dispatch(strategy, _quote_event(_instrument("BTCD-K100K"), bid="0.49", ask="0.51", event_id="atm-q"), ctx)
    _dispatch(strategy, _quote_event(_instrument("BTCD-K95K"), bid="0.94", ask="0.96", event_id="lo-q"), ctx)
    return _dispatch(
        strategy,
        _quote_event(_instrument("BTCD-K105K"), bid="0.04", ask="0.06", event_id="hi-q"),
        ctx,
    )


def _crypto_skew(strategy: Strategy, ctx: InMemoryContext) -> Sequence[StrategyDecision]:
    """Higher strike (K101K) quoted higher than lower strike (K100K) → butterfly violation."""

    _dispatch(strategy, _quote_event(_instrument("BTCD-K100K"), bid="0.40", ask="0.42", event_id="lo-strike"), ctx)
    return _dispatch(
        strategy,
        _quote_event(_instrument("BTCD-K101K"), bid="0.50", ask="0.52", event_id="hi-strike"),
        ctx,
    )


def _dispatch(strategy: Strategy, event: NormalizedEvent, ctx: InMemoryContext) -> Sequence[StrategyDecision]:
    return strategy.on_event(event, ctx)


def _instrument(market_id: str) -> InstrumentId:
    return InstrumentId(venue=Venue.KALSHI, market_id=market_id)


def _quote_event(
    instrument: InstrumentId,
    *,
    bid: str,
    ask: str,
    event_id: str = "quote",
) -> QuoteEvent:
    return QuoteEvent(
        event_id=EventId(event_id),
        quote=Quote(
            instrument_id=instrument,
            side=OutcomeSide.YES,
            bid=OrderBookLevel(price=Decimal(bid), quantity=Decimal("100")),
            ask=OrderBookLevel(price=Decimal(ask), quantity=Decimal("100")),
            exchange_ts=NOW,
            received_at=NOW,
        ),
    )


def _book_event(
    event_id: str,
    instrument: InstrumentId,
    *,
    bid: str,
    ask: str,
    bid_qty: str,
    ask_qty: str,
) -> OrderBookEvent:
    return OrderBookEvent(
        event_id=EventId(event_id),
        book=OrderBook(
            instrument_id=instrument,
            yes_bids=(OrderBookLevel(price=Decimal(bid), quantity=Decimal(bid_qty)),),
            yes_asks=(OrderBookLevel(price=Decimal(ask), quantity=Decimal(ask_qty)),),
            no_bids=(),
            no_asks=(),
            exchange_ts=NOW,
            received_at=NOW,
        ),
    )


def _trade_event(
    instrument: InstrumentId,
    *,
    price: str,
    quantity: str,
    event_id: str,
) -> TradeEvent:
    return TradeEvent(
        event_id=EventId(event_id),
        trade=Trade(
            instrument_id=instrument,
            side=OutcomeSide.YES,
            price=Decimal(price),
            quantity=Decimal(quantity),
            trade_id=event_id,
            exchange_ts=NOW,
            received_at=NOW,
        ),
    )
