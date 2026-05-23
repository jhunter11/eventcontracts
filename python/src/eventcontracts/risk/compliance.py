"""Compliance policy placeholders."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EligibilityContext:
    account_id: str
    country: str
    region: str | None
    venue: str
    market_category: str | None


class EligibilityPolicy:
    """Hard venue/account/geography gate for order placement."""

    def is_eligible(self, context: EligibilityContext) -> bool:
        raise NotImplementedError
