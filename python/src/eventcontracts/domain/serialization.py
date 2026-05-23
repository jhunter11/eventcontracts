"""Canonical serialization for domain contracts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from eventcontracts.domain.metadata import FrozenMap, thaw_value


JsonPrimitive = None | bool | int | float | str
JsonValue = JsonPrimitive | list["JsonValue"] | dict[str, "JsonValue"]


def to_primitive(value: Any) -> JsonValue:
    """Convert supported domain values into deterministic JSON primitives."""

    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, FrozenMap):
        return to_primitive(thaw_value(value))
    if isinstance(value, Mapping):
        return {str(key): to_primitive(value[key]) for key in sorted(value)}
    if isinstance(value, tuple | list):
        return [to_primitive(item) for item in value]
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: to_primitive(getattr(value, field.name))
            for field in fields(value)
        }
    raise TypeError(f"unsupported value for serialization: {type(value).__name__}")


def to_canonical_json(value: Any) -> str:
    """Serialize a domain value into stable compact JSON."""

    return json.dumps(
        to_primitive(value),
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(to_canonical_json(value).encode("utf-8")).hexdigest()
