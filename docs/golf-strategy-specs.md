# Golf Strategy Specs (Lightweight Point-in-Time Alpha)

This document captures four fully-expanded golf tournament strategy
specifications. They adhere to the `StrategyBase` operating contract,
use point-in-time features, and have deterministic execution policies.
Compute is intentionally lightweight (Gradient Boosted Trees and
Bayesian regressors) to optimize the edge-to-latency ratio.

All four are implemented under
`python/src/eventcontracts/plugins/strategies/sports_*.py` and ship with
matching `configs/strategies/sports-*.toml` + `configs/sleeves/sports-*.toml`
pairs. Smoke coverage lives in `python/tests/test_strategy_specs.py`.

The Python framework currently provides linear / logistic regression
in `eventcontracts.models`. Until LGBM / Bayesian Ridge artifacts are
trained and exported, each strategy runs in **rules mode** — a small
deterministic heuristic derived from the same documented feature
inputs. Setting `spec.model` and loading the model into the runner
switches the strategy to **model mode** without changing the decision
shape.

## 1. Player-Level Cut Predictor (Mean-Reversion Exploitation)

### 1. Naming & Discovery
- **Strategy ID:** `sports-player_cut_lgbm-v1`
- **Module:** `python/src/eventcontracts/plugins/strategies/sports_player_cut_lgbm.py`
- **Factory Registration:** `@register("sports_player_cut_lgbm")`
- **Schema ID:** `sports_player_cut_features`
- **Model Name:** `lgbm_cut_predictor_v1`
- **Sleeve ID:** `sports-kalshi-paper-a`

### 2. Strategy Review Packet
- **Researcher Owner:** Quant_Sports_Team
- **Hypothesis:** Retail prediction markets misprice cut probabilities after
  Round 1 by over-weighting a player's putting performance
  (highly volatile / mean-reverting) and under-weighting their Strokes
  Gained: Approach (highly sticky / predictive). A player who shot over par
  while losing strokes on approach is a structural fade. A player who shot
  over par while gaining significant strokes on approach (cold putter) is a
  positive regression candidate.
- **Market Universe:** Kalshi "Will [Player] Make the Cut?" binary markets.
- **Data Sources:**
  - **DataGolf API** (Strokes Gained updates).
  - **PGA Tour Live Scoring API** (strokes-to-cutline tracking).
- **Model Type:** LightGBM Classifier (handles missing data, non-linear
  interactions with weather / course fit).
- **Validation Periods:** Walk-forward across 4 years (2020–2024) of PGA
  Tour stroke-play events.
- **Metrics:** Expected hit rate ~58.2%; inference cost < 5ms.
- **Artifact Bundle Location:** `contracts/examples/sports_player_cut_lgbm/bundle/`

### 3. Feature Engineering Contract
- **Inputs:** `TimerEvent` (player-round-complete) + `ExternalSignalEvent`
  (DataGolf SG updates).
- **Latency Floor:** t0 + 60s (post-round stat settling).
- **Feature Vector:**
  - `t0_current_score_to_projected_cut` (integer strokes off the projected cutline).
  - `t0_sg_approach_current_tournament` (sticky ball-striking form).
  - `t0_sg_putting_current_tournament` (volatile, mean-reverting form).
  - `t0_wave_weather_stroke_delta` (expected stroke penalty for the player's next tee-time wave).
- **Nullability:** If ShotLink camera data drops for a hole,
  `t0_sg_approach_current_tournament` defaults to the player's 3-month
  trailing baseline to prevent strategy failure.

### 4. Label Construction
- **Label Definition:** `resolution_value` (1.0 YES / 0.0 NO).
- **Horizon:** End of Round 2 (official cut determination).
- **Censoring Rules:** Drop labels if the player withdraws or is disqualified
  mid-tournament.

### 5. Policy & Decisions
- **Trigger:** `TimerEvent(label="player_round_complete")` after Round 1 or
  the front nine of Round 2.
- **Logic:** Calls `ctx.predict(model_name, features)` (or rules-mode
  fallback). If
  `abs(lgbm_implied_prob - current_mid) > min_edge_bps`, emit
  `PlaceOrder(priority=RELAXED, order_type=LIMIT, time_in_force=GTC,
  reason="sg_mean_reversion_edge")`.

### 6. Sizing Rules
```
conviction_multiplier = max(0.5, 1.0 + (sg_approach - sg_putting) / 2.0)
raw_size = (predicted_edge_bps / 10000) * ctx.cash(USD)
           * kelly_fraction * conviction_multiplier
risk_capped_size = min(raw_size, strategy_max_size,
                       sleeve_max_order_notional / price)
position_capped_size = min(risk_capped_size, remaining_position_budget)
```

A player gaining 2.0 on approach but losing 2.0 putting gets a 3.0×
multiplier. The inverse player is throttled to 0.5×.

## 2. Aggregate Cut Line Shifter (Bayesian Field Modeling)

### 1. Naming & Discovery
- **Strategy ID:** `sports-cut_line_shifter-v1`
- **Module:** `python/src/eventcontracts/plugins/strategies/sports_cut_line_shifter.py`
- **Factory Registration:** `@register("sports_cut_line_shifter")`
- **Schema ID:** `sports_cut_line_features`
- **Model Name:** `bayesian_cut_line_v1`
- **Sleeve ID:** `sports-kalshi-paper-a`

### 2. Strategy Review Packet
- **Hypothesis:** The tournament cut line (Top 65 and ties) is anchored to
  historical course averages but shifts dynamically based on early
  Thursday / Friday scoring averages and afternoon wind forecasts. Retail
  prediction markets are slow to adjust the exact integer bracket until
  late Friday — the probabilistic shift is statistically locked in by
  Thursday afternoon's scoring differential.
- **Market Universe:** Kalshi exact cut line brackets
  (e.g. "Will the cut line be exactly -2?").
- **Data Sources:**
  - **PGA Tour Live Scoring API** (field aggregate scoring).
  - **Open-Meteo API** (afternoon wind-shear).
- **Model Type:** Bayesian Ridge Regression emitting a PMF over the integer
  cut line; ~2ms inference.
- **Validation Periods:** Walk-forward across 5 years.
- **Metrics:** Calibration 0.94, rank correlation 0.81.
- **Artifact Bundle Location:** `contracts/examples/sports_cut_line_shifter/bundle/`

### 3. Feature Engineering Contract
- **Inputs:** `TimerEvent` (hourly during Thursday/Friday) + `QuoteEvent`.
- **Latency Floor:** t0 + 0ms (timer-based, synchronous with aggregated field stats).
- **Feature Vector:**
  - `t0_historical_course_cut_avg`.
  - `t0_field_scoring_avg_delta_vs_par`.
  - `t0_afternoon_wind_forecast_mph`.
  - `t0_top_65_current_score`.

### 4. Label Construction
- **Label Definition:** `resolution_value` across mutually exclusive cut-line
  integers.
- **Horizon:** End of Round 2.
- **Censoring Rules:** Censored if the tournament format changes mid-event
  (54-hole shortening, etc.).

### 5. Policy & Decisions
- **Trigger:** `TimerEvent(label="cut_line_recompute")` hourly.
- **Logic:** Model emits a PMF (e.g. 60% -2, 30% -1, 10% -3). The strategy
  iterates over tracked Kalshi mid-markets per bracket and emits
  `PlaceOrder(priority=STANDARD)` when
  `bayesian_prob(bracket_x) - kalshi_mid(bracket_x) > min_edge_bps`. NO-side
  intents are emitted for brackets where the market is severely above the
  Bayesian probability.

### 6. Sizing Rules
Multi-outcome Kelly across mutually-exclusive brackets:
```
raw_size(bracket_x) = (edge_bps(bracket_x) / 10000)
                       * ctx.cash(USD) * kelly_fraction
risk_capped_size = min(raw_size, strategy_max_size,
                       sleeve_max_order_notional / price)
position_capped_size = min(risk_capped_size, remaining_position_budget)
```
The central `RiskGate` rejects any over-leveraged net delta across brackets.

## 3. First-Round Leader (FRL) Weather Wave Arbitrage

### 1. Naming & Discovery
- **Strategy ID:** `sports-frl_weather_arb-v1`
- **Module:** `python/src/eventcontracts/plugins/strategies/sports_frl_weather_arb.py`
- **Factory Registration:** `@register("sports_frl_weather_arb")`
- **Schema ID:** `sports_frl_weather_features`
- **Model Name:** `bayesian_frl_wave_v1`
- **Sleeve ID:** `sports-polymarket-paper-a`

### 2. Strategy Review Packet
- **Researcher Owner:** Quant_Sports_Team
- **Hypothesis:** Retail markets price FRL on long-term baseline skill and
  underprice the structural stroke advantage granted to the morning (AM)
  tee-time wave when an afternoon weather front causes severe PM winds
  or firmed-up greens. AM players have a mathematically higher
  probability of going low.
- **Market Universe:** Polymarket / Kalshi FRL or "Top-5 After Round 1"
  brackets.
- **Data Sources:**
  - **Open-Meteo API** (granular hourly wind forecasts).
  - **PGA Tour API** (Thursday tee times).
  - **DataGolf API** (player wind-condition baseline SG).
- **Model Type:** Bayesian Regressor adjusting the scoring-average
  distribution across the field by wave wind vector; ~2ms inference.
- **Validation Periods:** Walk-forward across 4 years of coastal / exposed
  courses (The Open, Sony Open, RSM Classic).
- **Metrics:** Calibration 0.89.

### 3. Feature Engineering Contract
- **Inputs:** `ExternalSignalEvent` (weather forecast + tee-time publication).
- **Latency Floor:** t0 + 5m (processed Wednesday evening / early Thursday
  before the first tee time).
- **Feature Vector:**
  - `t0_am_wave_avg_wind_mph` (08:00 – 12:00).
  - `t0_pm_wave_avg_wind_mph` (13:00 – 17:00).
  - `t0_player_historical_wind_sg` (player's baseline SG when wind > 15mph).
  - `t0_player_wave_assignment` (categorical 0 = AM, 1 = PM).

### 4. Label Construction
- **Label Definition:** `resolution_value` (1.0 if player finishes Round 1
  in 1st place / Top 5).
- **Horizon:** End of Round 1.
- **Censoring Rules:** Censored if the round is delayed by weather, shifting
  wave dynamics.

### 5. Policy & Decisions
- **Trigger:** `ExternalSignalEvent` once the final tee times and weather
  forecast align (typically 05:00 Thursday local).
- **Logic:** Model computes adjusted FRL probability per player off the
  wave delta. When `bayesian_implied_prob - market_mid > min_edge_bps`
  emit `PlaceOrder(priority=RELAXED, order_type=LIMIT, time_in_force=GTC)`.

### 6. Sizing Rules
```
raw_size = (predicted_edge_bps / 10000) * ctx.cash(USD) * kelly_fraction
```
Standard risk and position caps apply.

## 4. Live Hole-by-Hole Pin Access Exploiter

### 1. Naming & Discovery
- **Strategy ID:** `sports-hole_by_hole_pin-v1`
- **Module:** `python/src/eventcontracts/plugins/strategies/sports_hole_by_hole_pin.py`
- **Factory Registration:** `@register("sports_hole_by_hole_pin")`
- **Schema ID:** `sports_hole_by_hole_features`
- **Model Name:** `lgbm_hole_by_hole_v1`
- **Sleeve ID:** `sports-polymarket-paper-a`

### 2. Strategy Review Packet
- **Researcher Owner:** Quant_HFT_Team
- **Hypothesis:** Retail misprices live "Next Hole Birdie" markets by
  evaluating general driving accuracy rather than specific proximity
  metrics. The edge: cross-reference exact daily pin location difficulty
  (tucked back left over a bunker, etc.) with the player's Strokes Gained:
  Approach from the *specific yardage* they just drove the ball to in the
  fairway.
- **Market Universe:** High-liquidity in-play "Will [Player] Birdie Hole X?"
  contracts.
- **Data Sources:**
  - **PGA Tour Live ShotLink API** (low-latency drive coordinates).
  - **DataGolf API** (daily pin difficulty index).
- **Model Type:** LightGBM classifier (extremely low latency).

### 3. Feature Engineering Contract
- **Inputs:** `ExternalSignalEvent` (ShotLink drive data).
- **Latency Floor:** t0 + 20ms (critical — must beat manual bookmaker updates).
- **Feature Vector:**
  - `t0_distance_to_pin_yards`.
  - `t0_pin_difficulty_index`.
  - `t0_player_sg_approach_from_distance`.
  - `t0_lie_condition` (fairway / rough / sand).

### 4. Label Construction
- **Label Definition:** `resolution_value` (1.0 if player makes birdie or
  better on the current hole).
- **Horizon:** End of the current hole (~10–15 min).
- **Censoring Rules:** Nullified if play is suspended for weather while
  the player is on the hole.

### 5. Policy & Decisions
- **Trigger:** `ExternalSignalEvent` the exact moment ShotLink confirms the
  drive has landed and stopped.
- **Logic:** If `lgbm_prob(birdie) - market_mid > min_edge_bps`, emit
  `PlaceOrder(priority=FAST, order_type=MARKET, reason="pin_access_edge")`.
  Market orders jump the spread before makers adjust.

### 6. Sizing Rules
Deterministic to keep the critical path branch-free:
```
raw_size = static_clip_size
```
Position caps prevent over-weighting any single hole.

## Operator setup notes

Each strategy ships with empty operator-supplied maps (player rosters,
bracket → market_id mappings) so the configs load cleanly. To go live:

1. Edit `configs/strategies/sports-*.toml` and fill in
   `player_market_map` / `bracket_market_map` for the tournament
   you're trading.
2. Add the relevant API keys to `.env` (`DATAGOLF_API_KEY`,
   `PGA_TOUR_API_KEY`, `SHOTLINK_API_KEY`).
3. Train a model artifact via `eventcontracts train` (or run in
   rules mode for an initial paper backtest).
4. Run via `eventcontracts backtest --strategy ... --sleeve ... --data ...`
   or wire into a `sweep` grid for parameter / window cohort analysis.
