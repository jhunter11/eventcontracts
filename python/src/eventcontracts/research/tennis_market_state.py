"""Tennis lifecycle and sharp-reference valuation helpers.

The promoted tennis model already has feature engineering and a live strategy.
This module adds the missing pre-trade state layer: discover a concrete Kalshi
match market, attach sharp/reference odds, turn those odds into a de-vigged
probability, and emit a signal/edge record that is explicit about odds coverage,
model-vs-sharp agreement, fees, and lifecycle status.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from eventcontracts.domain.validation import require_aware_datetime, require_non_empty
from eventcontracts.research.ledger import stable_hash

_SPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class TennisMarketCandidate:
    """One Kalshi tennis match market candidate."""

    market_id: str
    player_1: str
    player_2: str
    scheduled_start: datetime | None
    lifecycle_status: str
    source: str = "kalshi"
    tournament: str | None = None
    surface: str | None = None

    def __post_init__(self) -> None:
        require_non_empty(self.market_id, "market_id")
        require_non_empty(self.player_1, "player_1")
        require_non_empty(self.player_2, "player_2")
        require_non_empty(self.lifecycle_status, "lifecycle_status")
        require_non_empty(self.source, "source")
        if self.scheduled_start is not None:
            require_aware_datetime(self.scheduled_start, "scheduled_start")

    @property
    def player_1_key(self) -> str:
        return normalize_player_name(self.player_1)

    @property
    def player_2_key(self) -> str:
        return normalize_player_name(self.player_2)


@dataclass(frozen=True)
class SharpOddsSnapshot:
    """Two-sided sharp/reference odds for a tennis match."""

    market_id: str
    player_1: str
    player_2: str
    p1_decimal_odds: float
    p2_decimal_odds: float
    as_of: datetime
    source: str

    def __post_init__(self) -> None:
        require_non_empty(self.market_id, "market_id")
        require_non_empty(self.player_1, "player_1")
        require_non_empty(self.player_2, "player_2")
        require_aware_datetime(self.as_of, "as_of")
        require_non_empty(self.source, "source")
        if self.p1_decimal_odds <= 1.0 or self.p2_decimal_odds <= 1.0:
            raise ValueError("decimal odds must be > 1.0")

    @property
    def p1_raw_implied(self) -> float:
        return 1.0 / self.p1_decimal_odds

    @property
    def p2_raw_implied(self) -> float:
        return 1.0 / self.p2_decimal_odds

    @property
    def overround(self) -> float:
        return self.p1_raw_implied + self.p2_raw_implied

    @property
    def p1_devig_probability(self) -> float:
        return self.p1_raw_implied / self.overround

    @property
    def p2_devig_probability(self) -> float:
        return self.p2_raw_implied / self.overround


@dataclass(frozen=True)
class TennisReferenceValuation:
    """Fair value produced from sharp odds plus an optional model probability."""

    market_id: str
    as_of: datetime
    player_1: str
    player_2: str
    p1_fair_probability: float
    p1_sharp_probability: float
    p1_model_probability: float | None
    model_weight: float
    model_sharp_disagreement: float | None
    confidence: float
    odds_present: bool
    feature_hash: str
    schema_version: str = "tennis-reference-valuation-v1"

    def __post_init__(self) -> None:
        require_non_empty(self.market_id, "market_id")
        require_aware_datetime(self.as_of, "as_of")
        require_non_empty(self.player_1, "player_1")
        require_non_empty(self.player_2, "player_2")
        for name in ("p1_fair_probability", "p1_sharp_probability", "confidence"):
            value = getattr(self, name)
            if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be finite and in [0, 1]")
        if self.p1_model_probability is not None and not 0.0 <= self.p1_model_probability <= 1.0:
            raise ValueError("p1_model_probability must be in [0, 1]")
        if not 0.0 <= self.model_weight <= 1.0:
            raise ValueError("model_weight must be in [0, 1]")
        require_non_empty(self.feature_hash, "feature_hash")


@dataclass(frozen=True)
class TennisReferenceDecision:
    """Paper-only executable-edge decision for a tennis match market."""

    market_id: str
    as_of: datetime
    fair_yes: float
    side: str
    executable_price: float | None
    raw_edge: float | None
    fee: float | None
    net_edge: float | None
    candidate: bool
    reason: str
    schema_version: str = "tennis-reference-decision-v1"

    def __post_init__(self) -> None:
        require_non_empty(self.market_id, "market_id")
        require_aware_datetime(self.as_of, "as_of")
        if not 0.0 <= self.fair_yes <= 1.0:
            raise ValueError("fair_yes must be in [0, 1]")
        if self.side not in {"YES", "NO", "NONE"}:
            raise ValueError("side must be YES, NO, or NONE")
        require_non_empty(self.reason, "reason")


def normalize_player_name(name: str) -> str:
    """Accent/punctuation-insensitive player key for lifecycle matching."""

    text = unicodedata.normalize("NFKD", name)
    ascii_text = "".join(ch for ch in text if not unicodedata.combining(ch))
    cleaned = re.sub(r"[^a-zA-Z0-9 ]+", " ", ascii_text).lower()
    return _SPACE_RE.sub(" ", cleaned).strip()


def is_tradeable_lifecycle(
    candidate: TennisMarketCandidate,
    *,
    now: datetime,
    max_hours_to_start: float = 72.0,
) -> bool:
    """Return True when a market is open/upcoming enough for pre-match research."""

    require_aware_datetime(now, "now")
    if candidate.lifecycle_status.lower() not in {"open", "active", "upcoming"}:
        return False
    if candidate.scheduled_start is None:
        return True
    hours = (candidate.scheduled_start - now).total_seconds() / 3600.0
    return 0.0 <= hours <= max_hours_to_start


def build_reference_valuation(
    candidate: TennisMarketCandidate,
    sharp: SharpOddsSnapshot,
    *,
    model_probability: float | None = None,
    model_weight: float = 0.35,
) -> TennisReferenceValuation:
    """Build a fair value from de-vigged sharp odds plus optional model input."""

    if candidate.market_id != sharp.market_id:
        raise ValueError("candidate and sharp market_id differ")
    if candidate.player_1_key != normalize_player_name(sharp.player_1):
        raise ValueError("candidate player_1 does not match sharp odds")
    if candidate.player_2_key != normalize_player_name(sharp.player_2):
        raise ValueError("candidate player_2 does not match sharp odds")
    if not 0.0 <= model_weight <= 1.0:
        raise ValueError("model_weight must be in [0, 1]")
    if model_probability is not None and not 0.0 <= model_probability <= 1.0:
        raise ValueError("model_probability must be in [0, 1]")

    sharp_p = sharp.p1_devig_probability
    if model_probability is None:
        fair_p = sharp_p
        disagreement = None
        confidence = max(sharp_p, 1.0 - sharp_p)
        applied_weight = 0.0
    else:
        fair_p = model_weight * model_probability + (1.0 - model_weight) * sharp_p
        disagreement = abs(model_probability - sharp_p)
        confidence = max(fair_p, 1.0 - fair_p) * (1.0 - disagreement)
        applied_weight = model_weight
    features = {
        "market_id": candidate.market_id,
        "players": [candidate.player_1, candidate.player_2],
        "sharp_source": sharp.source,
        "p1_decimal_odds": sharp.p1_decimal_odds,
        "p2_decimal_odds": sharp.p2_decimal_odds,
        "p1_sharp_probability": sharp_p,
        "p1_model_probability": model_probability,
        "model_weight": applied_weight,
        "overround": sharp.overround,
    }
    return TennisReferenceValuation(
        market_id=candidate.market_id,
        as_of=sharp.as_of,
        player_1=candidate.player_1,
        player_2=candidate.player_2,
        p1_fair_probability=_clip_probability(fair_p),
        p1_sharp_probability=sharp_p,
        p1_model_probability=model_probability,
        model_weight=applied_weight,
        model_sharp_disagreement=disagreement,
        confidence=_clip_probability(confidence),
        odds_present=True,
        feature_hash=stable_hash(features),
    )


def evaluate_tennis_reference_candidate(
    valuation: TennisReferenceValuation,
    *,
    yes_bid: float | None,
    yes_ask: float | None,
    min_net_edge: float = 0.015,
    min_confidence: float = 0.55,
    max_model_sharp_disagreement: float = 0.18,
    fee_coeff: float = 0.07,
) -> TennisReferenceDecision:
    """Evaluate whether a tennis market is a paper candidate after costs."""

    if (
        valuation.model_sharp_disagreement is not None
        and valuation.model_sharp_disagreement > max_model_sharp_disagreement
    ):
        return _tennis_no_candidate(valuation, "model_sharp_disagreement_too_large")
    if valuation.confidence < min_confidence:
        return _tennis_no_candidate(valuation, "confidence_below_threshold")
    if yes_bid is None or yes_ask is None:
        return _tennis_no_candidate(valuation, "missing_two_sided_quote")
    if not 0.0 <= yes_bid <= yes_ask <= 1.0:
        return _tennis_no_candidate(valuation, "invalid_quote")
    fair_yes = valuation.p1_fair_probability
    yes_fee = fee_coeff * yes_ask * (1.0 - yes_ask)
    yes_net = fair_yes - yes_ask - yes_fee
    no_price = 1.0 - yes_bid
    no_fee = fee_coeff * no_price * (1.0 - no_price)
    no_net = (1.0 - fair_yes) - no_price - no_fee
    if yes_net >= no_net:
        side, executable, raw_edge, fee, net_edge = "YES", yes_ask, fair_yes - yes_ask, yes_fee, yes_net
    else:
        side, executable, raw_edge, fee, net_edge = "NO", no_price, (1.0 - fair_yes) - no_price, no_fee, no_net
    reason = "paper_candidate" if net_edge >= min_net_edge else "net_edge_below_threshold"
    return TennisReferenceDecision(
        market_id=valuation.market_id,
        as_of=valuation.as_of,
        fair_yes=fair_yes,
        side=side,
        executable_price=executable,
        raw_edge=raw_edge,
        fee=fee,
        net_edge=net_edge,
        candidate=net_edge >= min_net_edge,
        reason=reason,
    )


def external_signal_payload(valuation: TennisReferenceValuation) -> dict[str, Any]:
    """Payload shape consumed by ``sports_tennis_xgboost``."""

    return {
        "market_id": valuation.market_id,
        "player_1": valuation.player_1,
        "player_2": valuation.player_2,
        "player_1_win_probability": round(valuation.p1_fair_probability, 6),
        "model_confidence": round(valuation.confidence, 6),
        "odds_present": valuation.odds_present,
        "p1_sharp_probability": round(valuation.p1_sharp_probability, 6),
        "p1_model_probability": (
            round(valuation.p1_model_probability, 6) if valuation.p1_model_probability is not None else None
        ),
        "model_sharp_disagreement": (
            round(valuation.model_sharp_disagreement, 6)
            if valuation.model_sharp_disagreement is not None
            else None
        ),
        "feature_hash": valuation.feature_hash,
        "schema_version": valuation.schema_version,
    }


def _tennis_no_candidate(valuation: TennisReferenceValuation, reason: str) -> TennisReferenceDecision:
    return TennisReferenceDecision(
        market_id=valuation.market_id,
        as_of=valuation.as_of,
        fair_yes=valuation.p1_fair_probability,
        side="NONE",
        executable_price=None,
        raw_edge=None,
        fee=None,
        net_edge=None,
        candidate=False,
        reason=reason,
    )


def _clip_probability(value: float) -> float:
    return max(0.0, min(1.0, value))
