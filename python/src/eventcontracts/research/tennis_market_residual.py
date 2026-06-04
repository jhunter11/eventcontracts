"""Independent tennis market-residual research harness.

This module is a *deliberately separate* validator from the production tennis
path (`research/tennis_v2.py`, `plugins/strategies/sports_tennis_xgboost.py`).
It was built from scratch to answer the only question the live path cares about:

    Can a PRE-MATCH win-probability model beat the de-vigged closing line by
    enough to clear Kalshi fees + spread at an executable price?

The blind research that produced it (see
``live-test/tennis-independent-research-findings.md``) concluded *no* for the
ATP match-winner market: the closing line is near-efficient and engineered
features carry no exploitable residual beyond the price. So this module is not a
trading model — it is the reusable **edge gate** that future tennis ideas must
clear, encoding that finding as tested, executable knowledge.

Design constraints (match AGENTS.md + repo conventions):
  * No network, no orders, no live-path coupling. Pure functions + numpy.
  * The executable-edge gate uses the canonical Kalshi taker-fee curve
    (``risk/limits.py::_kalshi_fee_per_contract`` / ``rust/crates/risk/fees.rs``),
    Decimal-exact, including the per-contract cent ceiling.
  * Strict chronological evaluation; every feature is causal (state strictly
    before the match it describes).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_CEILING, Decimal

import numpy as np

__all__ = [
    "MatchRecord",
    "PrematchAssessment",
    "EvalMetrics",
    "WalkForwardResult",
    "devig_two_way",
    "kalshi_taker_fee_per_contract",
    "EloBook",
    "MarketResidualEvaluator",
    "executable_edge_gate",
    "assess_prematch",
    "synthetic_matches",
]


# --------------------------------------------------------------------------- #
# Core market math
# --------------------------------------------------------------------------- #
def devig_two_way(odds_a: float, odds_b: float) -> tuple[float, float]:
    """De-vig a two-way decimal-odds market.

    Returns ``(prob_a, overround)`` using the multiplicative (proportional)
    method. ``overround`` is the booked sum of raw implied probabilities
    (``>= 1`` for a real market with margin). Returns ``(nan, nan)`` if either
    price is missing or non-positive.
    """
    if not (math.isfinite(odds_a) and math.isfinite(odds_b)):
        return math.nan, math.nan
    if odds_a <= 1.0 or odds_b <= 1.0:
        return math.nan, math.nan
    ra, rb = 1.0 / odds_a, 1.0 / odds_b
    s = ra + rb
    if s <= 0:
        return math.nan, math.nan
    return ra / s, s


def kalshi_taker_fee_per_contract(
    price: float, quantity: int = 1, rate_bps: int = 700
) -> float:
    """Canonical Kalshi taker fee per contract, in dollars.

    Mirrors ``risk/limits.py::_kalshi_fee_per_contract`` and the Rust
    ``kalshi_taker_fee_ticks`` curve: ``ceil_to_cent(rate * P * (1-P) * qty) /
    qty`` with ``rate = rate_bps / 10_000``. The cent ceiling is applied to the
    *total* (so a single contract pays at least 1c whenever the raw fee is
    positive) which bites hardest at extreme prices — exactly where any
    favorite-longshot edge lives.
    """
    if quantity <= 0 or rate_bps <= 0:
        return 0.0
    if not math.isfinite(price) or price <= 0.0 or price >= 1.0:
        return 0.0
    rate = Decimal(rate_bps) / Decimal("10000")
    p = Decimal(repr(price))
    raw = rate * p * (Decimal("1") - p) * Decimal(quantity)
    total = raw.quantize(Decimal("0.01"), rounding=ROUND_CEILING)
    return float(total / Decimal(quantity))


# --------------------------------------------------------------------------- #
# Causal Elo
# --------------------------------------------------------------------------- #
class EloBook:
    """538-style decaying-K Elo, tracked overall and per surface.

    ``K_i = k_base / (n_i + k_shift) ** k_decay`` where ``n_i`` is the number of
    matches the player has already been rated on, so new players move fast and
    established players move slowly. Surface ratings update only on matches of
    that surface.
    """

    def __init__(
        self,
        *,
        init_rating: float = 1500.0,
        k_base: float = 250.0,
        k_shift: float = 5.0,
        k_decay: float = 0.4,
    ) -> None:
        self.init_rating = init_rating
        self.k_base = k_base
        self.k_shift = k_shift
        self.k_decay = k_decay
        self._overall: dict[str, float] = {}
        self._n: dict[str, int] = {}
        self._surf: dict[tuple[str, str], float] = {}
        self._surf_n: dict[tuple[str, str], int] = {}

    def _k(self, n: int) -> float:
        return self.k_base / ((n + self.k_shift) ** self.k_decay)

    @staticmethod
    def expected(rating_a: float, rating_b: float) -> float:
        """Elo expected score for A vs B (probability A wins)."""
        return 1.0 / (1.0 + 10.0 ** ((rating_b - rating_a) / 400.0))

    def rating(self, player: str, surface: str) -> tuple[float, float, int]:
        """Return ``(overall_rating, surface_rating, matches_played)``."""
        return (
            self._overall.get(player, self.init_rating),
            self._surf.get((player, surface), self.init_rating),
            self._n.get(player, 0),
        )

    def update(self, winner: str, loser: str, surface: str) -> None:
        """Apply one match result to overall and surface books."""
        rw = self._overall.get(winner, self.init_rating)
        rl = self._overall.get(loser, self.init_rating)
        ew = self.expected(rw, rl)
        self._overall[winner] = rw + self._k(self._n.get(winner, 0)) * (1.0 - ew)
        self._overall[loser] = rl + self._k(self._n.get(loser, 0)) * (ew - 1.0)
        self._n[winner] = self._n.get(winner, 0) + 1
        self._n[loser] = self._n.get(loser, 0) + 1

        kw, kl = (winner, surface), (loser, surface)
        rws = self._surf.get(kw, self.init_rating)
        rls = self._surf.get(kl, self.init_rating)
        ews = self.expected(rws, rls)
        self._surf[kw] = rws + self._k(self._surf_n.get(kw, 0)) * (1.0 - ews)
        self._surf[kl] = rls + self._k(self._surf_n.get(kl, 0)) * (ews - 1.0)
        self._surf_n[kw] = self._surf_n.get(kw, 0) + 1
        self._surf_n[kl] = self._surf_n.get(kl, 0) + 1


# --------------------------------------------------------------------------- #
# Records & payloads
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class MatchRecord:
    """One de-anonymized pre-match row.

    ``player1``/``player2`` are NOT ordered by outcome; ``p1_won`` is the label.
    Odds are decimal closing prices for player1 / player2 (optional).
    """

    match_date: date
    player1: str
    player2: str
    surface: str
    p1_won: bool
    best_of: int = 3
    level: str = ""
    odds_p1: float | None = None
    odds_p2: float | None = None
    odds_source: str | None = None


@dataclass(frozen=True)
class PrematchAssessment:
    """Runner-compatible payload concept for a single pre-match opportunity.

    Mirrors the fields a live sleeve cares about without touching the live path:
    a fair-value ``probability``, a ``confidence``, market metadata, and the
    fee-adjusted edge that gates whether this is even a candidate. It is
    explicit that an executable market snapshot is *required* before any of this
    becomes actionable.
    """

    probability: float
    confidence: float
    odds_present: bool
    odds_source: str | None
    market_implied_prob: float | None
    edge_vs_market: float | None
    fee_adjusted_edge: float | None
    recommend: str
    requires_market_snapshot: bool = True


@dataclass(frozen=True)
class EvalMetrics:
    n: int
    brier: float
    logloss: float
    ece: float


@dataclass
class WalkForwardResult:
    market: EvalMetrics
    anchored: EvalMetrics
    brier_skill_score: float
    beats_market: bool
    per_year: list[tuple[int, int, float, float]] = field(default_factory=list)
    # pooled out-of-sample arrays (for the executable gate)
    oos_market: np.ndarray = field(default_factory=lambda: np.array([]))
    oos_model: np.ndarray = field(default_factory=lambda: np.array([]))
    oos_y: np.ndarray = field(default_factory=lambda: np.array([]))


# --------------------------------------------------------------------------- #
# Metrics + a tiny numpy logistic regression (no sklearn dependency)
# --------------------------------------------------------------------------- #
def _brier(p: np.ndarray, y: np.ndarray) -> float:
    return float(np.mean((p - y) ** 2))


def _logloss(p: np.ndarray, y: np.ndarray) -> float:
    p = np.clip(p, 1e-9, 1 - 1e-9)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def _ece(p: np.ndarray, y: np.ndarray, bins: int = 10) -> float:
    edges = np.linspace(0.0, 1.0, bins + 1)
    n = len(p)
    if n == 0:
        return math.nan
    total = 0.0
    for i in range(bins):
        hi_inclusive = i == bins - 1
        m = (p >= edges[i]) & ((p <= edges[i + 1]) if hi_inclusive else (p < edges[i + 1]))
        if not m.any():
            continue
        total += m.sum() / n * abs(float(p[m].mean()) - float(y[m].mean()))
    return float(total)


def _logit(p: np.ndarray) -> np.ndarray:
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return np.log(p / (1 - p))


class _LogisticRegression:
    """L2-regularized logistic regression via gradient descent (numpy only)."""

    def __init__(self, l2: float = 1.0, lr: float = 0.5, n_iter: int = 600) -> None:
        self.l2 = l2
        self.lr = lr
        self.n_iter = n_iter
        self.w: np.ndarray | None = None
        self.b: float = 0.0

    def fit(self, X: np.ndarray, y: np.ndarray) -> _LogisticRegression:
        n, d = X.shape
        w = np.zeros(d)
        b = 0.0
        yf = y.astype(np.float64)
        for _ in range(self.n_iter):
            z = X @ w + b
            p = 1.0 / (1.0 + np.exp(-z))
            g = p - yf
            gw = X.T @ g / n + self.l2 * w / n
            gb = float(np.mean(g))
            w -= self.lr * gw
            b -= self.lr * gb
        self.w, self.b = w, b
        return self

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        assert self.w is not None, "model not fitted"
        z = X @ self.w + self.b
        return 1.0 / (1.0 + np.exp(-z))


# --------------------------------------------------------------------------- #
# Feature builder + walk-forward evaluator
# --------------------------------------------------------------------------- #
_FEATURES = ("elo_diff", "elo_surf_diff", "form_diff", "h2h_diff")


def _build_features(
    records: list[MatchRecord],
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Causal feature pass. Returns ``(X, y, market_p1, years)``.

    Every feature for a row uses only state accumulated from strictly earlier
    rows; Elo / form / H2H are updated *after* the row is recorded.
    """
    rows = sorted(records, key=lambda r: r.match_date)
    elo = EloBook()
    form: dict[str, list[int]] = {}
    h2h: dict[tuple[str, str], int] = {}

    X = np.full((len(rows), len(_FEATURES)), np.nan)
    y = np.zeros(len(rows), dtype=np.int64)
    market = np.full(len(rows), np.nan)
    years = np.zeros(len(rows), dtype=np.int64)

    for i, r in enumerate(rows):
        p1, p2 = r.player1, r.player2
        ov1, sf1, _n1 = elo.rating(p1, r.surface)
        ov2, sf2, _n2 = elo.rating(p2, r.surface)
        X[i, 0] = ov1 - ov2
        X[i, 1] = sf1 - sf2
        f1, f2 = form.get(p1, []), form.get(p2, [])
        wr1 = (sum(f1[-15:]) / len(f1[-15:])) if f1 else math.nan
        wr2 = (sum(f2[-15:]) / len(f2[-15:])) if f2 else math.nan
        X[i, 2] = (wr1 - wr2) if not (math.isnan(wr1) or math.isnan(wr2)) else math.nan
        X[i, 3] = h2h.get((p1, p2), 0) - h2h.get((p2, p1), 0)

        y[i] = 1 if r.p1_won else 0
        years[i] = r.match_date.year
        if r.odds_p1 is not None and r.odds_p2 is not None:
            pa, _over = devig_two_way(r.odds_p1, r.odds_p2)
            market[i] = pa

        # update causal state AFTER recording
        w, ln = (p1, p2) if r.p1_won else (p2, p1)
        elo.update(w, ln, r.surface)
        form.setdefault(p1, []).append(1 if r.p1_won else 0)
        form.setdefault(p2, []).append(0 if r.p1_won else 1)
        h2h[(w, ln)] = h2h.get((w, ln), 0) + 1

    return X, y, market, years


def _impute_standardize(
    Xtr: np.ndarray, Xte: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    med = np.nanmedian(Xtr, axis=0)
    med = np.where(np.isfinite(med), med, 0.0)
    Xtr = np.where(np.isfinite(Xtr), Xtr, med)
    Xte = np.where(np.isfinite(Xte), Xte, med)
    mu = Xtr.mean(axis=0)
    sd = Xtr.std(axis=0)
    sd = np.where(sd > 1e-9, sd, 1.0)
    return (Xtr - mu) / sd, (Xte - mu) / sd


class MarketResidualEvaluator:
    """Walk-forward test of an odds-anchored model vs the de-vigged line.

    The anchored model is a logistic regression on ``[logit(market), features]``
    — it *starts* at the price and can only help if the features carry residual
    signal the market missed. ``beats_market`` is the headline: if it is False
    (or the skill score is ~0), there is no pre-match modelling edge.
    """

    def __init__(self, *, min_train: int = 1500, start_eval_year: int | None = None) -> None:
        self.min_train = min_train
        self.start_eval_year = start_eval_year

    def walk_forward(self, records: list[MatchRecord]) -> WalkForwardResult:
        X, y, market, years = _build_features(records)
        usable = np.isfinite(market)
        uniq_years = sorted(set(int(v) for v in years[usable]))
        start = self.start_eval_year or (uniq_years[len(uniq_years) // 3] if uniq_years else 0)

        m_oos: list[np.ndarray] = []
        a_oos: list[np.ndarray] = []
        y_oos: list[np.ndarray] = []
        per_year: list[tuple[int, int, float, float]] = []

        for ty in uniq_years:
            if ty < start:
                continue
            tr = usable & (years < ty)
            te = usable & (years == ty)
            if tr.sum() < self.min_train or te.sum() < 20:
                continue
            Xtr, Xte = _impute_standardize(X[tr], X[te])
            mtr, mte = market[tr], market[te]
            Xa_tr = np.column_stack([_logit(mtr), Xtr])
            Xa_te = np.column_stack([_logit(mte), Xte])
            model = _LogisticRegression().fit(Xa_tr, y[tr])
            pa = model.predict_proba(Xa_te)
            m_oos.append(mte)
            a_oos.append(pa)
            y_oos.append(y[te].astype(float))
            per_year.append((ty, int(te.sum()), _brier(mte, y[te]), _brier(pa, y[te])))

        if not y_oos:
            empty = EvalMetrics(0, math.nan, math.nan, math.nan)
            return WalkForwardResult(empty, empty, math.nan, False)

        mp = np.concatenate(m_oos)
        ap = np.concatenate(a_oos)
        yy = np.concatenate(y_oos)
        market_m = EvalMetrics(len(yy), _brier(mp, yy), _logloss(mp, yy), _ece(mp, yy))
        anchored_m = EvalMetrics(len(yy), _brier(ap, yy), _logloss(ap, yy), _ece(ap, yy))
        bss = 1.0 - anchored_m.brier / market_m.brier if market_m.brier > 0 else math.nan
        return WalkForwardResult(
            market=market_m,
            anchored=anchored_m,
            brier_skill_score=bss,
            beats_market=bool(anchored_m.brier < market_m.brier),
            per_year=per_year,
            oos_market=mp,
            oos_model=ap,
            oos_y=yy,
        )


# --------------------------------------------------------------------------- #
# Executable-edge gate
# --------------------------------------------------------------------------- #
def executable_edge_gate(
    model_p: np.ndarray,
    market_p: np.ndarray,
    realized_y: np.ndarray,
    *,
    edge_threshold: float = 0.03,
    cross: float = 0.02,
    rate_bps: int = 700,
) -> dict[str, float]:
    """Realized EV of betting the model's disagreement with the market.

    For each match where ``|model_p - market_p| > edge_threshold`` we take the
    model-favored side, executing at ``market mid + cross`` (the taker crosses a
    half-spread), and pay the canonical Kalshi fee on the executed price. EV per
    contract uses the *actual* outcomes (honest backtest):
    ``net = realized_payoff - executed_price - fee``.

    Returns counts and per-contract EV. ``cross`` defaults to 2c (a 4c spread),
    a realistic stand-in for thin pre-match tennis books.
    """
    model_p = np.asarray(model_p, dtype=float)
    market_p = np.asarray(market_p, dtype=float)
    y = np.asarray(realized_y, dtype=float)
    bet_yes = (model_p - market_p) > edge_threshold
    bet_no = (market_p - model_p) > edge_threshold

    nets: list[float] = []
    for sel, is_yes in ((bet_yes, True), (bet_no, False)):
        if not sel.any():
            continue
        if is_yes:
            pe = np.clip(market_p[sel] + cross, 0.01, 0.99)
            payoff = y[sel]
        else:
            pe = np.clip((1.0 - market_p[sel]) + cross, 0.01, 0.99)
            payoff = 1.0 - y[sel]
        fee = np.array([kalshi_taker_fee_per_contract(float(p), 1, rate_bps) for p in pe])
        nets.extend((payoff - pe - fee).tolist())

    if not nets:
        return {"n_bets": 0, "ev_per_contract": 0.0, "total_ev": 0.0, "is_positive": 0.0}
    arr = np.array(nets)
    return {
        "n_bets": float(arr.size),
        "ev_per_contract": float(arr.mean()),
        "total_ev": float(arr.sum()),
        "is_positive": float(arr.mean() > 0.0),
    }


# --------------------------------------------------------------------------- #
# Pre-match assessment payload
# --------------------------------------------------------------------------- #
def assess_prematch(
    *,
    probability: float,
    market_implied_prob: float | None,
    odds_present: bool,
    odds_source: str | None,
    executable_price: float | None,
    side: str = "yes",
    rate_bps: int = 700,
) -> PrematchAssessment:
    """Build a runner-compatible :class:`PrematchAssessment`.

    ``recommend`` is ``"no_trade"`` unless odds are present and the edge against
    the market survives the canonical taker fee at the executable price. This is
    the gate, not a trade instruction — a market snapshot is still required.
    """
    confidence = abs(probability - 0.5) * 2.0
    edge_vs_market: float | None = None
    fee_adj: float | None = None
    recommend = "no_trade"

    if odds_present and market_implied_prob is not None:
        edge_vs_market = probability - market_implied_prob
        if executable_price is not None and 0.0 < executable_price < 1.0:
            fee = kalshi_taker_fee_per_contract(executable_price, 1, rate_bps)
            if side == "yes":
                # buy YES at executable_price, fair value = probability
                fee_adj = (probability - executable_price) - fee
            else:
                fee_adj = ((1.0 - probability) - executable_price) - fee
            if fee_adj > 0.0:
                recommend = "candidate"
    return PrematchAssessment(
        probability=probability,
        confidence=confidence,
        odds_present=odds_present,
        odds_source=odds_source,
        market_implied_prob=market_implied_prob,
        edge_vs_market=edge_vs_market,
        fee_adjusted_edge=fee_adj,
        recommend=recommend,
    )


# --------------------------------------------------------------------------- #
# No-network synthetic fixtures (for tests / demos)
# --------------------------------------------------------------------------- #
def synthetic_matches(
    *,
    n_matches: int = 4000,
    n_players: int = 80,
    market_calibration: float = 1.0,
    seed: int = 0,
    start_year: int = 2015,
    seasons: int = 6,
) -> list[MatchRecord]:
    """Generate deterministic synthetic matches for tests.

    Each player has a latent skill; the *true* win prob is
    ``sigmoid(skill_i - skill_j)``. The market prices
    ``sigmoid(market_calibration * (skill_i - skill_j))``:
      * ``market_calibration == 1.0`` -> efficient market (== truth): an
        outcome-derived model cannot beat it (skill score ~0 or negative).
      * ``market_calibration < 1.0``  -> underconfident market: a model that
        recovers skill from outcomes *can* beat it (skill score > 0).
    """
    rng = np.random.default_rng(seed)
    skill = rng.normal(0.0, 0.7, size=n_players)
    surfaces = ["Hard", "Clay", "Grass"]
    records: list[MatchRecord] = []
    for k in range(n_matches):
        i, j = rng.integers(0, n_players, size=2)
        while j == i:
            j = rng.integers(0, n_players)
        diff = float(skill[i] - skill[j])
        true_p = 1.0 / (1.0 + math.exp(-diff))
        mkt_p = 1.0 / (1.0 + math.exp(-market_calibration * diff))
        margin = 1.04  # 4% two-way overround
        odds_i = float(margin / max(mkt_p, 1e-3))
        odds_j = float(margin / max(1.0 - mkt_p, 1e-3))
        p1_won = bool(rng.random() < true_p)
        year = start_year + (k * seasons) // n_matches
        month = 1 + (k % 12)
        records.append(
            MatchRecord(
                match_date=date(year, month, 1 + (k % 27)),
                player1=f"P{i:03d}",
                player2=f"P{j:03d}",
                surface=surfaces[k % 3],
                p1_won=p1_won,
                best_of=3,
                level="ATP250",
                odds_p1=round(odds_i, 3),
                odds_p2=round(odds_j, 3),
                odds_source="synthetic",
            )
        )
    return records
