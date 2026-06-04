# Strategy Test & Characterization Report

Date: 2026-06-02

Scope: test and characterize **all 38 strategy configs** — execution speed, how
each reads/triggers, decision behavior (**arb vs conviction hold** vs maker vs
scalp), and decision-math accuracy. Read-only, offline, no orders.

Reproduce: `.venv/Scripts/python.exe python/scripts/strategy_characterization.py`
(the harness) + `python -m pytest python/tests -q` (the suite).

## What "accuracy" means here (and what it doesn't)

There are two different questions and only one is answerable offline:

- **Decision-math accuracy** (answered, exact): given inputs, does the strategy
  emit the correct decision? Verified below against an independent reference.
- **Statistical / out-of-sample predictive accuracy** (NOT answered here): does
  the model beat the market at real fills? That requires labeled historical
  replay with point-in-time data + the CLV ledger — the promotion gate, which
  needs data/creds and is out of scope for a synthetic harness. Do not read this
  report as "these strategies are profitable"; read it as "these strategies are
  wired correctly, fast, and behave as classified."

## Headline results

| Dimension | Result |
|---|---|
| Configs tested | **38 / 38 instantiate** (0 failures) |
| Full test suite | **439 passed** (43.9s) |
| Hot-path `on_event` latency | **0.19–4.1 µs** (median ~0.52 µs) |
| `ladder_cdf` normal CDF vs scipy | max\|err\| = **1.11e-16** (machine epsilon) |
| `ladder_cdf` logistic CDF vs scipy | max\|err\| = **0.0** |
| No-arb sum-lock boundary | fires at Σask 0.90 (3 legs), not at 1.02 (0) — **exact** |
| `external_edge` edge gate | fires at 700bps (min 400), not at 100bps — **exact** |

**Verdict:** every strategy loads, runs, and is classified; the deterministic
decision math is exact; execution speed is irrelevant to edge (Python decision
cost is single-digit microseconds while Kalshi network RTT is ~10–35 ms from EC2,
i.e. ~10,000× larger — see the latency playbook). The binding constraints remain
**data/producers and proof**, not compute speed.

## Speed (decision-path latency)

Hot-path = time to process one `QuoteEvent` (the most frequent event), 3000 iters
after warmup.

- **Sub-µs (0.19–0.25 µs):** the lean runtimes — `kalshi_noarb_scanner`,
  `macro_nfp_absorber`, both microstructure scalpers, `macro_fed_gnn`,
  `sports_frl_weather_arb`, `sports_player_cut_lgbm`.
- **~0.5 µs:** the `external_edge` / `ladder_cdf` family and most scaffolds.
- **~3.3–4.1 µs (the "slow" tail):** `weather_temperature_arbitrage` (4.1),
  `sports_tennis_xgboost` (3.8), `arbitrage_cross_venue` (3.8), `example_threshold`
  (3.3), `sports_hole_by_hole_pin` (4.1) — heavier per-quote work (snapshot
  construction, feature vectors, dual-venue bookkeeping).

Even the slowest is **~4 µs**, ~four orders of magnitude under network RTT.
Conclusion: **no strategy here is compute-latency-bound.** The latency-sensitive
families (BTC final-window, microstructure) are bound by *network/queue* latency,
not the Python decision, and remain excluded/observe-only per the playbook.

## Behavior classification — arb vs conviction hold (and the rest)

| Class | Count | Strategies | Order style |
|---|---:|---|---|
| **Arb / no-arb lock** | 3 | `kalshi_noarb_scanner`, `range-ladder-noarb`, `arbitrage_cross_venue` | IOC, immediate, risk-free-when-locked; **leg risk** is the headline caveat |
| **Conviction hold (directional)** | ~24 | weather (×3 incl. ladder-cdf), entertainment (box office + awards), macro (cpi/cpi-cdf/fed), equity (range + close-cdf), commodity (brent-cdf), tennis, mlb-outright, sports-sharp-lag, politics, and slow scaffolds | LIMIT GTC, fair-priced, held to settlement (some with trailing-stop exit) |
| **Maker / liquidity provision** | 2 | `macro_nfp_absorber`, `liquidity_tail_risk_insurance` | resting quotes / spread capture around events |
| **Scalp (fast, inventory-bounded)** | 2 | `microstructure_obi_scalper`, `microstructure_queue_evader` | fast LIMIT, cancel-heavy; network-latency-bound (excluded) |
| **Observe / needs-specific-signal** | 7 | aerospace, court-docket, earnings, energy-storage, flu, shipping, tariff | scaffolds whose specific external-payload keys the generic battery doesn't synthesize |

**The arb vs conviction distinction in practice:**
- **Arb** = the edge is *logical consistency*, realized immediately with IOC legs;
  payoff is bounded/risk-free **iff all legs fill** — partial fills are the whole
  risk. Latency-relaxed (it's not a race), but execution must become atomic before
  going past paper.
- **Conviction hold** = the edge is a *better probability than the mid*; you take a
  directional position and hold to settlement (or exit on model-fair convergence /
  source invalidation / trailing stop). Latency-irrelevant; the edge is the
  distribution, and the risk is being wrong, not being slow.

## How each strategy reads (trigger surface)

- **Quote + external** (the predictive majority): cache the YES mid from quotes,
  act on an external signal (model probability, or a distribution for `ladder_cdf`).
- **Quote-only**: `kalshi_noarb_scanner` / `arbitrage_cross_venue` (pure book
  logic), and `politics_primary_momentum` (settlement + quote).
- **Trade-triggered**: `example_threshold`, `macro_fed_gnn` (front-month prints).
- **Timer-triggered**: `macro_nfp_absorber` (release windows), `aerospace_launch_delay`.
- **Book / own-order**: the microstructure scalpers (`book`, `own_order_update`).

Full per-strategy table (reads / tier / hot-µs / emits / behavior) is the harness
stdout — regenerate with the command at the top.

## Accuracy — decision-math correctness (exact)

Independent checks (not self-consistency):

1. `ladder_cdf` normal CDF vs `scipy.stats.norm.cdf` over 5 points: **1.11e-16**
   max error — exact to floating point. Logistic vs `scipy.stats.logistic`
   (scale = σ·√3/π): **0.0**. The coherent ladder is therefore monotone and
   mass-consistent by construction.
2. `kalshi_noarb_scanner` exclusive lock: with three 0.30 asks (Σ=0.90, fees≈0.044)
   it emits **3** buy-YES legs; with three 0.34 asks (Σ=1.02) it emits **0** — the
   lock fires exactly on the `Σask < 1 − fees − min_edge` boundary.
3. `external_edge` edge gate: mid 0.33, prob 0.40 (edge 700bps ≥ 400) → **1** order;
   prob 0.34 (edge 100bps < 400) → **0** — exact threshold behavior.
4. Suite-wide: **439 tests pass**, covering strategy unit behavior, risk gates,
   pricing discretization, parity, and the V6-S2 risk-approval path.

## Notable findings

- **10 / 38 emit on the generic battery.** The rest correctly **hold** because they
  need their specific signal/market: the `ladder_cdf`/`external_edge` family fires
  (they take a generic probability/distribution), `kalshi_noarb_scanner` fires its
  lock, and the 7 scaffolds + box-office/tennis need exact payload keys or model
  bundles. NoAction-on-synthetic is the correct, conservative behavior — not a bug.
- **`weather_temperature_arbitrage` and `sports_tennis_xgboost`** are the heaviest
  per-quote (~4 µs) due to snapshot/feature work — still negligible vs network.
- **The producer gap is the real bottleneck**, confirmed: the directional sleeves
  are wired and fast but emit only when fed a real signal; live decisions need the
  external-signal producers (CDF engine, sharp-odds feed, nowcast) named in v7.
- **No instantiation failures** — including model-backed `sports_tennis_xgboost`,
  which loads cleanly under the installed deps.

## What this report does NOT establish (next gates)

Per the promotion gate and edge-validation philosophy, none of the above implies
profitability. Still required, per sleeve: labeled historical replay, **CLV vs the
Kalshi mid at real fills**, fee/spread/slippage-realistic fill simulation,
exec-mode attribution, capacity from real book depth, and (for arb) atomic
multi-leg execution. Those need data + (for live capture) Kalshi read creds, not a
synthetic harness.
