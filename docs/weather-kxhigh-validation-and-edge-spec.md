# KXHIGH Weather Strategy — Validation & Edge Spec (v1)

Status: pre-deployment validation plan. Owner workflow: versioned spec, weighted to
trading logic, backed by real runs. Created 2026-05-31.

## Governing principle (sharpened)

> A model can be well-calibrated against weather outcomes and still have **zero
> trading edge** if the market is equally or more accurate.

The validated calibration gate (Brier 0.0395, ECE 0.0056 vs NOAA ground truth)
answers *"are our probabilities honest?"* — which is **necessary but not
sufficient**. The deployment question is:

> **Are our probabilities better than the market *at the moment we can actually
> trade*, net of fees, spread, fills, and adverse selection?**

This mirrors the tennis finding ([[eventcontracts-tennis-tradeability]]): a sharp
model ≠ a beatable market. Do **not** expand the station universe until the current
three (NY/CHI/MIA) show real, settled, post-cost market edge.

## Current state (grounded in code)

| Component | File | State |
|---|---|---|
| Calibration model (bias+σ per station/month, normal CDF) | `weather/calibration.py` | walk-forward gate PASS; **σ is ~nowcast-lead (not lead-aware)** |
| KXHIGH bracket pricing | `weather/kxhigh.py` | greater/less/between → YES prob; verified vs live rules (below) |
| Strategy (rules-mode, edge-vs-mid, passive/taker) | `plugins/strategies/weather_temperature_arbitrage.py` | 4dp `fair_price` in **taker** path only |
| Paper harness (price live books, miscalibration guard, record/settle) | `scripts/weather_kxhigh_paper.py` | has fee model, high-so-far distribution pricing, miscalibration guard, lead=0-only edges, CLV + fee-net PnL enrichment |
| Paper ledger | `data/weather-paper/kxhigh_ledger.jsonl`, `live-test/weather-kxhigh-distribution-ledger.jsonl` | prior entries remain unsettled; latest high-so-far distribution pass recorded 0 new candidates -> edge UNPROVEN |
| Rust twin | `rust/crates/runner/src/lib.rs` | exists, but parity should be re-audited whenever producer payload/decision gates change |

## Phase 1 — Data integrity (label correctness first)

**1. Settlement-source verification — DONE 2026-05-31.** Live Kalshi rules confirm:
- Source/station: **NWS Climatological Report (Daily), Central Park** = the model's
  `KXHIGH_STATIONS["KXHIGHNY"]` (GHCND `USW00094728`). ✓
- `greater` floor F: rules "greater than F°" → YES = high ≥ F+1 = model `p_high_at_least(F+1)`. ✓
- `less` cap C: "less than C°" → YES = high ≤ C−1 = model `1 − p_high_at_least(C)`. ✓
- `between [F,C]`: "between F-C°" inclusive → YES = F ≤ high ≤ C = model `p_between(F,C)`. ✓
- **GHCND-vs-CLI risk — RECONCILED 2026-05-31, PASS, no fix needed**
  (`scripts/weather_settlement_reconcile.py`): the worry was that the market settles
  on the **NWS CLI Daily integer °F** while the model fits/settles on **GHCND TMAX**
  (tenths-°C → °F + round), which could differ ±1°F and flip 1°-wide brackets. Tested
  by recomputing each settled bracket's YES/NO from GHCND TMAX and comparing to
  Kalshi's actual result: **810 brackets across NY/CHI/MIA × 45 days each → 0
  disagreements**. The °F→tenths-°C→°F round-trip is clean for these ASOS stations,
  so GHCND reproduces the CLI label exactly. (Caveat: the ladder is too coarse to pin
  the exact integer head-to-head — `exact-pinned days=0` — but the outcome test is
  sensitive to ±1° errors at the boundaries near the high and found none.) Keep the
  reconcile script as a pre-deployment / kill-switch check (halt on any disagreement).
- **2. Confirm day-boundary/timezone:** local calendar day max — already mirrored
  between the dataset builder and `daily_high_from_snapshot`; add an explicit test.
- **3. Build the settled paper-PnL + CLV logging loop — DONE 2026-05-31.**
  `scripts/weather_kxhigh_paper.py` now records side-aware `model_price`,
  `market_mid_at_entry`, `fill_price`, fees, and close time on paper entries.
  `--settle` enriches entries with `market_mid_near_close`, `clv`,
  `actual_high_int`, `won`, and fee-net `realized_pnl`; use `--write-settled`
  for in-place ledger updates or `--settle-out` for an enriched copy. Evidence is
  still pending: the existing ledger has five 2026-05-31 NY entries and GHCND has
  not published the actual yet.

## Phase 2 — Model correction

- **Lead-aware bias & σ (highest model priority).** σ is fit ~nowcast, so lead≥1
  brackets are over-confident (the harness already discards all but lead=0 for this
  reason). Fit `bias(lead)`, `σ(lead)` so forward brackets price honestly and stop
  manufacturing wing "edges."
- **Bracket-level calibration.** Overall calibration hides tail errors. Validate by
  station × month × lead × **bracket distance from forecast mean** × liquidity ×
  time-of-day × spread. Track center / near-the-money / far-OTM-wing / extreme-tail
  separately — most mass (and the good overall Brier) sits at center, but edges live
  in the tails.
- **Sample-size-aware hierarchical shrinkage.** global → station → station-month →
  station-month-lead residual distributions, shrinking sparse buckets toward parents
  so noisy buckets don't create false confidence.
- **Add log loss + sharpness + calibration slope/intercept**, not just Brier/ECE —
  log loss punishes the exact overconfidence we worry about at lead≥1.

## Phase 3 — Intraday alpha (where the hypothesis says the edge is)

- **High-so-far conditioning - WIRED 2026-06-03.** The thesis is retail
  over-extrapolates *morning* temps. `weather.distribution` now prices KXHIGH
  brackets from a terminal daily-high distribution, and both `live_paper.py` and
  `weather_kxhigh_paper.py` can condition same-day KXHIGH on an
  `open_meteo_hourly_proxy` high-so-far lower bound. This is still not the
  official NWS settlement print; it is a read-only intraday proxy that must be
  validated by CLV and settlement.
- **Forecast-update reaction tracking.** Log `previous_fair`, `new_fair`,
  `fair_delta`, `forecast_run_time`, and the market's repricing lag. Tests whether
  any edge is "be faster than the market after a forecast update" — and whether that
  latency is capturable over a REST/WS path (the tennis latency lesson).
- **Regime flags.** cold front / heat wave / storm / large intraday swing / high
  wind / cloud-cover / coastal sea-breeze. Track calibration & PnL by regime —
  critical for MIA (sea breeze, storms) and CHI (lake effect, fronts).

## Phase 4 — Market integration & honest validation

**Validation metrics (replace "is it calibrated?" with "does it beat the market?"):**
EV vs market · realized PnL after fees · **CLV (closing_mid − fill_price)** ·
fill-adjusted edge decay · hit rate · avg edge captured · max drawdown — **broken
out by station, lead, bracket, liquidity bucket, and execution mode.**

- **CLV as the fast intermediate proof.** Settled PnL is the final word but slow and
  noisy; CLV is a faster diagnostic. Per paper trade record `model_price`,
  `market_mid_at_entry`, `fill_price`, `market_mid_near_close`, `settlement`,
  `realized_pnl`. **Positive predicted edge but negative CLV ⇒ the edge is fake.**
  Implementation exists; now run the recorder/settler on a schedule until there is
  enough evidence by station/bracket/side.
- **Edge-decay after signal:** market price at entry, +1/+5/+15/+30 min, near close,
  settlement → classify fast alpha vs slow alpha vs stale-book artifact vs noise.
- **Market shrinkage / anchoring.** When model disagrees with the liquid market by
  >σ, treat it as model error (the harness already flags this). Formalize: trade
  only the residual and shrink toward market (mirrors tennis market-anchored).
- **Benchmark ladder** — every change must beat the simpler option on EV/PnL/CLV:
  market-mid-only · raw Open-Meteo · calibrated-deterministic (current) · ensemble ·
  intraday-conditioned · market-shrunk. Stops complexity that doesn't help tradability.

**Execution-quality gates (the apparent backtest edge depends on fills):**
- **Stale/illiquid-book filter:** min bid/ask size, max spread, max quote age, min
  recent trade activity, min depth. Distinguish real edge from stale quote / wide-
  spread artifact / one-contract bait (`fair 0.62 vs ask 0.48` is usually a stale
  tiny ask).
- **Adverse-selection check (passive only):** log posted price, fill time, mid
  before/after fill, settlement. If passive fills cluster right before the mid moves
  against you, you're being picked off — validate maker separately from taker.
- **Execution-mode attribution:** never pool passive-maker / taker / passive-mid /
  laddered / re-entry; a model can be +EV as taker and −EV as maker.
- **Fee & slippage stress:** 0 bps · actual fee (`0.07·p·(1−p)`) · +1 tick · +2 tick
  · half-size fills · worst-side spread. A real edge survives conservative fills.
- **Confidence-tiered sizing:** Tier-1 (lead0, liquid, tight, well-calibrated) full
  Kelly; Tier-2 (lead1, medium) reduced; Tier-3 (tail / sparse bucket) observe-only.

## Phase 5 — Deployment

- **Wire the live signal producer.** The strategy is rules-mode: it consumes
  `ExternalSignalEvent.payload["implied_prob"]`. Stand up a producer that runs the
  KXHIGH calibration live and emits that event (the paper harness computes the
  pricing but isn't wired as a producer). Confirm Python/Rust parity after any
  producer payload or decision-gate change.
- **Fix passive-path `fair_price` metadata.** Only `_taker_decision`→`_place_order`
  emits `fair_price` (4dp ✓); the passive `PlaceOrder` omits it. If the risk gate
  requires `fair_price` (it rejected tennis with `InvalidNumeric`), `passive_mid`
  mode is silently blocked. Emit `_fair_price_4dp` in both paths.
- **Kill-switch framework (define before capital):** pause station if realized PnL <
  −X over N trades; pause bracket if CLV negative over N; **halt all on settlement-
  source mismatch**; pause lead bucket if ECE > threshold; pause on spread >
  max_spread, stale forecast data, or unavailable official station data.
- **Expand stations only after positive settled evidence** on the current three.

## Biggest missing pieces (priority order)

1. ~~Settlement-source GHCND-vs-CLI reconciliation~~ — DONE (PASS, 810/810 brackets)
2. CLV tracking loop implementation — DONE; settled PnL/CLV evidence across many
   trades is now the #1 open item
3. Liquidity / stale-book filters
4. Bracket-level (tail) calibration + log loss
5. Adverse-selection analysis (passive)
6. Official station high-so-far validation; Open-Meteo high-so-far proxy is wired
   but not yet proven
7. Lead-aware σ (+ ensemble spread)
8. Market shrinkage

## Latest 2026-06-03 high-so-far distribution pass

- Code path: `weather.distribution` is now importable without pulling in the
  heavyweight `eventcontracts.research` package, avoiding a script/import cycle.
- Live signal producer: `_kxhigh_external_signal` now emits distribution metadata:
  `distribution_method`, `distribution_feature_hash`, `high_so_far_f`,
  `high_so_far_source`, and `latent_expected_high_f`.
- Paper recorder: `weather_kxhigh_paper.py` prices every bracket through
  `DailyHighDistribution`; recorded rows include the same distribution metadata.
- Read-only command:
  `.venv\Scripts\python.exe python\scripts\weather_kxhigh_paper.py --record live-test\weather-kxhigh-distribution-ledger.jsonl --size 5`
  - exit 0.
  - `KXHIGHNY`, `KXHIGHCHI`, and `KXHIGHMIA` same-day markets were effectively
    one-sided after high-so-far conditioning.
  - Future-day apparent gaps remained gated as lead=1 overconfidence or
    model/market center disagreement.
  - Recorded paper entries: `0`; `live-test/weather-kxhigh-distribution-ledger.jsonl`
    is empty.
  - Decision: not deployable; continue read-only capture and settlement/CLV
    validation only.

Verification:

- `.venv\Scripts\python.exe -m pytest python\tests\test_weather_kxhigh.py python\tests\test_weather_distribution.py -q`
  - exit 0, 22 passed.
- `.venv\Scripts\python.exe -m ruff check python\src\eventcontracts\weather\distribution.py python\src\eventcontracts\cli\live_paper.py python\scripts\weather_kxhigh_paper.py python\tests\test_weather_kxhigh.py`
  - exit 0, clean.
- `.venv\Scripts\python.exe -m mypy --no-incremental python\src\eventcontracts\weather\distribution.py python\src\eventcontracts\cli\live_paper.py python\scripts\weather_kxhigh_paper.py python\tests\test_weather_kxhigh.py`
  - exit 0, clean.
- `powershell -ExecutionPolicy Bypass -File scripts\verify-eventcontracts.ps1 -PythonTests python\tests\test_weather_kxhigh.py,python\tests\test_weather_distribution.py -RuffTargets python\src\eventcontracts\weather\distribution.py,python\src\eventcontracts\cli\live_paper.py,python\scripts\weather_kxhigh_paper.py,python\tests\test_weather_kxhigh.py -MypyTargets python\src\eventcontracts\weather\distribution.py,python\src\eventcontracts\cli\live_paper.py,python\scripts\weather_kxhigh_paper.py,python\tests\test_weather_kxhigh.py`
  - exit 0, 22 pytest passed, ruff clean, mypy clean.
- `powershell -ExecutionPolicy Bypass -File scripts\check-dangerous-actions.ps1 -Path python\src\eventcontracts\weather\distribution.py,python\src\eventcontracts\cli\live_paper.py,python\scripts\weather_kxhigh_paper.py,python\tests\test_weather_kxhigh.py,docs\weather-kxhigh-validation-and-edge-spec.md,live-test\weather-kxhigh-distribution-ledger.jsonl`
  - exit 0, dangerous action scan clean.

**Strongest recommendation:** prove the current 3-station universe has real market
edge — after settlement, fees, spread, stale-book filtering, and adverse selection —
*before* any model elaboration or station expansion.
