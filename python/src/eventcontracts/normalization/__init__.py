"""Normalization for venue-specific contracts and events."""

from eventcontracts.normalization.basic import (
    BASIC_NORMALIZERS,
    normalize_order_book,
    normalize_quote,
    normalize_trade,
)
from eventcontracts.normalization.contracts import (
    ContractMatchCandidate,
    ContractMatchDecision,
    ContractNormalizer,
)
from eventcontracts.normalization.kalshi import KalshiNormalizer, kalshi_normalizers
from eventcontracts.normalization.pipeline import (
    EventNormalizer,
    NormalizationPipeline,
    NormalizationResult,
    normalize_all,
)
from eventcontracts.storage.interfaces import NormalizationReject

__all__ = [
    "BASIC_NORMALIZERS",
    "ContractMatchCandidate",
    "ContractMatchDecision",
    "ContractNormalizer",
    "EventNormalizer",
    "KalshiNormalizer",
    "NormalizationReject",
    "NormalizationPipeline",
    "NormalizationResult",
    "normalize_all",
    "normalize_order_book",
    "normalize_quote",
    "normalize_trade",
    "kalshi_normalizers",
]
