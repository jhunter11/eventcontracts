"""Shared domain invariant checks."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

ZERO = Decimal("0")
ONE = Decimal("1")


def require_non_empty(value: str, field_name: str) -> None:
    if not value:
        raise ValueError(f"{field_name} must be non-empty")


def require_aware_datetime(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def require_optional_aware_datetime(value: datetime | None, field_name: str) -> None:
    if value is not None:
        require_aware_datetime(value, field_name)


def require_positive_decimal(value: Decimal, field_name: str) -> None:
    if value <= ZERO:
        raise ValueError(f"{field_name} must be positive: {value}")


def require_non_negative_decimal(value: Decimal, field_name: str) -> None:
    if value < ZERO:
        raise ValueError(f"{field_name} must be non-negative: {value}")


def require_probability_decimal(value: Decimal, field_name: str = "probability") -> None:
    if value < ZERO or value > ONE:
        raise ValueError(f"{field_name} must be between 0 and 1: {value}")


def require_currency(value: str, field_name: str = "currency") -> None:
    require_non_empty(value, field_name)
    if len(value) < 3 or len(value) > 8:
        raise ValueError(f"{field_name} must be a currency code: {value}")
    if value != value.upper():
        raise ValueError(f"{field_name} must be uppercase: {value}")


def require_non_negative_int(value: int, field_name: str) -> None:
    if value < 0:
        raise ValueError(f"{field_name} must be non-negative: {value}")


def require_positive_int(value: int, field_name: str) -> None:
    if value <= 0:
        raise ValueError(f"{field_name} must be positive: {value}")
