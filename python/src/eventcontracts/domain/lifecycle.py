"""Market lifecycle and settlement events."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from eventcontracts.domain.metadata import FrozenMap, freeze_mapping
from eventcontracts.domain.models import InstrumentId, OutcomeSide
from eventcontracts.domain.validation import (
    require_aware_datetime,
    require_non_empty,
    require_optional_aware_datetime,
    require_probability_decimal,
    require_utc_datetime,
)


class MarketLifecycleKind(str, Enum):
    LISTED = "listed"
    OPENED = "opened"
    PAUSED = "paused"
    RESUMED = "resumed"
    METADATA_UPDATED = "metadata_updated"
    CLOSED = "closed"
    DETERMINED = "determined"
    DISPUTED = "disputed"
    FINALIZED = "finalized"


@dataclass(frozen=True)
class MarketLifecycleEvent:
    instrument_id: InstrumentId
    kind: MarketLifecycleKind
    exchange_ts: datetime | None
    received_at: datetime
    reason: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=FrozenMap)

    def __post_init__(self) -> None:
        require_optional_aware_datetime(self.exchange_ts, "exchange_ts")
        require_aware_datetime(self.received_at, "received_at")
        if self.reason is not None:
            require_non_empty(self.reason, "reason")
        object.__setattr__(self, "metadata", freeze_mapping(self.metadata))


@dataclass(frozen=True)
class SettlementEvent:
    instrument_id: InstrumentId
    resolved_side: OutcomeSide | None
    payout_per_contract: Decimal
    currency: str
    settled_at: datetime
    source: str
    metadata: Mapping[str, Any] = field(default_factory=FrozenMap)

    def __post_init__(self) -> None:
        require_probability_decimal(self.payout_per_contract, "payout_per_contract")
        require_utc_datetime(self.settled_at, "settled_at")
        require_non_empty(self.source, "source")
        object.__setattr__(self, "metadata", freeze_mapping(self.metadata))
