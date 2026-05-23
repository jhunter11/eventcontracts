from decimal import Decimal

from eventcontracts.domain.models import InstrumentId, Probability, Venue
from eventcontracts.replay.clock import ReplayClock, ReplayWindow


def test_probability_bounds() -> None:
    assert Probability(Decimal("0.5")).value == Decimal("0.5")


def test_instrument_id() -> None:
    instrument = InstrumentId(venue=Venue.KALSHI, market_id="EXAMPLE")
    assert instrument.venue == Venue.KALSHI
    assert instrument.market_id == "EXAMPLE"


def test_replay_clock_ticks() -> None:
    from datetime import datetime, timedelta, timezone

    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 1, 1, 0, 2, tzinfo=timezone.utc)
    clock = ReplayClock(ReplayWindow(start, end), timedelta(minutes=1))
    assert clock.ticks() == [
        datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc),
        datetime(2026, 1, 1, 0, 2, tzinfo=timezone.utc),
    ]
