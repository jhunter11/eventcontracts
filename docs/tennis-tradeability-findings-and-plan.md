# Tennis Model Tradeability — Findings & Staged Plan

**Date:** 2026-05-30
**Audience:** Implementation agent / operator deciding whether and how to make the
`sports_tennis_xgboost` sleeve trade real capital.
**Status:** Research complete; **no live-path or model-pipeline code changed by this
pass.** The findings below redirect strategy from "build a better win model" to
"reprice the venue against a sharp reference," and spell out why the obvious code
changes are gated behind this spec rather than done ad hoc.

All numbers are **walk-forward** (train strictly on past seasons, test on the next),
on the canonical datasets:
`data/tennis/tennis_atp/tennis_atp-master/` (62,768 ATP main-tour matches, 2005–2026)
merged with `data/tennis/tennis_data_odds/` (Pinnacle/Bet365/Max/Avg decimal odds).
Evaluation universe = the **37,572 odds-present matches (2018–2026)** that exactly
match the live sleeve's `require_odds_present=true` filter. Reusable scripts (untracked):
`python/scripts/tennis_{v2_edge_backtest,market_anchored,clv_edge,totals_model}.py`.

---

## §0. Verdict

1. **The winner model cannot beat the ATP closing line.** This is not a tuning
   problem; the line is a near-efficient sharp price and the Sackmann features carry
   little orthogonal signal.
2. **The realistic, robust signal is the sharp-vs-soft price gap (CLV), not the
   model.** The path to tradeability is repricing the *venue* (Kalshi) against a
   sharp reference — the model is at most a fallback fair-value estimator.
3. **Tradeability at real entry prices is NOT yet proven.** Every positive ROI below
   was simulated at **de-vigged** prices; net of a real ~2–4% per-side margin the
   edge is thin. The only test that settles it needs **real Kalshi historical
   quotes**, which we do not have.
4. The earlier "deep-negative no-go" was partly a **backtest fee bug** (flat 7%-of-
   stake instead of Kalshi's `0.07·P·(1−P)` ≈ 1.1–1.7% of stake at tennis prices).

---

## §1. Evidence

### 1a. Plain winner model is dominated by the line
Pooled out-of-sample log-loss (lower = better), 37,572 matches:

| Estimator | log-loss | Brier | beats line? |
|---|---|---|---|
| Closing line (de-vigged Pinnacle) | 0.58843 | 0.20243 | — |
| Plain v2 model (antisymmetric) | 0.59215 | — | ✗ — worse in **all 9** seasons |

The de-vigged line is already a model feature (`p1_implied_prob`), and XGBoost still
produces a worse estimate — it adds variance to a good signal.

### 1b. Market-anchored model — the principled "improve the model" construction
XGBoost with `base_margin = logit(line)` + heavy regularization (`max_depth=3`,
`min_child_weight=20`, `reg_lambda=5`, `reg_alpha=1`), so it *starts* at the line and
only learns robust residuals.

| Estimator | pooled log-loss | beats line? |
|---|---|---|
| **Anchored model** | **0.58789** | ✓ pooled, but only **5/9** seasons |

It stops the degradation and extracts a **tiny** real residual (≈0.09% log-loss).
Betting sweep (real Kalshi fee, **de-vigged** entry price): +1.2% ROI @ edge≥100bps
(n=29,352) scaling monotonically to +4.3% @ edge≥500bps (n=5,306). Monotone-in-
threshold is the signature of a genuine (if small) edge — but see §1d on entry price.

### 1c. Sharp beats soft (the real structural fact)
Pinnacle (sharp) vs Bet365 / market-Avg (soft), 32,554 matches with both:
`pinnacle_logloss 0.57993 < bet365_logloss 0.58084` (and `< Avg 0.58050`) — sharp
beats soft as a pure calibration fact, **independent of any betting rule**, robust to
the soft proxy used. Backing the soft-book disagreement: favourites @ edge≥200bps →
+2.7% ROI, win-rate 0.775, n=4,063; positive in **10/14** seasons.

### 1d. The honest caveat that caps all betting numbers
Both the §1b and §1c sweeps **enter at de-vigged probabilities**, i.e. no-vig fair
odds that no book actually offers. Real quotes carry a ~2–4% per-side margin on tennis
singles. Net of that, the anchored edge is ≈break-even and the CLV edge is thin. The
sweeps prove *direction and correlation*, not a bankable real-price edge.

### 1e. Duration / total-games (the "time-to-expiry" idea)
Leak-free **only as a totals-market target** (match length is unknown pre-match, so it
cannot be a winner feature). Walk-forward, 18,832 matches: over/under-total-games
**AUC 0.568**, MAE 1.57% better than a per-(best-of, surface) baseline. A real but
modest signal — and Kalshi tennis is **moneyline**, with no totals odds source, so
there is nothing to monetize it on today.

### 1f. Fee model — match the Rust curve
Source of truth: `rust/crates/risk/src/fees.rs::kalshi_taker_fee_ticks` —
`fee = ceil(rate_bps · P · (1−P) · qty / 1e10) · 100` cents, `rate_bps=700`. Any
post-fee backtest must use this, not a flat stake percentage. (The Rust live gate and
`risk/limits.py::check_fee_adjusted_edge` already use it; only the research backtest
was wrong.)

---

## §2. Why the obvious code changes are gated here, not done

- **Promoting the anchored model into `train_v2`** (`python/src/eventcontracts/research/tennis_v2.py`)
  is *not* contained: a `base_margin` model's raw output is a **residual over the line**,
  so it requires `logit(line)` as a base-margin input at inference. That breaks the ONNX
  export (`export_v2_onnx`) and the Rust scorer + the committed parity contracts
  (`contracts/parity/sports_tennis_xgboost/*`, `contracts/parity/tennis_v2_features/*`),
  which all assume a standalone `binary:logistic` probability. It would need the Rust
  scorer to add `logit(line)` post-hoc and a parity regeneration — a coordinated
  Rust+Python change, exactly what the promotion gate exists to control.
- **Sharp-reference repricing** touches the live-money path (the fair value fed to the
  fee-adjusted-edge gate) and needs a **live sharp-odds feed** at runtime. The odds-feed
  plumbing exists (`tennis-merge-odds` CLI, F8 — see `python/src/eventcontracts/research/tennis_odds_feed.py`),
  but wiring a sharp consensus in as *fair value* is a strategy-logic change deserving
  its own verified phase.

Given §1d (edge unproven at real prices), shipping either now would be trading on a
signal we have not validated where it counts.

---

## §3. Staged plan

**Stage 1 — Validate at real prices (do first; currently blocked on data).**
Acquire real Kalshi tennis historical quotes (or run a paper-capture window on live
Kalshi tennis markets) and re-run `tennis_clv_edge.py` / `tennis_market_anchored.py`
with the **actual Kalshi entry price** as the fill, not a de-vigged book price. Go/no-go
on a real edge lives here. Blocker: no Kalshi quote history on hand.

**Stage 2 — Sharp-reference repricing (the tradeable architecture, if Stage 1 is green).**
Fair value = de-vigged sharp consensus (Pinnacle, or multi-book consensus) when present,
falling back to the anchored model. The strategy emits an order only when the Kalshi
price deviates from fair value by ≥ threshold, sized through the existing
`check_fee_adjusted_edge` gate. Needs: a runtime sharp-odds source (extend F8 feed),
the anchored model as fallback (Stage 3), and full parity + S2 smoke per the promotion
gate.

**Stage 3 — Anchored model as fallback estimator (parity-coordinated).**
Add the `base_margin=logit(line)` training mode behind a flag; teach the Rust scorer to
add `logit(line)` to the model output; regenerate parity fixtures; bump
`feature_schema_version`. Only worth doing as the Stage-2 fallback, not standalone.

**Stage 4 — Totals market (contingent).**
If/when Kalshi lists tennis over/under-games markets, build the §1e duration model into
a dedicated sleeve. Park until such a market exists.

---

## §4. Bottom line
The model is not, and cannot easily be made, a winner-market alpha against a sharp
line. The credible edge is structural (sharp > soft) and must be harvested by
repricing the venue, not by a better classifier — and it is unproven until tested at
real Kalshi prices. Do **not** fund the current sleeve. Stage 1 is the gating next step.
