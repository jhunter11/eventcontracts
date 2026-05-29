"""Typed identifiers used across the framework.

NewType wrappers keep IDs from being silently swapped for each other while
remaining cheap string values that can cross process and language boundaries.
"""

from __future__ import annotations

import re
from typing import NewType
from uuid import uuid4

StrategyId = NewType("StrategyId", str)
SleeveId = NewType("SleeveId", str)
RunId = NewType("RunId", str)

ClientOrderId = NewType("ClientOrderId", str)
VenueOrderId = NewType("VenueOrderId", str)
FillId = NewType("FillId", str)

CorrelationId = NewType("CorrelationId", str)
EventId = NewType("EventId", str)

ModelName = NewType("ModelName", str)
ModelVersion = NewType("ModelVersion", str)

FeatureSchemaId = NewType("FeatureSchemaId", str)

_CLIENT_ORDER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")


def validate_client_order_id(value: str) -> ClientOrderId:
    if not _CLIENT_ORDER_ID_RE.fullmatch(value):
        raise ValueError(
            "client_order_id must be 1-64 chars of letters, digits, underscore, dot, colon, or dash"
        )
    return ClientOrderId(value)


def new_client_order_id(prefix: str = "ec") -> ClientOrderId:
    safe_prefix = re.sub(r"[^A-Za-z0-9_.:-]+", "-", prefix).strip("-") or "ec"
    safe_prefix = safe_prefix[:16]
    return validate_client_order_id(f"{safe_prefix}:{uuid4().hex[:24]}")
