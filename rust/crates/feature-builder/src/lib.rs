//! Online feature-building boundary.
//!
//! Two layers live here:
//!
//! 1. The contract-shaped `FeatureBuilder` trait (used by the Python research
//!    pipeline and replay/parity tests).
//! 2. The hot-path `Scorer` trait — venue-agnostic, takes `&[f32]` feature
//!    vectors, returns `Vec<f32>` outputs. `eventcontracts-model-runtime`
//!    implements it on `Session`; tests implement it with a closure.
//!
//! Hot-path strategies extract a `[f32; N]` from a `HotQuote` (or another
//! `HotEvent`), feed it to a `Scorer`, and decide. The strategy never needs
//! to depend on `ort` — it's generic over `S: Scorer`.

use eventcontracts_runtime_hot::HotQuote;
use serde::{Deserialize, Serialize};
use std::collections::VecDeque;
use thiserror::Error;

// The FeatureBuilder trait (contract-shaped, with FeatureSchema and
// OnlineFeatureState) used to live here but had no Rust implementations.
// Python research owns that path; the live Rust hot-path uses the Scorer
// trait + quote_features_* below. Removed to keep the crate slim.

// ---------- hot-path scorer ----------

#[derive(Debug, Error)]
pub enum ScorerError {
    #[error("scorer expected input width {expected}, received {actual}")]
    InputWidth { expected: usize, actual: usize },
    #[error("invalid scorer output at index {index}: {value} ({reason})")]
    InvalidOutput {
        index: usize,
        value: f32,
        reason: &'static str,
    },
    #[error("scorer backend error: {0}")]
    Backend(String),
}

/// Venue-agnostic, model-agnostic scoring boundary used by hot-path
/// strategies. The model-runtime crate implements this for ONNX `Session`;
/// tests implement it inline with a closure.
pub trait Scorer: Send {
    /// Width of the feature vector the model was trained on.
    fn input_width(&self) -> usize;
    /// Score one feature row. Returned slice is the model's logits / probs /
    /// regressed value — the strategy decides what to do with it.
    fn predict(&self, features: &[f32]) -> Result<Vec<f32>, ScorerError>;
}

/// Number of features in [`quote_features`]. Fixed so a model trained
/// against this contract has a stable input width.
pub const QUOTE_FEATURE_WIDTH: usize = 4;
pub const ROLLING_QUOTE_FEATURE_WIDTH: usize = 8;

#[derive(Clone, Copy, Debug, Default)]
pub struct QuoteSample {
    pub bid: f32,
    pub ask: f32,
    pub spread: f32,
    pub mid: f32,
}

#[derive(Clone, Debug)]
pub struct RollingQuoteState {
    window: VecDeque<QuoteSample>,
    capacity: usize,
    /// Running sum of `sample.mid` for O(1) mean. Updated on push/pop.
    mid_sum: f32,
    /// Running sum of `sample.spread` for O(1) mean.
    spread_sum: f32,
}

impl Default for RollingQuoteState {
    fn default() -> Self {
        Self::new(64)
    }
}

impl RollingQuoteState {
    pub fn new(capacity: usize) -> Self {
        Self {
            window: VecDeque::with_capacity(capacity.max(1)),
            capacity: capacity.max(1),
            mid_sum: 0.0,
            spread_sum: 0.0,
        }
    }

    pub fn len(&self) -> usize {
        self.window.len()
    }

    pub fn is_empty(&self) -> bool {
        self.window.is_empty()
    }

    /// O(1) — maintain running sums for mean computation. Previously
    /// `iter().sum()` was called on every quote (64 adds per push at
    /// default capacity).
    pub fn push(&mut self, sample: QuoteSample) {
        if self.window.len() == self.capacity {
            if let Some(evicted) = self.window.pop_front() {
                self.mid_sum -= evicted.mid;
                self.spread_sum -= evicted.spread;
            }
        }
        self.mid_sum += sample.mid;
        self.spread_sum += sample.spread;
        self.window.push_back(sample);
    }

    pub fn mean_mid(&self) -> f32 {
        if self.window.is_empty() {
            0.0
        } else {
            self.mid_sum / self.window.len() as f32
        }
    }

    pub fn mean_spread(&self) -> f32 {
        if self.window.is_empty() {
            0.0
        } else {
            self.spread_sum / self.window.len() as f32
        }
    }
}

/// Stateless feature extractor: turns a `HotQuote` into a 4-vector of
/// `[bid_dollars, ask_dollars, spread_dollars, mid_dollars]`. Prices are
/// converted from the 4-decimal fixed-point representation back to f32
/// dollars exactly once, at the model boundary — strategies pass this
/// straight into `Scorer::predict`.
pub fn quote_features(quote: &HotQuote) -> [f32; QUOTE_FEATURE_WIDTH] {
    quote_features_raw(fixed_to_f32(quote.bid.raw()), fixed_to_f32(quote.ask.raw()))
}

/// Same as [`quote_features`] but takes pre-converted `f32` dollar prices.
/// Strategies that already hold a 6-decimal `runner::FixedPrice` (or any
/// other internal representation) can call this without re-allocating a
/// `HotQuote` per tick.
#[inline]
pub fn quote_features_raw(bid_dollars: f32, ask_dollars: f32) -> [f32; QUOTE_FEATURE_WIDTH] {
    let spread = ask_dollars - bid_dollars;
    let mid = (bid_dollars + ask_dollars) * 0.5;
    [bid_dollars, ask_dollars, spread, mid]
}

pub fn quote_features_rolling(
    state: &mut RollingQuoteState,
    quote: &HotQuote,
) -> [f32; ROLLING_QUOTE_FEATURE_WIDTH] {
    quote_features_rolling_raw(
        state,
        fixed_to_f32(quote.bid.raw()),
        fixed_to_f32(quote.ask.raw()),
    )
}

pub fn quote_features_rolling_raw(
    state: &mut RollingQuoteState,
    bid_dollars: f32,
    ask_dollars: f32,
) -> [f32; ROLLING_QUOTE_FEATURE_WIDTH] {
    let [bid, ask, spread, mid] = quote_features_raw(bid_dollars, ask_dollars);
    let prev = state.window.back().copied();
    let prev_mid = prev.map(|p| p.mid).unwrap_or(mid);
    let prev_spread = prev.map(|p| p.spread).unwrap_or(spread);
    state.push(QuoteSample {
        bid,
        ask,
        spread,
        mid,
    });
    // O(1) mean lookups (F7): running sums maintained on push/pop.
    [
        bid,
        ask,
        spread,
        mid,
        mid - prev_mid,
        spread - prev_spread,
        state.mean_mid(),
        state.mean_spread(),
    ]
}

#[inline]
fn fixed_to_f32(raw: i64) -> f32 {
    // 10_000 ticks per dollar. f32 is plenty for the 0..1 probability range
    // every binary event-contract price lives in.
    (raw as f64 / 10_000.0) as f32
}

/// Feature order shared with `contracts/examples/tennis_xgboost/feature_schema.json`.
pub const TENNIS_XGBOOST_FEATURE_NAMES: [&str; 20] = [
    "elo_diff",
    "surface_elo_diff",
    "rank_log_advantage",
    "seed_advantage",
    "age_diff",
    "height_cm_diff",
    "h2h_win_pct_diff",
    "recent_win_pct_diff",
    "recent_match_count_diff",
    "days_rest_diff",
    "best_of_5",
    "is_grand_slam",
    "p1_implied_prob",
    "implied_prob_diff",
    "odds_overround",
    "surface_hard",
    "surface_clay",
    "surface_grass",
    "surface_carpet",
    "surface_unknown",
];

/// Pre-match state input to the deployed tennis ONNX model.
///
/// Stateful event processing is kept outside this value: a live adapter
/// computes Elo, H2H, recent form, and rest using prior completed matches,
/// then passes this snapshot to the pure vector function below.
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(default)]
pub struct TennisMatchSnapshot {
    pub surface: String,
    pub tourney_level: String,
    pub best_of: i32,
    pub p1_elo: f64,
    pub p2_elo: f64,
    pub p1_surface_elo: f64,
    pub p2_surface_elo: f64,
    pub p1_rank: Option<i32>,
    pub p2_rank: Option<i32>,
    pub p1_seed: Option<i32>,
    pub p2_seed: Option<i32>,
    pub p1_age: Option<f64>,
    pub p2_age: Option<f64>,
    pub p1_height_cm: Option<f64>,
    pub p2_height_cm: Option<f64>,
    pub p1_h2h_wins: i32,
    pub p2_h2h_wins: i32,
    pub p1_recent_wins: i32,
    pub p2_recent_wins: i32,
    pub p1_recent_matches: i32,
    pub p2_recent_matches: i32,
    pub p1_days_since_match: Option<i32>,
    pub p2_days_since_match: Option<i32>,
    pub p1_decimal_odds: Option<f64>,
    pub p2_decimal_odds: Option<f64>,
}

impl Default for TennisMatchSnapshot {
    fn default() -> Self {
        Self {
            surface: "Unknown".into(),
            tourney_level: String::new(),
            best_of: 3,
            p1_elo: 1500.0,
            p2_elo: 1500.0,
            p1_surface_elo: 1500.0,
            p2_surface_elo: 1500.0,
            p1_rank: None,
            p2_rank: None,
            p1_seed: None,
            p2_seed: None,
            p1_age: None,
            p2_age: None,
            p1_height_cm: None,
            p2_height_cm: None,
            p1_h2h_wins: 0,
            p2_h2h_wins: 0,
            p1_recent_wins: 0,
            p2_recent_wins: 0,
            p1_recent_matches: 0,
            p2_recent_matches: 0,
            p1_days_since_match: None,
            p2_days_since_match: None,
            p1_decimal_odds: None,
            p2_decimal_odds: None,
        }
    }
}

/// Produce exactly the float32 tensor row expected by `model.onnx`.
pub fn tennis_xgboost_feature_vector(snapshot: &TennisMatchSnapshot) -> [f32; 20] {
    let (p1_probability, p2_probability, overround) =
        normalized_implied_probabilities(snapshot.p1_decimal_odds, snapshot.p2_decimal_odds);
    let surface = snapshot.surface.trim().to_ascii_lowercase();
    let h2h_total = snapshot.p1_h2h_wins + snapshot.p2_h2h_wins;
    let h2h_p1 = (f64::from(snapshot.p1_h2h_wins) + 0.5) / (f64::from(h2h_total) + 1.0);
    let p1_recent = smoothed_win_pct(snapshot.p1_recent_wins, snapshot.p1_recent_matches);
    let p2_recent = smoothed_win_pct(snapshot.p2_recent_wins, snapshot.p2_recent_matches);
    [
        (snapshot.p1_elo - snapshot.p2_elo) as f32,
        (snapshot.p1_surface_elo - snapshot.p2_surface_elo) as f32,
        rank_log_advantage(snapshot.p1_rank, snapshot.p2_rank) as f32,
        seed_advantage(snapshot.p1_seed, snapshot.p2_seed) as f32,
        difference(snapshot.p1_age, snapshot.p2_age) as f32,
        difference(snapshot.p1_height_cm, snapshot.p2_height_cm) as f32,
        ((2.0 * h2h_p1) - 1.0) as f32,
        (p1_recent - p2_recent) as f32,
        (snapshot.p1_recent_matches - snapshot.p2_recent_matches) as f32,
        (rest_days(snapshot.p1_days_since_match) - rest_days(snapshot.p2_days_since_match)) as f32,
        if snapshot.best_of >= 5 { 1.0 } else { 0.0 },
        if snapshot.tourney_level.eq_ignore_ascii_case("G") {
            1.0
        } else {
            0.0
        },
        p1_probability as f32,
        (p1_probability - p2_probability) as f32,
        overround as f32,
        if surface == "hard" { 1.0 } else { 0.0 },
        if surface == "clay" { 1.0 } else { 0.0 },
        if surface == "grass" { 1.0 } else { 0.0 },
        if surface == "carpet" { 1.0 } else { 0.0 },
        if matches!(surface.as_str(), "hard" | "clay" | "grass" | "carpet") {
            0.0
        } else {
            1.0
        },
    ]
}

pub fn tennis_xgboost_named_feature_vector(
    snapshot: &TennisMatchSnapshot,
) -> Vec<(&'static str, f32)> {
    TENNIS_XGBOOST_FEATURE_NAMES
        .iter()
        .copied()
        .zip(tennis_xgboost_feature_vector(snapshot))
        .collect()
}

fn difference(left: Option<f64>, right: Option<f64>) -> f64 {
    left.unwrap_or(0.0) - right.unwrap_or(0.0)
}

fn rank_log_advantage(p1_rank: Option<i32>, p2_rank: Option<i32>) -> f64 {
    let p1 = p1_rank.filter(|value| *value > 0).unwrap_or(2500);
    let p2 = p2_rank.filter(|value| *value > 0).unwrap_or(2500);
    f64::from(p2).ln_1p() - f64::from(p1).ln_1p()
}

fn seed_advantage(p1_seed: Option<i32>, p2_seed: Option<i32>) -> f64 {
    f64::from(p2_seed.filter(|value| *value > 0).unwrap_or(64))
        - f64::from(p1_seed.filter(|value| *value > 0).unwrap_or(64))
}

fn smoothed_win_pct(wins: i32, matches: i32) -> f64 {
    (f64::from(wins) + 2.5) / (f64::from(matches) + 5.0)
}

fn rest_days(value: Option<i32>) -> f64 {
    f64::from(value.unwrap_or(0).clamp(0, 30))
}

fn normalized_implied_probabilities(p1: Option<f64>, p2: Option<f64>) -> (f64, f64, f64) {
    let p1_raw = p1
        .filter(|value| *value > 1.0)
        .map_or(0.0, |value| 1.0 / value);
    let p2_raw = p2
        .filter(|value| *value > 1.0)
        .map_or(0.0, |value| 1.0 / value);
    let overround = p1_raw + p2_raw;
    if overround <= 0.0 {
        (0.5, 0.5, 0.0)
    } else {
        (p1_raw / overround, p2_raw / overround, overround)
    }
}

// ---------- tennis v2 (34-feature) ----------
//
// Mirrors `eventcontracts.research.tennis_v2.feature_row_v2` operation-for-
// operation. ONLY the stateless snapshot -> vector arithmetic lives here; the
// stateful accumulation (dynamic-K/MoV Elo, rolling serve/return, fatigue
// windows, opponent-adjusted form) stays Python-side in the training-frame
// builder, exactly like v1. Cross-language agreement is pinned by
// `contracts/parity/tennis_v2_features/feature_cases.json` (Python-generated,
// asserted in `tennis_v2_tests::feature_vector_matches_python_fixture`).

/// Feature order shared with `tennis_v2.TENNIS_V2_FEATURE_NAMES` and the
/// promoted v2 `feature_schema.json`.
pub const TENNIS_V2_FEATURE_NAMES: [&str; 34] = [
    "elo_diff",
    "surface_elo_diff",
    "elo_blend_diff",
    "rank_log_advantage",
    "rank_points_log_diff",
    "seed_advantage",
    "age_diff",
    "height_cm_diff",
    "hand_matchup",
    "serve_pts_won_diff",
    "return_pts_won_diff",
    "serve_edge_diff",
    "ace_rate_diff",
    "df_rate_diff",
    "bp_save_pct_diff",
    "bp_convert_pct_diff",
    "recent_win_pct_diff",
    "opp_adjusted_form_diff",
    "surface_win_pct_diff",
    "recent_match_count_diff",
    "days_rest_diff",
    "recent_matches_14d_diff",
    "recent_games_14d_diff",
    "prev_match_long_diff",
    "best_of_5",
    "is_grand_slam",
    "round_ordinal",
    "surface_hard",
    "surface_clay",
    "surface_grass",
    "p1_implied_prob",
    "implied_prob_diff",
    "odds_overround",
    "odds_present",
];

/// Pre-match v2 state input. Serde field names match the Python
/// `TennisV2Snapshot` dataclass so a single JSON shape feeds both languages.
/// Defaults mirror the Python priors so a default snapshot scores identically.
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(default)]
pub struct TennisV2Snapshot {
    pub surface: String,
    pub tourney_level: String,
    pub best_of: i32,
    pub round: String,
    pub p1_elo: f64,
    pub p2_elo: f64,
    pub p1_surface_elo: f64,
    pub p2_surface_elo: f64,
    pub p1_elo_blend: f64,
    pub p2_elo_blend: f64,
    pub p1_rank: Option<i32>,
    pub p2_rank: Option<i32>,
    pub p1_rank_points: Option<f64>,
    pub p2_rank_points: Option<f64>,
    pub p1_seed: Option<i32>,
    pub p2_seed: Option<i32>,
    pub p1_age: Option<f64>,
    pub p2_age: Option<f64>,
    pub p1_height_cm: Option<f64>,
    pub p2_height_cm: Option<f64>,
    pub p1_hand: String,
    pub p2_hand: String,
    pub p1_serve_won: f64,
    pub p2_serve_won: f64,
    pub p1_return_won: f64,
    pub p2_return_won: f64,
    pub p1_ace_rate: f64,
    pub p2_ace_rate: f64,
    pub p1_df_rate: f64,
    pub p2_df_rate: f64,
    pub p1_bp_save: f64,
    pub p2_bp_save: f64,
    pub p1_bp_convert: f64,
    pub p2_bp_convert: f64,
    pub p1_recent_wins: i32,
    pub p2_recent_wins: i32,
    pub p1_recent_matches: i32,
    pub p2_recent_matches: i32,
    pub p1_opp_adjusted_form: f64,
    pub p2_opp_adjusted_form: f64,
    pub p1_surface_wins: i32,
    pub p2_surface_wins: i32,
    pub p1_surface_matches: i32,
    pub p2_surface_matches: i32,
    pub p1_days_since_match: Option<i32>,
    pub p2_days_since_match: Option<i32>,
    pub p1_matches_14d: i32,
    pub p2_matches_14d: i32,
    pub p1_games_14d: i32,
    pub p2_games_14d: i32,
    pub p1_prev_long: i32,
    pub p2_prev_long: i32,
    pub p1_decimal_odds: Option<f64>,
    pub p2_decimal_odds: Option<f64>,
}

impl Default for TennisV2Snapshot {
    fn default() -> Self {
        Self {
            surface: "Unknown".into(),
            tourney_level: String::new(),
            best_of: 3,
            round: String::new(),
            p1_elo: 1500.0,
            p2_elo: 1500.0,
            p1_surface_elo: 1500.0,
            p2_surface_elo: 1500.0,
            p1_elo_blend: 1500.0,
            p2_elo_blend: 1500.0,
            p1_rank: None,
            p2_rank: None,
            p1_rank_points: None,
            p2_rank_points: None,
            p1_seed: None,
            p2_seed: None,
            p1_age: None,
            p2_age: None,
            p1_height_cm: None,
            p2_height_cm: None,
            p1_hand: "U".into(),
            p2_hand: "U".into(),
            p1_serve_won: 0.63,
            p2_serve_won: 0.63,
            p1_return_won: 0.37,
            p2_return_won: 0.37,
            p1_ace_rate: 0.07,
            p2_ace_rate: 0.07,
            p1_df_rate: 0.045,
            p2_df_rate: 0.045,
            p1_bp_save: 0.61,
            p2_bp_save: 0.61,
            p1_bp_convert: 0.40,
            p2_bp_convert: 0.40,
            p1_recent_wins: 0,
            p2_recent_wins: 0,
            p1_recent_matches: 0,
            p2_recent_matches: 0,
            p1_opp_adjusted_form: 0.0,
            p2_opp_adjusted_form: 0.0,
            p1_surface_wins: 0,
            p2_surface_wins: 0,
            p1_surface_matches: 0,
            p2_surface_matches: 0,
            p1_days_since_match: None,
            p2_days_since_match: None,
            p1_matches_14d: 0,
            p2_matches_14d: 0,
            p1_games_14d: 0,
            p2_games_14d: 0,
            p1_prev_long: 0,
            p2_prev_long: 0,
            p1_decimal_odds: None,
            p2_decimal_odds: None,
        }
    }
}

/// Produce exactly the float32 tensor row expected by the v2 `model.onnx`.
pub fn tennis_v2_feature_vector(s: &TennisV2Snapshot) -> [f32; 34] {
    let (p1_prob, p2_prob, overround) =
        normalized_implied_probabilities(s.p1_decimal_odds, s.p2_decimal_odds);
    let surface = s.surface.trim().to_ascii_lowercase();
    let p1_recent = smoothed_win_pct(s.p1_recent_wins, s.p1_recent_matches);
    let p2_recent = smoothed_win_pct(s.p2_recent_wins, s.p2_recent_matches);
    let p1_surface = smoothed_win_pct(s.p1_surface_wins, s.p1_surface_matches);
    let p2_surface = smoothed_win_pct(s.p2_surface_wins, s.p2_surface_matches);
    let p1_total = s.p1_serve_won + s.p1_return_won;
    let p2_total = s.p2_serve_won + s.p2_return_won;
    [
        (s.p1_elo - s.p2_elo) as f32,
        (s.p1_surface_elo - s.p2_surface_elo) as f32,
        (s.p1_elo_blend - s.p2_elo_blend) as f32,
        rank_log_advantage(s.p1_rank, s.p2_rank) as f32,
        (log1p_points(s.p1_rank_points) - log1p_points(s.p2_rank_points)) as f32,
        seed_advantage(s.p1_seed, s.p2_seed) as f32,
        difference(s.p1_age, s.p2_age) as f32,
        difference(s.p1_height_cm, s.p2_height_cm) as f32,
        hand_matchup(&s.p1_hand, &s.p2_hand),
        (s.p1_serve_won - s.p2_serve_won) as f32,
        (s.p1_return_won - s.p2_return_won) as f32,
        (p1_total - p2_total) as f32,
        (s.p1_ace_rate - s.p2_ace_rate) as f32,
        (s.p1_df_rate - s.p2_df_rate) as f32,
        (s.p1_bp_save - s.p2_bp_save) as f32,
        (s.p1_bp_convert - s.p2_bp_convert) as f32,
        (p1_recent - p2_recent) as f32,
        (s.p1_opp_adjusted_form - s.p2_opp_adjusted_form) as f32,
        (p1_surface - p2_surface) as f32,
        (s.p1_recent_matches - s.p2_recent_matches) as f32,
        (rest_days(s.p1_days_since_match) - rest_days(s.p2_days_since_match)) as f32,
        (s.p1_matches_14d - s.p2_matches_14d) as f32,
        (s.p1_games_14d - s.p2_games_14d) as f32,
        (s.p1_prev_long - s.p2_prev_long) as f32,
        if s.best_of >= 5 { 1.0 } else { 0.0 },
        if s.tourney_level.eq_ignore_ascii_case("G") {
            1.0
        } else {
            0.0
        },
        round_ordinal(&s.round),
        if surface == "hard" { 1.0 } else { 0.0 },
        if surface == "clay" { 1.0 } else { 0.0 },
        if surface == "grass" { 1.0 } else { 0.0 },
        p1_prob as f32,
        (p1_prob - p2_prob) as f32,
        overround as f32,
        if overround > 0.0 { 1.0 } else { 0.0 },
    ]
}

pub fn tennis_v2_named_feature_vector(s: &TennisV2Snapshot) -> Vec<(&'static str, f32)> {
    TENNIS_V2_FEATURE_NAMES
        .iter()
        .copied()
        .zip(tennis_v2_feature_vector(s))
        .collect()
}

/// `math.log1p(_number(points, 0.0))` — missing rank-points default to 0.
fn log1p_points(points: Option<f64>) -> f64 {
    points.unwrap_or(0.0).ln_1p()
}

/// `+1` p1 lefty vs righty, `-1` righty vs lefty, `0` otherwise. Mirrors
/// `tennis_v2._hand_matchup` (first char, ASCII-uppercased).
fn hand_matchup(p1_hand: &str, p2_hand: &str) -> f32 {
    let first =
        |h: &str| -> Option<char> { h.trim().chars().next().map(|c| c.to_ascii_uppercase()) };
    match (first(p1_hand), first(p2_hand)) {
        (Some('L'), Some('R')) => 1.0,
        (Some('R'), Some('L')) => -1.0,
        _ => 0.0,
    }
}

/// Round name -> [0,1]. Mirrors `tennis_v2._ROUND_ORDINAL` / `_MAX_ROUND`.
fn round_ordinal(round_name: &str) -> f32 {
    let ordinal = match round_name.trim().to_ascii_uppercase().as_str() {
        "R128" => 1,
        "R64" => 2,
        "R32" => 3,
        "R16" => 4,
        "QF" => 5,
        "SF" => 6,
        "F" => 7,
        "RR" => 3,
        "BR" => 6,
        _ => 0,
    };
    ordinal as f32 / 7.0
}

#[cfg(test)]
mod hot_path_tests {
    use super::*;
    use eventcontracts_runtime_hot::{FixedPrice, HotQuote};

    #[test]
    fn quote_features_layout_is_bid_ask_spread_mid() {
        let q = HotQuote {
            instrument: "kalshi:X".into(),
            bid: FixedPrice(4200), // $0.42
            ask: FixedPrice(4400), // $0.44
        };
        let v = quote_features(&q);
        assert_eq!(v.len(), QUOTE_FEATURE_WIDTH);
        assert!((v[0] - 0.42).abs() < 1e-6);
        assert!((v[1] - 0.44).abs() < 1e-6);
        assert!((v[2] - 0.02).abs() < 1e-6);
        assert!((v[3] - 0.43).abs() < 1e-6);
    }

    #[test]
    fn rolling_quote_features_include_deltas_and_window_means() {
        let mut state = RollingQuoteState::new(2);
        let first = quote_features_rolling_raw(&mut state, 0.42, 0.44);
        assert_eq!(state.len(), 1);
        assert!((first[4] - 0.0).abs() < 1e-6);
        let second = quote_features_rolling_raw(&mut state, 0.43, 0.46);
        assert_eq!(state.len(), 2);
        assert!((second[4] - 0.015).abs() < 1e-6);
        assert!((second[7] - 0.025).abs() < 1e-6);
    }

    /// Tiny test-only `Scorer` so downstream crates can pin their
    /// strategy logic without depending on `ort`.
    struct ConstScorer {
        out: Vec<f32>,
    }
    impl Scorer for ConstScorer {
        fn input_width(&self) -> usize {
            QUOTE_FEATURE_WIDTH
        }
        fn predict(&self, features: &[f32]) -> Result<Vec<f32>, ScorerError> {
            if features.len() != self.input_width() {
                return Err(ScorerError::InputWidth {
                    expected: self.input_width(),
                    actual: features.len(),
                });
            }
            Ok(self.out.clone())
        }
    }

    #[test]
    fn scorer_returns_configured_output_for_correct_width() {
        let s = ConstScorer {
            out: vec![0.72, 0.28],
        };
        let v = s.predict(&[0.1, 0.2, 0.3, 0.4]).unwrap();
        assert_eq!(v, vec![0.72, 0.28]);
    }

    #[test]
    fn scorer_rejects_wrong_width() {
        let s = ConstScorer { out: vec![0.5] };
        let err = s.predict(&[0.1, 0.2]).unwrap_err();
        assert!(matches!(
            err,
            ScorerError::InputWidth {
                expected: 4,
                actual: 2
            }
        ));
    }
}

#[cfg(test)]
mod tennis_tests {
    use super::*;

    #[test]
    fn tennis_vector_matches_python_contract_order_and_calculations() {
        let snapshot = TennisMatchSnapshot {
            surface: "Grass".into(),
            tourney_level: "G".into(),
            best_of: 5,
            p1_elo: 1600.0,
            p2_elo: 1500.0,
            p1_surface_elo: 1580.0,
            p2_surface_elo: 1510.0,
            p1_rank: Some(2),
            p2_rank: Some(20),
            p1_seed: Some(1),
            p2_seed: Some(8),
            p1_age: Some(26.5),
            p2_age: Some(30.0),
            p1_height_cm: Some(188.0),
            p2_height_cm: Some(180.0),
            p1_h2h_wins: 3,
            p2_h2h_wins: 1,
            p1_recent_wins: 8,
            p2_recent_wins: 5,
            p1_recent_matches: 10,
            p2_recent_matches: 10,
            p1_days_since_match: Some(3),
            p2_days_since_match: Some(5),
            p1_decimal_odds: Some(1.50),
            p2_decimal_odds: Some(2.75),
        };
        let vector = tennis_xgboost_feature_vector(&snapshot);

        assert_eq!(TENNIS_XGBOOST_FEATURE_NAMES.len(), vector.len());
        assert_eq!(vector[0], 100.0);
        assert_eq!(vector[1], 70.0);
        assert_eq!(vector[10], 1.0);
        assert_eq!(vector[11], 1.0);
        assert!((vector[12] - 0.64705884).abs() < 1e-7);
        assert_eq!(vector[17], 1.0);
        assert_eq!(vector[19], 0.0);
    }

    #[test]
    fn tennis_vector_defaults_missing_odds_and_unknown_surface() {
        let vector = tennis_xgboost_feature_vector(&TennisMatchSnapshot::default());

        assert_eq!(vector[12], 0.5);
        assert_eq!(vector[14], 0.0);
        assert_eq!(vector[19], 1.0);
    }
}

#[cfg(test)]
mod tennis_v2_tests {
    use super::*;
    use serde::Deserialize;

    #[test]
    fn v2_default_snapshot_is_neutral() {
        let v = tennis_v2_feature_vector(&TennisV2Snapshot::default());
        assert_eq!(v.len(), 34);
        // elo/surface/blend diffs zero; rank/seed advantage zero (both default).
        assert_eq!(v[0], 0.0);
        assert_eq!(v[2], 0.0);
        // neutral market block when no odds: p1_implied_prob=0.5, present=0.
        assert_eq!(v[30], 0.5);
        assert_eq!(v[33], 0.0);
        // unknown surface -> no one-hot fires.
        assert_eq!(v[27], 0.0);
        assert_eq!(v[28], 0.0);
        assert_eq!(v[29], 0.0);
    }

    #[test]
    fn v2_hand_and_round_helpers() {
        assert_eq!(hand_matchup("L", "R"), 1.0);
        assert_eq!(hand_matchup("R", "L"), -1.0);
        assert_eq!(hand_matchup("R", "R"), 0.0);
        assert!((round_ordinal("QF") - 5.0 / 7.0).abs() < 1e-6);
        assert_eq!(round_ordinal("F"), 1.0);
        assert_eq!(round_ordinal("unknown"), 0.0);
    }

    #[derive(Deserialize)]
    struct ParityFixture {
        feature_names: Vec<String>,
        cases: Vec<ParityFeatureCase>,
    }

    #[derive(Deserialize)]
    struct ParityFeatureCase {
        case_id: String,
        snapshot: TennisV2Snapshot,
        expected: Vec<f64>,
    }

    /// The keystone Python<->Rust parity guard: the committed fixture is
    /// generated by `tennis_v2.feature_parity_fixture()`; here Rust loads the
    /// same snapshots through serde and asserts the vector agrees within f32
    /// tolerance. If the two feature builders ever drift, this fails.
    #[test]
    fn feature_vector_matches_python_fixture() {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../../contracts/parity/tennis_v2_features/feature_cases.json");
        let raw = std::fs::read_to_string(&path)
            .unwrap_or_else(|e| panic!("read parity fixture {}: {e}", path.display()));
        let fixture: ParityFixture = serde_json::from_str(&raw).expect("parse parity fixture");

        assert_eq!(
            fixture.feature_names.as_slice(),
            TENNIS_V2_FEATURE_NAMES,
            "feature name/order drift between Python and Rust"
        );
        assert!(!fixture.cases.is_empty(), "fixture has no cases");

        for case in &fixture.cases {
            let actual = tennis_v2_feature_vector(&case.snapshot);
            assert_eq!(
                case.expected.len(),
                actual.len(),
                "case {} width mismatch",
                case.case_id
            );
            for (i, (&exp, act)) in case.expected.iter().zip(actual.iter()).enumerate() {
                let diff = (exp - f64::from(*act)).abs();
                let tol = 1e-4 + 1e-6 * exp.abs();
                assert!(
                    diff <= tol,
                    "case {} feature {} ({}): python={exp} rust={act} diff={diff} tol={tol}",
                    case.case_id,
                    i,
                    TENNIS_V2_FEATURE_NAMES[i],
                );
            }
        }
    }
}
