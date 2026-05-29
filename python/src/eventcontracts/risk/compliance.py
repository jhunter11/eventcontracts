"""Compliance policy placeholders."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class EligibilityContext:
    account_id: str
    country: str
    region: str | None
    venue: str
    market_category: str | None


@dataclass(frozen=True)
class EligibilityPolicy:
    """Hard venue/account/geography gate for order placement.

    With no constraints configured the policy is an explicit allow-all policy,
    not a placeholder that explodes at runtime.
    """

    allowed_countries: tuple[str, ...] = ()
    blocked_countries: tuple[str, ...] = ()
    blackout_start: datetime | None = None
    blackout_end: datetime | None = None

    def is_eligible(self, context: EligibilityContext) -> bool:
        country = context.country.upper()
        if self.allowed_countries and country not in {
            value.upper() for value in self.allowed_countries
        }:
            return False
        if country in {value.upper() for value in self.blocked_countries}:
            return False
        if self.blackout_start is not None and self.blackout_end is not None:
            now = datetime.now(tz=UTC)
            if self.blackout_start <= now <= self.blackout_end:
                return False
        return True


class NoOpEligibilityPolicy(EligibilityPolicy):
    """Named allow-all policy for paper and local research runs."""

    def __init__(self) -> None:
        super().__init__()
