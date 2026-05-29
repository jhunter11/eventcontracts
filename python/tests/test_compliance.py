"""Compliance policy gates."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from eventcontracts.risk.compliance import (
    EligibilityContext,
    EligibilityPolicy,
    NoOpEligibilityPolicy,
)


def _context(country: str = "US") -> EligibilityContext:
    return EligibilityContext(
        account_id="acct-1",
        country=country,
        region="NY",
        venue="kalshi",
        market_category="weather",
    )


def test_noop_eligibility_policy_allows_by_default() -> None:
    assert NoOpEligibilityPolicy().is_eligible(_context()) is True


def test_eligibility_policy_blocks_disallowed_country() -> None:
    policy = EligibilityPolicy(allowed_countries=("US",))

    assert policy.is_eligible(_context("US")) is True
    assert policy.is_eligible(_context("CA")) is False


def test_eligibility_policy_blocks_blackout_window() -> None:
    now = datetime.now(tz=UTC)
    policy = EligibilityPolicy(
        blackout_start=now - timedelta(seconds=1),
        blackout_end=now + timedelta(seconds=60),
    )

    assert policy.is_eligible(_context()) is False
