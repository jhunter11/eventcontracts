from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from eventcontracts.research.btc_lead import (
    CryptoTick,
    RollingRealizedVol,
    SyntheticIndexState,
    evaluate_btc15m_timing_candidate,
    measure_reference_lead,
    parse_cfb_rti_stats,
    parse_coinbase_ticker,
    parse_kraken_ticker,
)

NOW = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)


def test_parse_coinbase_and_kraken_public_tickers() -> None:
    coinbase = parse_coinbase_ticker(
        {"product_id": "BTC-USD", "price": "100000.50", "time": "2026-06-02T11:59:59.900Z", "trade_id": 7},
        received_at=NOW,
    )
    kraken = parse_kraken_ticker({"result": {"XXBTZUSD": {"c": ["100010.10", "0.1"]}}}, received_at=NOW)

    assert coinbase.venue == "coinbase"
    assert coinbase.price == pytest.approx(100000.50)
    assert coinbase.tick_age_ms == pytest.approx(100.0)
    assert kraken.venue == "kraken"
    assert kraken.exchange_ts is None


def test_parse_cfb_rti_stats_timestamped_reference_print() -> None:
    received_at = datetime(2026, 6, 2, 12, 0, 1, tzinfo=UTC)
    cfb_time_ms = int(NOW.timestamp() * 1000) + 250

    tick = parse_cfb_rti_stats(
        {"type": "rti_stats", "id": "BRTI", "value": "100151.25", "time": cfb_time_ms},
        received_at=received_at,
    )

    assert tick.venue == "cfb-rti"
    assert tick.symbol == "BRTI"
    assert tick.price == pytest.approx(100_151.25)
    assert tick.exchange_ts == datetime(2026, 6, 2, 12, 0, 0, 250000, tzinfo=UTC)
    assert tick.received_at == received_at


def test_synthetic_index_weights_reachable_fresh_components_only() -> None:
    state = SyntheticIndexState({"coinbase": 0.5, "kraken": 0.5})
    state.update(
        CryptoTick(
            venue="coinbase",
            symbol="BTC-USD",
            price=100_000.0,
            exchange_ts=NOW - timedelta(milliseconds=100),
            received_at=NOW - timedelta(milliseconds=90),
        )
    )
    state.update(
        CryptoTick(
            venue="kraken",
            symbol="XBT/USD",
            price=101_000.0,
            exchange_ts=NOW - timedelta(milliseconds=1_500),
            received_at=NOW - timedelta(milliseconds=1_490),
        )
    )

    snapshot = state.snapshot(NOW, max_component_age_ms=500, min_venues=1)

    assert snapshot is not None
    assert snapshot.price == pytest.approx(100_000.0)
    assert snapshot.venue_count == 1
    assert snapshot.max_component_age_ms == pytest.approx(100.0)
    assert state.snapshot(NOW, max_component_age_ms=500, min_venues=2) is None


def test_reference_lead_requires_reference_exchange_timestamp() -> None:
    state = SyntheticIndexState({"coinbase": 1.0})
    state.update(
        CryptoTick(
            venue="coinbase",
            symbol="BTC-USD",
            price=100_000.0,
            exchange_ts=NOW - timedelta(milliseconds=10),
            received_at=NOW,
        )
    )
    synthetic = state.snapshot(NOW)
    assert synthetic is not None

    missing_reference = CryptoTick(venue="cfb-rti", symbol="BTC-USD", price=100_001.0, received_at=NOW)
    measured_reference = CryptoTick(
        venue="cfb-rti",
        symbol="BTC-USD",
        price=100_001.0,
        exchange_ts=NOW + timedelta(milliseconds=12),
        received_at=NOW + timedelta(milliseconds=14),
    )

    assert measure_reference_lead(synthetic, missing_reference).lead_ms is None
    assert measure_reference_lead(synthetic, measured_reference).lead_ms == pytest.approx(12.0)


def test_timing_candidate_requires_positive_lead_after_latency_and_costs() -> None:
    synthetic = SyntheticIndexState({"coinbase": 1.0})
    synthetic.update(CryptoTick(venue="coinbase", symbol="BTC-USD", price=100_150.0, received_at=NOW))
    snapshot = synthetic.snapshot(NOW)
    assert snapshot is not None

    blocked = evaluate_btc15m_timing_candidate(
        market_id="KXBTC15M-DEMO",
        synthetic=snapshot,
        strike=100_000.0,
        seconds_to_expiry=30.0,
        sigma_per_sec=5.0,
        yes_bid=Decimal("0.52"),
        yes_ask=Decimal("0.55"),
        measured_lead_ms=1.5,
        residual_latency_ms=2.0,
    )
    candidate = evaluate_btc15m_timing_candidate(
        market_id="KXBTC15M-DEMO",
        synthetic=snapshot,
        strike=100_000.0,
        seconds_to_expiry=30.0,
        sigma_per_sec=5.0,
        yes_bid=Decimal("0.52"),
        yes_ask=Decimal("0.55"),
        measured_lead_ms=4.0,
        residual_latency_ms=2.0,
    )

    assert not blocked.candidate
    assert blocked.reason == "lead_missing_or_below_residual_latency"
    assert candidate.candidate
    assert candidate.side == "YES"
    assert candidate.net_edge is not None and candidate.net_edge > 0.02


def test_rolling_realized_vol_needs_multiple_monotonic_points() -> None:
    vol = RollingRealizedVol(max_points=5)
    for i, price in enumerate((100_000.0, 100_020.0, 100_000.0, 100_030.0)):
        vol.update(CryptoTick(venue="coinbase", symbol="BTC-USD", price=price, received_at=NOW + timedelta(seconds=i)))

    sigma = vol.sigma_per_second()
    assert sigma is not None
    assert sigma > 0
