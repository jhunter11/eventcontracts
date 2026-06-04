"""NBA alternate-spread sharp-reference validator.

This module is a research-only producer/evaluator for Kalshi NBA spread ladders.
It anchors a normal final-margin distribution to a live sportsbook spread and
moneyline, then compares each Kalshi alternate-spread market to executable touch
after fees. It never submits or cancels orders; positive rows are paper/shadow
signals until CLV, fill, and settlement evidence prove them.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from statistics import NormalDist

from eventcontracts.domain.validation import require_aware_datetime, require_non_empty
from eventcontracts.research.calibration import net_edge
from eventcontracts.research.ledger import to_jsonable, write_jsonl

_EPS = 1e-12
_TEAM_RE = re.compile(
    r"^(?P<team>.+?) wins by over (?P<threshold>[0-9]+(?:\.[0-9]+)?) (?P<unit>points|runs)",
    re.I,
)


@dataclass(frozen=True)
class NbaSpreadValidationConfig:
    """Gate settings for a live NBA spread-ladder validation pass."""

    min_net_edge: float = 0.015
    min_executable_size: float = 1.0
    max_source_age_seconds: float = 180.0
    max_scoreboard_win_probability_disagreement: float | None = 0.12
    require_source_timestamp: bool = False
    paper_contracts: int = 5
    fee_coeff: float = 0.07
    slippage: float = 0.0

    def __post_init__(self) -> None:
        if self.min_net_edge < 0.0:
            raise ValueError("min_net_edge must be non-negative")
        if self.min_executable_size < 0.0:
            raise ValueError("min_executable_size must be non-negative")
        if self.max_source_age_seconds < 0.0:
            raise ValueError("max_source_age_seconds must be non-negative")
        if (
            self.max_scoreboard_win_probability_disagreement is not None
            and self.max_scoreboard_win_probability_disagreement < 0.0
        ):
            raise ValueError("max_scoreboard_win_probability_disagreement must be non-negative")
        if self.paper_contracts <= 0:
            raise ValueError("paper_contracts must be positive")
        if self.fee_coeff < 0.0:
            raise ValueError("fee_coeff must be non-negative")
        if self.slippage < 0.0:
            raise ValueError("slippage must be non-negative")


@dataclass(frozen=True)
class NbaGameState:
    """Point-in-time scoreboard state for one NBA game."""

    event_id: str
    name: str
    received_at: datetime
    home_team: str
    away_team: str
    home_abbrev: str
    away_abbrev: str
    home_score: int
    away_score: int
    status_state: str
    status_detail: str
    period: int
    completed: bool
    scoreboard_home_win_probability: float | None = None

    def __post_init__(self) -> None:
        require_non_empty(self.event_id, "event_id")
        require_non_empty(self.name, "name")
        require_aware_datetime(self.received_at, "received_at")
        require_non_empty(self.home_team, "home_team")
        require_non_empty(self.away_team, "away_team")
        require_non_empty(self.home_abbrev, "home_abbrev")
        require_non_empty(self.away_abbrev, "away_abbrev")
        require_non_empty(self.status_state, "status_state")
        require_non_empty(self.status_detail, "status_detail")
        if self.period < 0:
            raise ValueError("period must be non-negative")
        if self.scoreboard_home_win_probability is not None:
            _require_probability(self.scoreboard_home_win_probability, "scoreboard_home_win_probability")

    @property
    def home_margin(self) -> int:
        return self.home_score - self.away_score


@dataclass(frozen=True)
class NbaLiveOddsAnchor:
    """Live sportsbook line used to anchor the margin distribution."""

    provider: str
    as_of: datetime
    timestamp_basis: str
    home_point_spread: float
    home_spread_american: float
    away_spread_american: float
    home_moneyline_american: float
    away_moneyline_american: float
    over_under: float | None = None

    def __post_init__(self) -> None:
        require_non_empty(self.provider, "provider")
        require_aware_datetime(self.as_of, "as_of")
        require_non_empty(self.timestamp_basis, "timestamp_basis")
        for name in (
            "home_point_spread",
            "home_spread_american",
            "away_spread_american",
            "home_moneyline_american",
            "away_moneyline_american",
        ):
            value = getattr(self, name)
            if not math.isfinite(value):
                raise ValueError(f"{name} must be finite")

    @property
    def home_cover_threshold(self) -> float:
        """Home spread bet wins when final home margin is greater than this."""

        return -self.home_point_spread

    @property
    def home_cover_probability(self) -> float:
        return devig_two_way(
            american_to_probability(self.home_spread_american),
            american_to_probability(self.away_spread_american),
        )[0]

    @property
    def home_win_probability(self) -> float:
        return devig_two_way(
            american_to_probability(self.home_moneyline_american),
            american_to_probability(self.away_moneyline_american),
        )[0]


@dataclass(frozen=True)
class NbaMarginDistribution:
    """Normal approximation to the final home-margin distribution."""

    mean_home_margin: float
    sigma_home_margin: float
    home_cover_threshold: float
    home_cover_probability: float
    home_win_probability: float
    method: str = "normal_from_live_spread_and_moneyline"

    def __post_init__(self) -> None:
        if not math.isfinite(self.mean_home_margin):
            raise ValueError("mean_home_margin must be finite")
        if not math.isfinite(self.sigma_home_margin) or self.sigma_home_margin <= 0.0:
            raise ValueError("sigma_home_margin must be finite and positive")
        _require_probability(self.home_cover_probability, "home_cover_probability")
        _require_probability(self.home_win_probability, "home_win_probability")
        require_non_empty(self.method, "method")

    @property
    def normal(self) -> NormalDist:
        return NormalDist(mu=self.mean_home_margin, sigma=self.sigma_home_margin)

    def probability_home_margin_gt(self, threshold: float) -> float:
        return _clip_probability(1.0 - self.normal.cdf(threshold))

    def probability_home_margin_lt(self, threshold: float) -> float:
        return _clip_probability(self.normal.cdf(threshold))


@dataclass(frozen=True)
class NbaSpreadMarketQuote:
    """Kalshi executable-touch quote for one alternate-spread contract."""

    ticker: str
    title: str
    team_role: str
    team_phrase: str
    threshold: float
    received_at: datetime
    yes_bid: float
    yes_ask: float
    no_bid: float | None = None
    no_ask: float | None = None
    yes_bid_size: float | None = None
    yes_ask_size: float | None = None
    no_bid_size: float | None = None
    no_ask_size: float | None = None
    status: str | None = None
    close_time: str | None = None
    expected_expiration_time: str | None = None

    def __post_init__(self) -> None:
        require_non_empty(self.ticker, "ticker")
        require_non_empty(self.title, "title")
        if self.team_role not in {"home", "away"}:
            raise ValueError("team_role must be home or away")
        require_non_empty(self.team_phrase, "team_phrase")
        if self.threshold < 0.0 or not math.isfinite(self.threshold):
            raise ValueError("threshold must be finite and non-negative")
        require_aware_datetime(self.received_at, "received_at")
        _require_probability(self.yes_bid, "yes_bid")
        _require_probability(self.yes_ask, "yes_ask")
        if self.yes_ask < self.yes_bid:
            raise ValueError("yes_ask must be >= yes_bid")

    @property
    def spread(self) -> float:
        return self.yes_ask - self.yes_bid

    def side_size(self, side: str) -> float | None:
        if side == "YES":
            return self.yes_ask_size
        if side == "NO":
            return self.no_ask_size
        return None


@dataclass(frozen=True)
class NbaSpreadValuation:
    """Fair YES value for one Kalshi NBA spread market."""

    ticker: str
    as_of: datetime
    fair_yes: float
    valuation_method: str
    threshold: float
    team_role: str
    feature_payload: Mapping[str, object]

    def __post_init__(self) -> None:
        require_non_empty(self.ticker, "ticker")
        require_aware_datetime(self.as_of, "as_of")
        _require_probability(self.fair_yes, "fair_yes")
        require_non_empty(self.valuation_method, "valuation_method")
        if self.team_role not in {"home", "away"}:
            raise ValueError("team_role must be home or away")


@dataclass(frozen=True)
class NbaSpreadDecision:
    """Paper-only executable-edge decision for one spread market."""

    ticker: str
    as_of: datetime
    side: str
    fair_yes: float
    executable_price: float | None
    raw_edge: float | None
    fee: float | None
    net_edge: float | None
    executable_size: float | None
    expected_profit_dollars: float | None
    candidate: bool
    reason: str
    valuation_method: str
    source_age_seconds: float | None
    source_timestamp_basis: str

    def __post_init__(self) -> None:
        require_non_empty(self.ticker, "ticker")
        require_aware_datetime(self.as_of, "as_of")
        if self.side not in {"YES", "NO", "NONE"}:
            raise ValueError("side must be YES, NO, or NONE")
        _require_probability(self.fair_yes, "fair_yes")
        require_non_empty(self.reason, "reason")
        require_non_empty(self.valuation_method, "valuation_method")
        require_non_empty(self.source_timestamp_basis, "source_timestamp_basis")

    def as_signal_payload(self) -> dict[str, object]:
        """ExternalSignalEvent-shaped payload for the generic external_edge sleeve."""

        return {
            "market_id": self.ticker,
            "probability": round(self.fair_yes, 6),
            "yes_probability": round(self.fair_yes, 6),
            "confidence": round(max(self.fair_yes, 1.0 - self.fair_yes), 6),
            "strategy_family": "spread_sharp_reference",
            "source": "sharp-consensus",
            "candidate": self.candidate,
            "reason": self.reason,
            "side": self.side,
            "net_edge": round(self.net_edge, 6) if self.net_edge is not None else None,
            "executable_price": (
                round(self.executable_price, 6) if self.executable_price is not None else None
            ),
            "source_age_seconds": (
                round(self.source_age_seconds, 3) if self.source_age_seconds is not None else None
            ),
            "valuation_method": self.valuation_method,
        }


@dataclass(frozen=True)
class NbaSpreadValidationReport:
    """Complete live/read-only validation result."""

    as_of: datetime
    game: NbaGameState
    anchor: NbaLiveOddsAnchor
    distribution: NbaMarginDistribution
    markets: tuple[NbaSpreadMarketQuote, ...]
    valuations: tuple[NbaSpreadValuation, ...]
    decisions: tuple[NbaSpreadDecision, ...]
    config: NbaSpreadValidationConfig
    decision: str
    caveat: str
    schema_version: str = "nba-spread-validation-v1"

    def as_dict(self) -> dict[str, object]:
        candidates = [row for row in self.decisions if row.candidate]
        best = max(
            (row for row in self.decisions if row.net_edge is not None),
            key=lambda row: float(row.net_edge or -999.0),
            default=None,
        )
        total_expected = sum(row.expected_profit_dollars or 0.0 for row in candidates)
        return {
            "schema_version": self.schema_version,
            "as_of": self.as_of.isoformat(),
            "game": _to_jsonable_dataclass(self.game),
            "anchor": _to_jsonable_dataclass(self.anchor),
            "distribution": _to_jsonable_dataclass(self.distribution),
            "config": _to_jsonable_dataclass(self.config),
            "summary": {
                "markets": len(self.markets),
                "candidates": len(candidates),
                "best_ticker": best.ticker if best else None,
                "best_side": best.side if best else None,
                "best_net_edge": best.net_edge if best else None,
                "paper_contracts": self.config.paper_contracts,
                "candidate_expected_profit_dollars": total_expected,
                "decision": self.decision,
            },
            "valuations": [_to_jsonable_dataclass(row) for row in self.valuations],
            "decisions": [_to_jsonable_dataclass(row) for row in self.decisions],
            "caveat": self.caveat,
        }


@dataclass(frozen=True)
class NbaSpreadMarkoutRow:
    """One candidate valued against a later public bid."""

    ticker: str
    side: str
    entry_price: float
    entry_fee: float
    entry_net_edge: float
    markout_bid: float | None
    markout_after_entry_fee: float | None
    bid_size: float | None
    positive: bool
    reason: str

    def __post_init__(self) -> None:
        require_non_empty(self.ticker, "ticker")
        if self.side not in {"YES", "NO"}:
            raise ValueError("side must be YES or NO")
        _require_probability(self.entry_price, "entry_price")
        if self.entry_fee < 0.0:
            raise ValueError("entry_fee must be non-negative")
        require_non_empty(self.reason, "reason")


@dataclass(frozen=True)
class NbaSpreadMarkoutReport:
    """Markout summary for a prior NBA spread validation report."""

    as_of: datetime
    entry_report: str
    paper_contracts: int
    rows: tuple[NbaSpreadMarkoutRow, ...]
    decision: str
    schema_version: str = "nba-spread-markout-v1"

    def __post_init__(self) -> None:
        require_aware_datetime(self.as_of, "as_of")
        require_non_empty(self.entry_report, "entry_report")
        if self.paper_contracts <= 0:
            raise ValueError("paper_contracts must be positive")
        require_non_empty(self.decision, "decision")

    def as_dict(self) -> dict[str, object]:
        valid = [row for row in self.rows if row.markout_after_entry_fee is not None]
        values = [float(row.markout_after_entry_fee) for row in valid if row.markout_after_entry_fee is not None]
        total = sum(value * self.paper_contracts for value in values)
        mean = sum(values) / len(values) if values else None
        sorted_values = sorted(values)
        median = sorted_values[len(sorted_values) // 2] if sorted_values else None
        return {
            "schema_version": self.schema_version,
            "as_of": self.as_of.isoformat(),
            "entry_report": self.entry_report,
            "paper_contracts": self.paper_contracts,
            "summary": {
                "entries": len(self.rows),
                "markout_rows": len(valid),
                "positive_markouts": sum(1 for row in valid if row.positive),
                "mean_markout_after_entry_fee": mean,
                "median_markout_after_entry_fee": median,
                "total_markout_dollars": total,
                "decision": self.decision,
            },
            "rows": [_to_jsonable_dataclass(row) for row in self.rows],
        }


@dataclass(frozen=True)
class NbaSpreadSettlementRow:
    """Hold-to-settlement PnL for one candidate entry."""

    ticker: str
    side: str
    threshold: float
    team_role: str
    entry_price: float
    entry_fee: float
    yes_settled: bool | None
    payout: float | None
    pnl_after_entry_fee: float | None
    reason: str

    def __post_init__(self) -> None:
        require_non_empty(self.ticker, "ticker")
        if self.side not in {"YES", "NO"}:
            raise ValueError("side must be YES or NO")
        if self.team_role not in {"home", "away"}:
            raise ValueError("team_role must be home or away")
        _require_probability(self.entry_price, "entry_price")
        if self.entry_fee < 0.0:
            raise ValueError("entry_fee must be non-negative")
        require_non_empty(self.reason, "reason")


@dataclass(frozen=True)
class NbaSpreadSettlementReport:
    """Settlement PnL report for a prior NBA spread validation report."""

    as_of: datetime
    entry_report: str
    game: NbaGameState
    paper_contracts: int
    rows: tuple[NbaSpreadSettlementRow, ...]
    decision: str
    schema_version: str = "nba-spread-settlement-v1"

    def __post_init__(self) -> None:
        require_aware_datetime(self.as_of, "as_of")
        require_non_empty(self.entry_report, "entry_report")
        if self.paper_contracts <= 0:
            raise ValueError("paper_contracts must be positive")
        require_non_empty(self.decision, "decision")

    def as_dict(self) -> dict[str, object]:
        settled = [row for row in self.rows if row.pnl_after_entry_fee is not None]
        values = [float(row.pnl_after_entry_fee) for row in settled if row.pnl_after_entry_fee is not None]
        total = sum(value * self.paper_contracts for value in values)
        mean = sum(values) / len(values) if values else None
        return {
            "schema_version": self.schema_version,
            "as_of": self.as_of.isoformat(),
            "entry_report": self.entry_report,
            "paper_contracts": self.paper_contracts,
            "game": _to_jsonable_dataclass(self.game),
            "summary": {
                "entries": len(self.rows),
                "settled_rows": len(settled),
                "winning_rows": sum(1 for row in settled if (row.pnl_after_entry_fee or 0.0) > 0.0),
                "mean_pnl_after_entry_fee": mean,
                "total_pnl_dollars": total,
                "decision": self.decision,
            },
            "rows": [_to_jsonable_dataclass(row) for row in self.rows],
        }


def american_to_probability(american: float) -> float:
    """Convert American odds to raw implied probability."""

    if not math.isfinite(american) or american == 0.0:
        raise ValueError("american odds must be finite and non-zero")
    if american > 0:
        return 100.0 / (american + 100.0)
    return -american / (-american + 100.0)


def devig_two_way(raw_a: float, raw_b: float) -> tuple[float, float]:
    """Normalize two raw implied probabilities to remove overround."""

    _require_positive(raw_a, "raw_a")
    _require_positive(raw_b, "raw_b")
    total = raw_a + raw_b
    return raw_a / total, raw_b / total


def fit_margin_distribution(anchor: NbaLiveOddsAnchor) -> NbaMarginDistribution:
    """Fit a normal home-margin distribution from live spread and moneyline."""

    threshold = anchor.home_cover_threshold
    if abs(threshold) < 1e-9:
        raise ValueError("cannot identify distribution scale from a pick'em spread")
    nd = NormalDist()
    z_win = nd.inv_cdf(_clip_for_inverse(anchor.home_win_probability))
    z_cover = nd.inv_cdf(_clip_for_inverse(anchor.home_cover_probability))
    denom = z_win - z_cover
    if abs(denom) < 1e-9:
        raise ValueError("spread and moneyline anchor points imply zero margin variance")
    sigma = threshold / denom
    if sigma <= 0.0:
        raise ValueError("spread and moneyline anchor points imply negative margin variance")
    mean = z_win * sigma
    return NbaMarginDistribution(
        mean_home_margin=mean,
        sigma_home_margin=sigma,
        home_cover_threshold=threshold,
        home_cover_probability=anchor.home_cover_probability,
        home_win_probability=anchor.home_win_probability,
    )


def parse_nba_game_state(payload: Mapping[str, object], *, received_at: datetime) -> NbaGameState:
    """Parse the first ESPN scoreboard event or an ESPN event object."""

    require_aware_datetime(received_at, "received_at")
    event = payload
    if "events" in payload:
        events = payload.get("events")
        if not isinstance(events, Sequence) or not events:
            raise ValueError("scoreboard payload has no events")
        event = _mapping(events[0], "event")
    competitions = _sequence(event.get("competitions"), "competitions")
    competition = _mapping(competitions[0], "competition")
    competitors = [_mapping(item, "competitor") for item in _sequence(competition.get("competitors"), "competitors")]
    home = _competitor_by_home_away(competitors, "home")
    away = _competitor_by_home_away(competitors, "away")
    status = _mapping(competition.get("status") or event.get("status"), "status")
    status_type = _mapping(status.get("type"), "status.type")
    scoreboard_home_win_probability = _scoreboard_home_win_probability(competition)
    return NbaGameState(
        event_id=str(event.get("id") or competition.get("id") or ""),
        name=str(event.get("name") or ""),
        received_at=received_at,
        home_team=str(_mapping(home.get("team"), "home.team").get("displayName") or ""),
        away_team=str(_mapping(away.get("team"), "away.team").get("displayName") or ""),
        home_abbrev=str(_mapping(home.get("team"), "home.team").get("abbreviation") or ""),
        away_abbrev=str(_mapping(away.get("team"), "away.team").get("abbreviation") or ""),
        home_score=int(float(str(home.get("score") or "0"))),
        away_score=int(float(str(away.get("score") or "0"))),
        status_state=str(status_type.get("state") or ""),
        status_detail=str(status_type.get("detail") or status_type.get("description") or ""),
        period=int(float(str(status.get("period") or "0"))),
        completed=bool(status_type.get("completed")),
        scoreboard_home_win_probability=scoreboard_home_win_probability,
    )


def parse_espn_live_odds_anchor(
    payload: Mapping[str, object],
    *,
    received_at: datetime,
    prefer_provider_contains: str = "live",
) -> NbaLiveOddsAnchor:
    """Parse a live odds anchor from ESPN core odds payload."""

    require_aware_datetime(received_at, "received_at")
    items = [_mapping(item, "odds_item") for item in _sequence(payload.get("items"), "items")]
    if not items:
        raise ValueError("ESPN odds payload has no items")
    needle = prefer_provider_contains.lower()
    item = next(
        (
            row
            for row in items
            if needle in str(_mapping(row.get("provider"), "provider").get("name") or "").lower()
        ),
        items[-1],
    )
    home_team_odds = _mapping(item.get("homeTeamOdds"), "homeTeamOdds")
    away_team_odds = _mapping(item.get("awayTeamOdds"), "awayTeamOdds")
    current_home = _mapping(home_team_odds.get("current") or home_team_odds, "home.current")
    current_away = _mapping(away_team_odds.get("current") or away_team_odds, "away.current")
    home_spread = _point_spread_value(current_home.get("pointSpread"), item.get("spread"))
    return NbaLiveOddsAnchor(
        provider=str(_mapping(item.get("provider"), "provider").get("name") or ""),
        as_of=received_at,
        timestamp_basis="espn_api_received_at_no_odds_last_modified",
        home_point_spread=home_spread,
        home_spread_american=_required_float(
            home_team_odds.get("spreadOdds") or _spread_american(current_home),
            "home_spread_american",
        ),
        away_spread_american=_required_float(
            away_team_odds.get("spreadOdds") or _spread_american(current_away),
            "away_spread_american",
        ),
        home_moneyline_american=_required_float(home_team_odds.get("moneyLine"), "home_moneyline_american"),
        away_moneyline_american=_required_float(away_team_odds.get("moneyLine"), "away_moneyline_american"),
        over_under=_optional_float(item.get("overUnder")),
    )


def parse_kalshi_spread_market(
    row: Mapping[str, object],
    *,
    game: NbaGameState,
    received_at: datetime,
    orderbook: Mapping[str, object] | None = None,
) -> NbaSpreadMarketQuote | None:
    """Parse a Kalshi NBA alt-spread row, returning None when the title is not a spread."""

    require_aware_datetime(received_at, "received_at")
    title = str(row.get("yes_sub_title") or row.get("title") or "")
    match = _TEAM_RE.search(title)
    if match is None:
        return None
    team_phrase = match.group("team").strip()
    threshold = float(match.group("threshold"))
    team_role = _team_role(team_phrase, game)
    if team_role is None:
        return None
    yes_bid = _required_float(row.get("yes_bid_dollars"), "yes_bid_dollars")
    yes_ask = _required_float(row.get("yes_ask_dollars"), "yes_ask_dollars")
    quote = NbaSpreadMarketQuote(
        ticker=str(row.get("ticker") or ""),
        title=title,
        team_role=team_role,
        team_phrase=team_phrase,
        threshold=threshold,
        received_at=received_at,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=_optional_float(row.get("no_bid_dollars")),
        no_ask=_optional_float(row.get("no_ask_dollars")),
        status=str(row.get("status") or "") or None,
        close_time=str(row.get("close_time") or "") or None,
        expected_expiration_time=str(row.get("expected_expiration_time") or "") or None,
    )
    if orderbook is None:
        return quote
    return merge_orderbook_touch(quote, orderbook)


def merge_orderbook_touch(
    quote: NbaSpreadMarketQuote,
    orderbook_payload: Mapping[str, object],
) -> NbaSpreadMarketQuote:
    """Replace top-of-book prices/sizes using a public Kalshi orderbook payload."""

    book = _mapping(
        orderbook_payload.get("orderbook_fp") or orderbook_payload.get("orderbook") or orderbook_payload,
        "orderbook",
    )
    yes_bid, yes_bid_size = _best_bid(_optional_sequence(book.get("yes_dollars") or book.get("yes")))
    no_bid, no_bid_size = _best_bid(_optional_sequence(book.get("no_dollars") or book.get("no")))
    if yes_bid is None and no_bid is None:
        return quote
    new_yes_bid = quote.yes_bid if yes_bid is None else yes_bid
    new_no_bid = quote.no_bid if no_bid is None else no_bid
    new_yes_ask = quote.yes_ask if no_bid is None else 1.0 - no_bid
    new_no_ask = quote.no_ask if yes_bid is None else 1.0 - yes_bid
    return replace(
        quote,
        yes_bid=new_yes_bid,
        yes_ask=new_yes_ask,
        no_bid=new_no_bid,
        no_ask=new_no_ask,
        yes_bid_size=yes_bid_size,
        yes_ask_size=no_bid_size,
        no_bid_size=no_bid_size,
        no_ask_size=yes_bid_size,
    )


def evaluate_spread_ladder(
    *,
    game: NbaGameState,
    anchor: NbaLiveOddsAnchor,
    markets: Sequence[NbaSpreadMarketQuote],
    config: NbaSpreadValidationConfig | None = None,
    as_of: datetime | None = None,
) -> NbaSpreadValidationReport:
    """Evaluate a Kalshi NBA spread ladder against the live odds anchor."""

    active_config = config or NbaSpreadValidationConfig()
    now = as_of or datetime.now(UTC)
    require_aware_datetime(now, "as_of")
    distribution = fit_margin_distribution(anchor)
    source_age = (
        None
        if "no_odds_last_modified" in anchor.timestamp_basis
        else (now - anchor.as_of).total_seconds()
    )
    block_reason = _reference_consistency_block_reason(game, anchor, active_config)
    valuations: list[NbaSpreadValuation] = []
    decisions: list[NbaSpreadDecision] = []
    for quote in markets:
        valuation = value_spread_market(quote, anchor=anchor, distribution=distribution)
        valuations.append(valuation)
        decisions.append(
            decide_spread_market(
                quote,
                valuation,
                source_age_seconds=source_age,
                source_timestamp_basis=anchor.timestamp_basis,
                config=active_config,
                block_reason=block_reason,
            )
        )
    candidates = [row for row in decisions if row.candidate]
    if candidates:
        report_decision = "paper_candidate:live_spread_reference_edge_needs_markout_and_settlement"
    else:
        report_decision = "kill_or_defer:no_fee_net_executable_candidate"
    caveat = (
        "Research/paper only. ESPN/DraftKings odds are used as a sharp reference, "
        "but the public odds payload may not carry an upstream update timestamp; "
        "positive rows require quote persistence, CLV, settlement, and fill evidence."
    )
    return NbaSpreadValidationReport(
        as_of=now,
        game=game,
        anchor=anchor,
        distribution=distribution,
        markets=tuple(markets),
        valuations=tuple(valuations),
        decisions=tuple(decisions),
        config=active_config,
        decision=report_decision,
        caveat=caveat,
    )


def value_spread_market(
    quote: NbaSpreadMarketQuote,
    *,
    anchor: NbaLiveOddsAnchor,
    distribution: NbaMarginDistribution,
) -> NbaSpreadValuation:
    """Compute fair YES probability for one Kalshi spread quote."""

    method = distribution.method
    if quote.team_role == "home":
        fair = distribution.probability_home_margin_gt(quote.threshold)
        if abs(quote.threshold - anchor.home_cover_threshold) < 1e-9:
            fair = anchor.home_cover_probability
            method = "direct_live_spread_anchor"
    else:
        fair = distribution.probability_home_margin_lt(-quote.threshold)
    features = {
        "threshold": quote.threshold,
        "team_role": quote.team_role,
        "provider": anchor.provider,
        "home_point_spread": anchor.home_point_spread,
        "home_cover_probability": anchor.home_cover_probability,
        "home_win_probability": anchor.home_win_probability,
        "mean_home_margin": distribution.mean_home_margin,
        "sigma_home_margin": distribution.sigma_home_margin,
    }
    return NbaSpreadValuation(
        ticker=quote.ticker,
        as_of=anchor.as_of,
        fair_yes=fair,
        valuation_method=method,
        threshold=quote.threshold,
        team_role=quote.team_role,
        feature_payload=features,
    )


def decide_spread_market(
    quote: NbaSpreadMarketQuote,
    valuation: NbaSpreadValuation,
    *,
    source_age_seconds: float | None,
    source_timestamp_basis: str,
    config: NbaSpreadValidationConfig,
    block_reason: str | None = None,
) -> NbaSpreadDecision:
    """Apply fee, freshness, and displayed-size gates."""

    edge = net_edge(
        valuation.fair_yes,
        yes_bid=quote.yes_bid,
        yes_ask=quote.yes_ask,
        fee_coeff=config.fee_coeff,
        slippage=config.slippage,
        min_edge=0.0,
    )
    if edge.executable_price is None or edge.net_edge is None:
        return _decision_none(
            quote,
            valuation,
            source_age_seconds=source_age_seconds,
            source_timestamp_basis=source_timestamp_basis,
            reason="net_edge_below_zero",
        )
    fair_side = valuation.fair_yes if edge.side == "YES" else 1.0 - valuation.fair_yes
    raw_edge = fair_side - edge.executable_price
    size = quote.side_size(edge.side)
    size_ok = size is None or size >= config.min_executable_size
    source_timestamp_missing = source_age_seconds is None or _proxy_timestamp_basis(source_timestamp_basis)
    source_timestamp_ok = not config.require_source_timestamp or not source_timestamp_missing
    source_age_ok = source_age_seconds is None or source_age_seconds <= config.max_source_age_seconds
    candidate = (
        edge.net_edge >= config.min_net_edge
        and size_ok
        and source_timestamp_ok
        and source_age_ok
        and block_reason is None
    )
    if candidate:
        reason = "paper_candidate_requires_clv_settlement_and_fill_proof"
    elif block_reason is not None:
        reason = block_reason
    elif not source_timestamp_ok:
        reason = "source_timestamp_missing"
    elif not source_age_ok:
        reason = "source_stale"
    elif not size_ok:
        reason = "insufficient_displayed_size"
    else:
        reason = "net_edge_below_threshold"
    expected_profit = (
        edge.net_edge * min(float(config.paper_contracts), size)
        if size is not None
        else edge.net_edge * config.paper_contracts
    )
    return NbaSpreadDecision(
        ticker=quote.ticker,
        as_of=valuation.as_of,
        side=edge.side,
        fair_yes=valuation.fair_yes,
        executable_price=edge.executable_price,
        raw_edge=raw_edge,
        fee=edge.fee,
        net_edge=edge.net_edge,
        executable_size=size,
        expected_profit_dollars=expected_profit,
        candidate=candidate,
        reason=reason,
        valuation_method=valuation.valuation_method,
        source_age_seconds=source_age_seconds,
        source_timestamp_basis=source_timestamp_basis,
    )


def _proxy_timestamp_basis(value: str) -> bool:
    basis = value.lower()
    return "received_at" in basis or "no_odds_last_modified" in basis or "proxy" in basis


def render_markdown(report: NbaSpreadValidationReport) -> str:
    """Render a compact validation report."""

    data = report.as_dict()
    summary = _mapping(data["summary"], "summary")
    candidates = [row for row in report.decisions if row.candidate]
    best_rows = sorted(
        (row for row in report.decisions if row.net_edge is not None),
        key=lambda row: row.net_edge or -999.0,
        reverse=True,
    )[:12]
    candidate_profit = _required_float(
        summary.get("candidate_expected_profit_dollars"),
        "candidate_expected_profit_dollars",
    )
    lines = [
        "# Spread sharp-reference validation",
        "",
        f"- Generated: {report.as_of.isoformat()}",
        f"- Game: {report.game.name} ({report.game.away_score}-{report.game.home_score}, {report.game.status_detail})",
        f"- Anchor: {report.anchor.provider} home spread {report.anchor.home_point_spread:+.1f}, "
        f"home ML {report.anchor.home_moneyline_american:+.0f}, away ML {report.anchor.away_moneyline_american:+.0f}",
        f"- Distribution: mean home margin {report.distribution.mean_home_margin:+.2f}, "
        f"sigma {report.distribution.sigma_home_margin:.2f}",
        f"- Markets checked: {summary.get('markets')}",
        f"- Candidates: {summary.get('candidates')}",
        f"- Candidate expected profit at {report.config.paper_contracts} contracts each: "
        f"{candidate_profit:+.4f}",
        f"- Decision: {summary.get('decision')}",
        "",
        "## Top fee-net rows",
        "",
        "| ticker | side | fair_yes | px | fee | net | size | method | reason |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for row in best_rows:
        lines.append(
            "| "
            f"{row.ticker} | {row.side} | {row.fair_yes:.4f} | "
            f"{_fmt(row.executable_price)} | {_fmt(row.fee)} | {_fmt(row.net_edge)} | "
            f"{_fmt(row.executable_size, digits=2)} | {row.valuation_method} | {row.reason} |"
        )
    if not best_rows:
        lines.append("| _none_ |  |  |  |  |  |  |  |  |")
    lines.extend(
        [
            "",
            "## Candidate signals",
            "",
            "| ticker | side | fair_yes | net | expected_profit | timestamp_basis |",
            "| --- | --- | ---: | ---: | ---: | --- |",
        ]
    )
    for row in candidates:
        lines.append(
            "| "
            f"{row.ticker} | {row.side} | {row.fair_yes:.4f} | {_fmt(row.net_edge)} | "
            f"{_fmt(row.expected_profit_dollars)} | {row.source_timestamp_basis} |"
        )
    if not candidates:
        lines.append("| _none_ |  |  |  |  |  |")
    lines.extend(["", "## Caveat", "", report.caveat, ""])
    return "\n".join(lines)


def markout_report_from_entry_report(
    entry_report: Mapping[str, object],
    *,
    current_quotes: Mapping[str, NbaSpreadMarketQuote],
    as_of: datetime,
    entry_report_name: str,
) -> NbaSpreadMarkoutReport:
    """Mark candidate decisions to current displayed bids after entry fee."""

    require_aware_datetime(as_of, "as_of")
    summary = _mapping(entry_report.get("summary"), "summary")
    paper_contracts = int(_required_float(summary.get("paper_contracts"), "paper_contracts"))
    rows: list[NbaSpreadMarkoutRow] = []
    for raw in _sequence(entry_report.get("decisions"), "decisions"):
        decision = _mapping(raw, "decision")
        if not decision.get("candidate"):
            continue
        ticker = str(decision.get("ticker") or "")
        side = str(decision.get("side") or "")
        entry_price = _required_float(decision.get("executable_price"), "executable_price")
        entry_fee = _required_float(decision.get("fee"), "fee")
        entry_net_edge = _required_float(decision.get("net_edge"), "net_edge")
        quote = current_quotes.get(ticker)
        if quote is None:
            rows.append(
                NbaSpreadMarkoutRow(
                    ticker=ticker,
                    side=side,
                    entry_price=entry_price,
                    entry_fee=entry_fee,
                    entry_net_edge=entry_net_edge,
                    markout_bid=None,
                    markout_after_entry_fee=None,
                    bid_size=None,
                    positive=False,
                    reason="missing_current_quote",
                )
            )
            continue
        markout_bid = quote.yes_bid if side == "YES" else quote.no_bid
        bid_size = quote.yes_bid_size if side == "YES" else quote.no_bid_size
        if markout_bid is None:
            rows.append(
                NbaSpreadMarkoutRow(
                    ticker=ticker,
                    side=side,
                    entry_price=entry_price,
                    entry_fee=entry_fee,
                    entry_net_edge=entry_net_edge,
                    markout_bid=None,
                    markout_after_entry_fee=None,
                    bid_size=bid_size,
                    positive=False,
                    reason="missing_current_side_bid",
                )
            )
            continue
        markout = markout_bid - entry_price - entry_fee
        rows.append(
            NbaSpreadMarkoutRow(
                ticker=ticker,
                side=side,
                entry_price=entry_price,
                entry_fee=entry_fee,
                entry_net_edge=entry_net_edge,
                markout_bid=markout_bid,
                markout_after_entry_fee=markout,
                bid_size=bid_size,
                positive=markout > 0.0,
                reason="positive_markout" if markout > 0.0 else "negative_markout",
            )
        )
    positives = [row for row in rows if row.positive]
    valid_markouts = [
        float(row.markout_after_entry_fee)
        for row in rows
        if row.markout_after_entry_fee is not None
    ]
    mean_markout = sum(valid_markouts) / len(valid_markouts) if valid_markouts else None
    report_decision = (
        "continue_paper:positive_markout_needs_larger_sample"
        if positives and mean_markout is not None and mean_markout > 0.0
        else "kill_or_defer:short_markout_negative"
    )
    return NbaSpreadMarkoutReport(
        as_of=as_of,
        entry_report=entry_report_name,
        paper_contracts=paper_contracts,
        rows=tuple(rows),
        decision=report_decision,
    )


def render_markout_markdown(report: NbaSpreadMarkoutReport) -> str:
    """Render a compact markout report."""

    data = report.as_dict()
    summary = _mapping(data["summary"], "summary")
    rows = sorted(
        report.rows,
        key=lambda row: row.markout_after_entry_fee if row.markout_after_entry_fee is not None else -999.0,
        reverse=True,
    )
    lines = [
        "# Spread markout",
        "",
        f"- Generated: {report.as_of.isoformat()}",
        f"- Entry report: {report.entry_report}",
        f"- Entries: {summary.get('entries')}",
        f"- Markout rows: {summary.get('markout_rows')}",
        f"- Positive markouts: {summary.get('positive_markouts')}",
        f"- Mean markout after entry fee: {_fmt(_optional_float(summary.get('mean_markout_after_entry_fee')))}",
        f"- Total at {report.paper_contracts} contracts: "
        f"{_fmt(_optional_float(summary.get('total_markout_dollars')))}",
        f"- Decision: {summary.get('decision')}",
        "",
        "| ticker | side | entry | bid | fee | markout | size | reason |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in rows[:20]:
        lines.append(
            "| "
            f"{row.ticker} | {row.side} | {_fmt(row.entry_price)} | {_fmt(row.markout_bid)} | "
            f"{_fmt(row.entry_fee)} | {_fmt(row.markout_after_entry_fee)} | "
            f"{_fmt(row.bid_size, digits=2)} | {row.reason} |"
        )
    if not rows:
        lines.append("| _none_ |  |  |  |  |  |  |  |")
    lines.append("")
    return "\n".join(lines)


def settlement_report_from_entry_report(
    entry_report: Mapping[str, object],
    *,
    game: NbaGameState,
    as_of: datetime,
    entry_report_name: str,
) -> NbaSpreadSettlementReport:
    """Settle candidate entries once the ESPN game state is final."""

    require_aware_datetime(as_of, "as_of")
    summary = _mapping(entry_report.get("summary"), "summary")
    paper_contracts = int(_required_float(summary.get("paper_contracts"), "paper_contracts"))
    valuation_by_ticker = {
        str(_mapping(raw, "valuation").get("ticker") or ""): _mapping(raw, "valuation")
        for raw in _sequence(entry_report.get("valuations"), "valuations")
    }
    rows: list[NbaSpreadSettlementRow] = []
    for raw in _sequence(entry_report.get("decisions"), "decisions"):
        decision = _mapping(raw, "decision")
        if not decision.get("candidate"):
            continue
        ticker = str(decision.get("ticker") or "")
        valuation = valuation_by_ticker.get(ticker)
        if valuation is None:
            continue
        side = str(decision.get("side") or "")
        threshold = _required_float(valuation.get("threshold"), "threshold")
        team_role = str(valuation.get("team_role") or "")
        entry_price = _required_float(decision.get("executable_price"), "executable_price")
        entry_fee = _required_float(decision.get("fee"), "fee")
        if not game.completed:
            rows.append(
                NbaSpreadSettlementRow(
                    ticker=ticker,
                    side=side,
                    threshold=threshold,
                    team_role=team_role,
                    entry_price=entry_price,
                    entry_fee=entry_fee,
                    yes_settled=None,
                    payout=None,
                    pnl_after_entry_fee=None,
                    reason="game_not_completed",
                )
            )
            continue
        yes_settled = _settled_yes(game.home_margin, team_role=team_role, threshold=threshold)
        payout = 1.0 if (yes_settled if side == "YES" else not yes_settled) else 0.0
        pnl = payout - entry_price - entry_fee
        rows.append(
            NbaSpreadSettlementRow(
                ticker=ticker,
                side=side,
                threshold=threshold,
                team_role=team_role,
                entry_price=entry_price,
                entry_fee=entry_fee,
                yes_settled=yes_settled,
                payout=payout,
                pnl_after_entry_fee=pnl,
                reason="settled",
            )
        )
    if not game.completed:
        report_decision = "pending:game_not_completed"
    else:
        values = [row.pnl_after_entry_fee for row in rows if row.pnl_after_entry_fee is not None]
        mean = sum(float(value) for value in values) / len(values) if values else 0.0
        report_decision = "paper_edge_supported:settlement_positive" if mean > 0.0 else "kill:settlement_negative"
    return NbaSpreadSettlementReport(
        as_of=as_of,
        entry_report=entry_report_name,
        game=game,
        paper_contracts=paper_contracts,
        rows=tuple(rows),
        decision=report_decision,
    )


def render_settlement_markdown(report: NbaSpreadSettlementReport) -> str:
    """Render a settlement report."""

    data = report.as_dict()
    summary = _mapping(data["summary"], "summary")
    rows = sorted(
        report.rows,
        key=lambda row: row.pnl_after_entry_fee if row.pnl_after_entry_fee is not None else -999.0,
        reverse=True,
    )
    lines = [
        "# Spread settlement",
        "",
        f"- Generated: {report.as_of.isoformat()}",
        f"- Entry report: {report.entry_report}",
        f"- Game: {report.game.name} ({report.game.away_score}-{report.game.home_score}, {report.game.status_detail})",
        f"- Entries: {summary.get('entries')}",
        f"- Settled rows: {summary.get('settled_rows')}",
        f"- Mean PnL after entry fee: {_fmt(_optional_float(summary.get('mean_pnl_after_entry_fee')))}",
        f"- Total at {report.paper_contracts} contracts: {_fmt(_optional_float(summary.get('total_pnl_dollars')))}",
        f"- Decision: {summary.get('decision')}",
        "",
        "| ticker | side | threshold | yes_settled | payout | pnl | reason |",
        "| --- | --- | ---: | --- | ---: | ---: | --- |",
    ]
    for row in rows[:30]:
        lines.append(
            "| "
            f"{row.ticker} | {row.side} | {row.threshold:.1f} | {row.yes_settled} | "
            f"{_fmt(row.payout)} | {_fmt(row.pnl_after_entry_fee)} | {row.reason} |"
        )
    if not rows:
        lines.append("| _none_ |  |  |  |  |  |  |")
    lines.append("")
    return "\n".join(lines)


def write_report_outputs(
    report: NbaSpreadValidationReport,
    *,
    report_json: Path,
    report_md: Path,
    signals_jsonl: Path,
) -> None:
    """Write JSON/Markdown report and ExternalSignal-shaped candidates."""

    report_json.parent.mkdir(parents=True, exist_ok=True)
    report_json.write_text(json.dumps(to_jsonable(report.as_dict()), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report_md.parent.mkdir(parents=True, exist_ok=True)
    report_md.write_text(render_markdown(report), encoding="utf-8")
    write_jsonl(signals_jsonl, [row.as_signal_payload() for row in report.decisions if row.candidate])


def write_markout_outputs(
    report: NbaSpreadMarkoutReport,
    *,
    report_json: Path,
    report_md: Path,
) -> None:
    """Write markout JSON and Markdown reports."""

    report_json.parent.mkdir(parents=True, exist_ok=True)
    report_json.write_text(json.dumps(to_jsonable(report.as_dict()), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report_md.parent.mkdir(parents=True, exist_ok=True)
    report_md.write_text(render_markout_markdown(report), encoding="utf-8")


def write_settlement_outputs(
    report: NbaSpreadSettlementReport,
    *,
    report_json: Path,
    report_md: Path,
) -> None:
    """Write settlement JSON and Markdown reports."""

    report_json.parent.mkdir(parents=True, exist_ok=True)
    report_json.write_text(json.dumps(to_jsonable(report.as_dict()), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report_md.parent.mkdir(parents=True, exist_ok=True)
    report_md.write_text(render_settlement_markdown(report), encoding="utf-8")


def fixture_game_state() -> NbaGameState:
    now = datetime(2026, 6, 4, 3, 55, tzinfo=UTC)
    return NbaGameState(
        event_id="401859963",
        name="New York Knicks at San Antonio Spurs",
        received_at=now,
        home_team="San Antonio Spurs",
        away_team="New York Knicks",
        home_abbrev="SA",
        away_abbrev="NY",
        home_score=27,
        away_score=19,
        status_state="in",
        status_detail="End of 1st",
        period=1,
        completed=False,
        scoreboard_home_win_probability=None,
    )


def fixture_anchor() -> NbaLiveOddsAnchor:
    return NbaLiveOddsAnchor(
        provider="DraftKings - Live Odds",
        as_of=datetime(2026, 6, 4, 3, 55, tzinfo=UTC),
        timestamp_basis="fixture",
        home_point_spread=-8.5,
        home_spread_american=-120.0,
        away_spread_american=-110.0,
        home_moneyline_american=-375.0,
        away_moneyline_american=270.0,
        over_under=205.5,
    )


def fixture_markets() -> tuple[NbaSpreadMarketQuote, ...]:
    now = datetime(2026, 6, 4, 3, 55, tzinfo=UTC)
    return (
        NbaSpreadMarketQuote(
            ticker="KXNBASPREAD-FIXTURE-SAS8",
            title="San Antonio wins by over 8.5 points",
            team_role="home",
            team_phrase="San Antonio",
            threshold=8.5,
            received_at=now,
            yes_bid=0.55,
            yes_ask=0.56,
            no_bid=0.44,
            no_ask=0.45,
            yes_bid_size=25.0,
            yes_ask_size=25.0,
            no_bid_size=25.0,
            no_ask_size=25.0,
            status="active",
        ),
        NbaSpreadMarketQuote(
            ticker="KXNBASPREAD-FIXTURE-SAS15",
            title="San Antonio wins by over 15.5 points",
            team_role="home",
            team_phrase="San Antonio",
            threshold=15.5,
            received_at=now,
            yes_bid=0.34,
            yes_ask=0.36,
            no_bid=0.64,
            no_ask=0.66,
            yes_bid_size=25.0,
            yes_ask_size=25.0,
            no_bid_size=25.0,
            no_ask_size=25.0,
            status="active",
        ),
        NbaSpreadMarketQuote(
            ticker="KXNBASPREAD-FIXTURE-NYK8",
            title="New York wins by over 8.5 points",
            team_role="away",
            team_phrase="New York",
            threshold=8.5,
            received_at=now,
            yes_bid=0.07,
            yes_ask=0.09,
            no_bid=0.91,
            no_ask=0.93,
            yes_bid_size=25.0,
            yes_ask_size=25.0,
            no_bid_size=25.0,
            no_ask_size=25.0,
            status="active",
        ),
    )


def _decision_none(
    quote: NbaSpreadMarketQuote,
    valuation: NbaSpreadValuation,
    *,
    source_age_seconds: float | None,
    source_timestamp_basis: str,
    reason: str,
) -> NbaSpreadDecision:
    return NbaSpreadDecision(
        ticker=quote.ticker,
        as_of=valuation.as_of,
        side="NONE",
        fair_yes=valuation.fair_yes,
        executable_price=None,
        raw_edge=None,
        fee=None,
        net_edge=None,
        executable_size=None,
        expected_profit_dollars=None,
        candidate=False,
        reason=reason,
        valuation_method=valuation.valuation_method,
        source_age_seconds=source_age_seconds,
        source_timestamp_basis=source_timestamp_basis,
    )


def _team_role(team_phrase: str, game: NbaGameState) -> str | None:
    team_key = _normalize_text(team_phrase)
    home_key = _normalize_text(game.home_team)
    away_key = _normalize_text(game.away_team)
    if home_key.startswith(team_key) or team_key in home_key:
        return "home"
    if away_key.startswith(team_key) or team_key in away_key:
        return "away"
    return None


def _reference_consistency_block_reason(
    game: NbaGameState,
    anchor: NbaLiveOddsAnchor,
    config: NbaSpreadValidationConfig,
) -> str | None:
    limit = config.max_scoreboard_win_probability_disagreement
    if limit is None or game.scoreboard_home_win_probability is None:
        return None
    disagreement = abs(anchor.home_win_probability - game.scoreboard_home_win_probability)
    if disagreement > limit:
        return "reference_scoreboard_win_probability_disagreement"
    return None


def _scoreboard_home_win_probability(competition: Mapping[str, object]) -> float | None:
    situation = competition.get("situation")
    if not isinstance(situation, Mapping):
        return None
    last_play = situation.get("lastPlay")
    if not isinstance(last_play, Mapping):
        return None
    probability = last_play.get("probability")
    if not isinstance(probability, Mapping):
        return None
    return _optional_float(probability.get("homeWinPercentage"))


def _settled_yes(home_margin: int, *, team_role: str, threshold: float) -> bool:
    if team_role == "home":
        return home_margin > threshold
    if team_role == "away":
        return home_margin < -threshold
    raise ValueError("team_role must be home or away")


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", value.lower())).strip()


def _best_bid(levels: Sequence[object]) -> tuple[float | None, float | None]:
    best_price: float | None = None
    best_size: float | None = None
    for raw in levels:
        if not isinstance(raw, Sequence) or len(raw) < 2 or isinstance(raw, str):
            continue
        price = _optional_float(raw[0])
        size = _optional_float(raw[1])
        if price is None:
            continue
        if best_price is None or price > best_price:
            best_price = price
            best_size = size
    return best_price, best_size


def _point_spread_value(point_spread: object, fallback: object) -> float:
    if isinstance(point_spread, Mapping):
        value = point_spread.get("american") or point_spread.get("alternateDisplayValue")
        parsed = _optional_float(value)
        if parsed is not None:
            return parsed
    return _required_float(fallback, "spread")


def _spread_american(value: object) -> float:
    if isinstance(value, Mapping):
        spread = _mapping(value.get("spread"), "spread")
        parsed = _optional_float(spread.get("american") or spread.get("alternateDisplayValue"))
        if parsed is not None:
            return parsed
    raise ValueError("missing spread american odds")


def _competitor_by_home_away(competitors: Sequence[Mapping[str, object]], home_away: str) -> Mapping[str, object]:
    for competitor in competitors:
        if competitor.get("homeAway") == home_away:
            return competitor
    raise ValueError(f"missing {home_away} competitor")


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return value
    raise ValueError(f"{name} must be an object")


def _sequence(value: object, name: str) -> Sequence[object]:
    if isinstance(value, Sequence) and not isinstance(value, str):
        return value
    raise ValueError(f"{name} must be a list")


def _optional_sequence(value: object) -> Sequence[object]:
    if isinstance(value, Sequence) and not isinstance(value, str):
        return value
    return ()


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _required_float(value: object, name: str) -> float:
    parsed = _optional_float(value)
    if parsed is None:
        raise ValueError(f"{name} must be a finite number")
    return parsed


def _require_probability(value: float, name: str) -> None:
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be finite and in [0, 1]")


def _require_positive(value: float, name: str) -> None:
    if not math.isfinite(value) or value <= 0.0:
        raise ValueError(f"{name} must be positive")


def _clip_probability(value: float) -> float:
    return min(1.0, max(0.0, value))


def _clip_for_inverse(value: float) -> float:
    return min(1.0 - _EPS, max(_EPS, value))


def _fmt(value: float | None, *, digits: int = 4) -> str:
    if value is None:
        return ""
    return f"{value:.{digits}f}"


def _to_jsonable_dataclass(value: object) -> object:
    return to_jsonable(value)
