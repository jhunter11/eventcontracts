"""Smoke tests for `eventcontracts.research` helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from eventcontracts.domain.events import EventProvenance, QuoteEvent, TradeEvent
from eventcontracts.domain.ids import EventId
from eventcontracts.domain.models import (
    InstrumentId,
    OrderBookLevel,
    OutcomeSide,
    Quote,
    Trade,
    Venue,
)
from eventcontracts.research import (
    backtest_one,
    compare_runs,
    load_partition_summary,
    summarize_report,
)
from eventcontracts.storage import ParquetEventStore
from tests.conftest import REPO_ROOT


def _seed(root: Path) -> None:
    store = ParquetEventStore(root)
    instrument = InstrumentId(venue=Venue.KALSHI, market_id="M-1", outcome_id=None)
    for i, price in enumerate(["0.46", "0.44", "0.42", "0.40"]):
        ts = datetime(2026, 1, 1, second=i, tzinfo=UTC)
        store.append_normalized(
            QuoteEvent(
                event_id=EventId(f"q-{i:03d}"),
                quote=Quote(
                    instrument_id=instrument,
                    side=OutcomeSide.YES,
                    bid=OrderBookLevel(price=Decimal("0.39"), quantity=Decimal("100")),
                    ask=OrderBookLevel(price=Decimal("0.40"), quantity=Decimal("100")),
                    exchange_ts=ts,
                    received_at=ts,
                ),
                provenance=EventProvenance(
                    source="fixture",
                    channel="quote",
                    venue=Venue.KALSHI,
                    source_sequence=f"q-{i}",
                ),
            )
        )
        store.append_normalized(
            TradeEvent(
                event_id=EventId(f"t-{i:03d}"),
                trade=Trade(
                    instrument_id=instrument,
                    side=OutcomeSide.YES,
                    price=Decimal(price),
                    quantity=Decimal("10"),
                    trade_id=f"tv-{i}",
                    exchange_ts=ts,
                    received_at=ts,
                ),
                provenance=EventProvenance(
                    source="fixture", channel="trade", venue=Venue.KALSHI
                ),
            )
        )
    store.flush()


def test_backtest_one_and_summarize(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    _seed(data_root)
    result = backtest_one(
        REPO_ROOT / "configs/strategies/example-threshold.toml",
        REPO_ROOT / "configs/sleeves/example-kalshi-paper.toml",
        data_root,
    )
    summary_text = summarize_report(result.report)
    assert "strategy_id" in summary_text
    assert "events_processed" in summary_text


def test_backtest_one_with_parameter_overrides(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    _seed(data_root)
    base = backtest_one(
        REPO_ROOT / "configs/strategies/example-threshold.toml",
        REPO_ROOT / "configs/sleeves/example-kalshi-paper.toml",
        data_root,
    )
    overridden = backtest_one(
        REPO_ROOT / "configs/strategies/example-threshold.toml",
        REPO_ROOT / "configs/sleeves/example-kalshi-paper.toml",
        data_root,
        parameter_overrides={"buy_below": "0.35", "size": "1"},
    )
    # A lower threshold should produce fewer or equal place-order intents.
    assert overridden.report.intents_dispatched <= base.report.intents_dispatched


def test_compare_runs_renders_table(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    _seed(data_root)
    a = backtest_one(
        REPO_ROOT / "configs/strategies/example-threshold.toml",
        REPO_ROOT / "configs/sleeves/example-kalshi-paper.toml",
        data_root,
    )
    b = backtest_one(
        REPO_ROOT / "configs/strategies/example-threshold.toml",
        REPO_ROOT / "configs/sleeves/example-kalshi-paper.toml",
        data_root,
        parameter_overrides={"buy_below": "0.40"},
    )
    table = compare_runs({"base": a.report, "lower_threshold": b.report})
    assert "run" in table
    assert "base" in table and "lower_threshold" in table


def test_load_partition_summary_returns_dict(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    _seed(data_root)
    summary = load_partition_summary(data_root)
    assert isinstance(summary, dict)
    assert summary["root"] == str(data_root)
