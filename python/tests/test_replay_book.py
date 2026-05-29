"""Order book reconstruction from streaming events."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from eventcontracts.domain.events import NormalizedEvent, OrderBookEvent, QuoteEvent, TradeEvent
from eventcontracts.domain.ids import EventId
from eventcontracts.domain.models import (
    InstrumentId,
    OrderBook,
    OrderBookLevel,
    OutcomeSide,
    Quote,
    Trade,
    Venue,
)
from eventcontracts.replay import OrderBookReconstructor, reconstruct_books
from eventcontracts.storage import FileStateStore

NOW = datetime(2026, 1, 15, tzinfo=UTC)
INSTR = InstrumentId(venue=Venue.KALSHI, market_id="M-1", outcome_id=None)


def _quote(side: OutcomeSide, bid: tuple[str, str] | None, ask: tuple[str, str] | None) -> QuoteEvent:
    return QuoteEvent(
        event_id=EventId(f"q-{side.value}-{bid}-{ask}"),
        quote=Quote(
            instrument_id=INSTR,
            side=side,
            bid=OrderBookLevel(price=Decimal(bid[0]), quantity=Decimal(bid[1])) if bid else None,
            ask=OrderBookLevel(price=Decimal(ask[0]), quantity=Decimal(ask[1])) if ask else None,
            exchange_ts=NOW,
            received_at=NOW,
        ),
    )


def _trade(price: str, qty: str, aggressor: OutcomeSide | None = None) -> TradeEvent:
    return TradeEvent(
        event_id=EventId(f"t-{price}-{qty}"),
        trade=Trade(
            instrument_id=INSTR,
            side=OutcomeSide.YES,
            price=Decimal(price),
            quantity=Decimal(qty),
            trade_id=None,
            exchange_ts=NOW,
            received_at=NOW,
            aggressor_side=aggressor,
        ),
    )


def _trade_side(side: OutcomeSide, price: str, qty: str, aggressor: OutcomeSide | None = None) -> TradeEvent:
    return TradeEvent(
        event_id=EventId(f"t-{side.value}-{price}-{qty}"),
        trade=Trade(
            instrument_id=INSTR,
            side=side,
            price=Decimal(price),
            quantity=Decimal(qty),
            trade_id=None,
            exchange_ts=NOW,
            received_at=NOW,
            aggressor_side=aggressor,
        ),
    )


def _book_event() -> OrderBookEvent:
    return OrderBookEvent(
        event_id=EventId("b-1"),
        book=OrderBook(
            instrument_id=INSTR,
            yes_bids=(OrderBookLevel(price=Decimal("0.45"), quantity=Decimal("20")),),
            yes_asks=(OrderBookLevel(price=Decimal("0.55"), quantity=Decimal("30")),),
            no_bids=(),
            no_asks=(),
            exchange_ts=NOW,
            received_at=NOW,
        ),
    )


def _full_binary_book_event() -> OrderBookEvent:
    return OrderBookEvent(
        event_id=EventId("b-full"),
        book=OrderBook(
            instrument_id=INSTR,
            yes_bids=(OrderBookLevel(price=Decimal("0.40"), quantity=Decimal("20")),),
            yes_asks=(OrderBookLevel(price=Decimal("0.55"), quantity=Decimal("30")),),
            no_bids=(OrderBookLevel(price=Decimal("0.45"), quantity=Decimal("30")),),
            no_asks=(OrderBookLevel(price=Decimal("0.60"), quantity=Decimal("20")),),
            exchange_ts=NOW,
            received_at=NOW,
        ),
    )


def test_quote_event_seeds_book_state() -> None:
    rec = OrderBookReconstructor()
    rec.observe(_quote(OutcomeSide.YES, ("0.40", "10"), ("0.55", "20")))
    state = rec.latest(INSTR)
    assert state is not None
    assert state.yes_bids[Decimal("0.40")] == Decimal("10")
    assert state.yes_asks[Decimal("0.55")] == Decimal("20")


def test_reconstructor_evicts_old_book_state() -> None:
    rec = OrderBookReconstructor(max_states=1)
    rec.observe(_quote(OutcomeSide.YES, ("0.40", "10"), ("0.55", "20")))
    other = InstrumentId(venue=Venue.KALSHI, market_id="M-2", outcome_id=None)
    rec.observe(
        QuoteEvent(
            event_id=EventId("q-other"),
            quote=Quote(
                instrument_id=other,
                side=OutcomeSide.YES,
                bid=OrderBookLevel(price=Decimal("0.41"), quantity=Decimal("10")),
                ask=OrderBookLevel(price=Decimal("0.56"), quantity=Decimal("20")),
                exchange_ts=NOW,
                received_at=NOW,
            ),
        )
    )

    assert rec.latest(INSTR) is None
    assert rec.latest(other) is not None


def test_quote_event_replaces_top_of_book_levels_without_ghost_liquidity() -> None:
    rec = OrderBookReconstructor()
    rec.observe(_quote(OutcomeSide.YES, ("0.40", "10"), ("0.55", "20")))
    rec.observe(_quote(OutcomeSide.YES, ("0.41", "11"), ("0.56", "21")))

    state = rec.latest(INSTR)

    assert state is not None
    assert state.yes_bids == {Decimal("0.41"): Decimal("11")}
    assert state.yes_asks == {Decimal("0.56"): Decimal("21")}


def test_book_event_replaces_state() -> None:
    rec = OrderBookReconstructor()
    rec.observe(_quote(OutcomeSide.YES, ("0.40", "10"), ("0.55", "20")))
    rec.observe(_book_event())
    state = rec.latest(INSTR)
    assert state is not None
    assert Decimal("0.40") not in state.yes_bids
    assert state.yes_bids[Decimal("0.45")] == Decimal("20")


def test_trade_decrements_resting_level() -> None:
    rec = OrderBookReconstructor()
    rec.observe(_quote(OutcomeSide.YES, ("0.40", "10"), ("0.55", "20")))
    rec.observe(_trade("0.55", "5", aggressor=OutcomeSide.YES))
    state = rec.latest(INSTR)
    assert state is not None
    assert state.yes_asks[Decimal("0.55")] == Decimal("15")


def test_trade_consuming_full_level_removes_it() -> None:
    rec = OrderBookReconstructor()
    rec.observe(_quote(OutcomeSide.YES, ("0.40", "10"), ("0.55", "20")))
    rec.observe(_trade("0.55", "20", aggressor=OutcomeSide.YES))
    state = rec.latest(INSTR)
    assert state is not None
    assert Decimal("0.55") not in state.yes_asks


def test_trade_decrements_complementary_no_ladder() -> None:
    rec = OrderBookReconstructor()
    rec.observe(_full_binary_book_event())

    rec.observe(_trade_side(OutcomeSide.YES, "0.55", "5", aggressor=OutcomeSide.YES))

    state = rec.latest(INSTR)
    assert state is not None
    assert state.yes_asks[Decimal("0.55")] == Decimal("25")
    assert state.no_bids[Decimal("0.45")] == Decimal("25")


def test_no_trade_decrements_yes_bid_and_no_ask_complement() -> None:
    rec = OrderBookReconstructor()
    rec.observe(_full_binary_book_event())

    rec.observe(_trade_side(OutcomeSide.NO, "0.60", "5", aggressor=OutcomeSide.NO))

    state = rec.latest(INSTR)
    assert state is not None
    assert state.no_asks[Decimal("0.60")] == Decimal("15")
    assert state.yes_bids[Decimal("0.40")] == Decimal("15")


def test_reconstruct_books_interleaves_synthetic_events() -> None:
    src: list[NormalizedEvent] = [
        _quote(OutcomeSide.YES, ("0.40", "10"), ("0.55", "20")),
        _trade("0.55", "5", aggressor=OutcomeSide.YES),
    ]
    output = list(reconstruct_books(iter(src)))
    # Two source events plus two synthetic book events.
    assert len(output) == 4
    assert isinstance(output[1], OrderBookEvent)
    assert isinstance(output[3], OrderBookEvent)


def test_file_state_store_round_trip(tmp_path: Path) -> None:
    from eventcontracts.domain.ids import StrategyId

    store = FileStateStore(tmp_path)
    sid = StrategyId("strat-test")
    assert store.load(sid) is None
    store.save(sid, b"hello world")
    assert store.load(sid) == b"hello world"
    store.clear(sid)
    assert store.load(sid) is None
