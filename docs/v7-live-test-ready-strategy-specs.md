# v7 — Live-Test-Ready Strategy Specs (Top Non-Latency 6)

Date: 2026-06-02

## Context

The edge map ranked where a non-latency prediction model is most profitable on
Kalshi; [`docs/kalshi-strategy-latency-playbook.md`](kalshi-strategy-latency-playbook.md)
ranked the repo's strategies and gave per-strategy promotion gates. This doc
packages the **top 6 non-latency strategies** as *live-test-ready* specs: each has
runnable strategy + sleeve TOMLs, a registered runtime, the exact run command, and
a promotion-gate checklist. The goal is to start **dry-run-live / live-paper capture
immediately** — no orders, no new Rust.

Scope: **docs + runnable configs only**. Latency-bound families (BTC settlement,
microstructure OBI/queue, golf hole-by-hole, in-play) are out — see
[§ Excluded](#excluded-latency-bound-families).

The one law behind the ranking (paid for by the tennis closing-line wall and the
BTC arb finding): **a prediction model only has edge where no faster/sharper
reference already prices the market.** All six below are chosen for the *absence*
of a sharp reference, which is also why ~100 ms execution is irrelevant to them.

---

## Runnability matrix (honest status)

"Runnable" = a registered runtime exists and emits correct decisions (verified by
`on_event` smoke + `validate-config`). "Live decisions today" = the live-paper
runner already produces this strategy's trigger events end-to-end.

| # | Strategy | Runtime (`name`) | Config | Live decisions on `ec live-paper` today | Remaining to first live decision |
|---|----------|------------------|--------|------------------------------------------|----------------------------------|
| 1 | Weather KXHIGH | `weather_temperature_arbitrage` | `weather-temperature-arbitrage.toml` | **Yes** — Open-Meteo producer is built into live-paper | none (it runs now) |
| 2a | Box office | `entertainment_box_office` | `entertainment-box-office.toml` | quotes only | wire the Fandango/TMDB signal producer |
| 2b | Awards | `external_edge` | `entertainment-awards.toml` | quotes only | wire the `awards-model` signal producer |
| 3 | Tennis sharp-ref | `sports_tennis_xgboost` | `sports-tennis-xgboost.toml` | via the tennis automation pipeline (snapshot+odds), not generic live-paper | merge a sharp-odds feed (already specced) |
| 4 | Macro CDF ladders | `macro_cpi_predictor` / `macro_fed_gnn` / `macro_nfp_absorber` | `macro-*.toml` | quotes/timer only | wire the nowcast producer |
| 5 | Equity range ladders | `external_edge` | `equity-index-range-ladder.toml` | quotes only | wire the `equity-terminal-dist` producer |
| 6 | Intra-Kalshi no-arb | `kalshi_noarb_scanner` | `kalshi-noarb-scanner.toml` | **Yes** — quote-only, no producer needed | replace placeholder bracket tickers with live ones |

**Bottom line:** Weather (full) and the No-Arb scanner (quote-only) run end-to-end
on live data **today**. The four signal-driven sleeves are runnable runtimes whose
**only** remaining blocker is an external-signal *producer* — exactly the "no mature
data pipeline yet" gap the playbook called out. Each spec below states its producer
contract so that work is unambiguous.

---

## Cross-cutting contracts

### The run ladder (every sleeve climbs it in order)

1. **Schema gate** — `python -m eventcontracts.cli validate-config strategy <toml>`
   and `... sleeve <toml>`. (All v7 configs pass.)
2. **Decision smoke** — feed synthetic/fixture events through `on_event`; confirm
   it emits the intended `PlaceOrder`/`NoAction` (done for the new runtimes).
3. **Live-paper capture** — `ec live-paper` (below): live Kalshi WS → strategy →
   risk gate → `DryRunGateway`. **Records decisions; places no orders.**
4. **Risk-approval smoke (V6-S2)** — once parity cases exist, replay them through
   the real runner+risk gate (`strategy_smoke`) and assert ≥1 risk-APPROVED intent
   (catches the silent "every order rejected" failure parity misses).
5. **Parity** — generate `(event_id, decision)` cases, diff Python vs Rust
   (`make parity-check`). Required before any non-paper sleeve.
6. **Dry-run-live on the Rust hot path** — `eventcontracts-live-runner` (below),
   for archetyped strategies.
7. **Micro-live (outside this workspace)** — `--live-submit`, tiny cap, only after
   the promotion gate passes. **Never run in this workspace.**

### Commands

Universal paper capture (live data, no orders):

```bash
python -m eventcontracts.cli live-paper \
  --strategy configs/strategies/<name>.toml \
  --sleeve   configs/sleeves/<name>-paper-a.toml \
  --out      runs/<name>-$(date +%Y%m%dT%H%M%SZ) \
  --patterns "<GLOB1>,<GLOB2>" \
  --max-duration-seconds 3600
```

Rust dry-run-live (archetyped strategies — `external_edge`, `threshold`; dry-run is
the default, `--live-submit` is NOT passed):

```bash
cargo run -p eventcontracts-live-runner --release -- \
  --strategy-spec configs/strategies/<name>.toml \
  --sleeve-spec   configs/sleeves/<name>-paper-a.toml \
  --pattern <PREFIX> --max-markets 5 --duration-secs 60
```

Gates: `make verify-strategy <name>` (live-promotability: parity + Rust archetype),
`make parity-check`.

> Auth: live-paper / dry-run-live need Kalshi **read** creds in `.env`
> (`KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH`, `KALSHI_ENV`). Without them, stop
> at the schema gate + decision smoke.

### CLV / paper ledger schema (the v7 promotion currency)

There is **no sharp closing line** in these markets, so CLV is measured against the
**Kalshi mid**, not a sharp book. `live-paper` writes `decisions.jsonl`,
`risk_verdicts.jsonl`, and (where applicable) `external_signals.jsonl` to `--out`;
the promotion ledger adds, per entry:

| Field | Meaning |
|---|---|
| `entry_mid`, `model_fair`, `entry_fill` | mid at decision, model fair value, executed price |
| `mid_t+1m / +5m / +15m / +30m` | mid drift after entry (realized CLV proxy) |
| `near_close_mid`, `settlement` | mid just before resolution, terminal outcome |
| `fees` | Kalshi `0.07·P·(1−P)` per contract (max at 0.5, tiny at the tails) |
| `pnl_after_fees` | settled PnL net of fees |
| `exec_mode` | maker / taker / passive-mid / IOC — attributed, not assumed |

### Promotion gate (every sleeve; "no graduation on calibration alone")

1. market universe + exact resolution rules; 2. data sources + source-latency +
failure behavior; 3. feature schema + nullability; 4. label construction +
censoring; 5. backtest with the real fee curve; 6. paper replay on real
quote/order-book data; 7. positive **CLV vs Kalshi mid** + settled PnL; 8. exec-mode
attribution; 9. capacity from real top-of-book depth; 10. kill switches + exposure
caps. Plus **green parity** before tiny-live.

### Capital policy (allocate by realized fill quality, never by thesis)

| Stage | Capital | Rule |
|---|---:|---|
| Research / Paper | $0 | record + simulate only |
| Micro-live (outside workspace) | $500–$2k | validate venue semantics, fills, cancels |
| Pilot | $5k–$25k | only if paper CLV + settled PnL are positive |
| Sleeve | $25k–$250k | only with per-sleeve drawdown + capacity proof |

### Known pre-promotion blockers (gate live, not paper)

From the playbook: mypy (12 errors), `cargo fmt` drift in
`rust/crates/runner/src/lib.rs`, `cargo clippy` large-enum in
`rust/crates/live-runner/src/main.rs`. Flagged here, fixed before live promotion.

---

## The uniform spec template

Each strategy below uses the same 10 sections: **1** Identity & wiring · **2** Edge
thesis & latency class · **3** Market universe & resolution rules · **4** Data
sources & failure behavior · **5** Feature/label/null contract · **6**
Policy/sizing/execution priority · **7** Risk caps & kill switches · **8** CLV
ledger focus · **9** Promotion gate (instantiated) · **10** Run command.

---

## 1. Weather — Temperature (KXHIGH)

1. **Identity** — `weather-temperature_arbitrage-v1` / `weather_temperature_arbitrage`
   / v1.0.0 · sleeves `weather-kalshi-paper-a`, `weather-kalshi-live-a` · Rust
   `threshold`/weather archetype. Patterns `KXHIGH*`, `KXTEMP*`, `WX-*`.
2. **Edge & latency** — public NOAA/HRRR/NBM ensembles the crowd under-uses; the
   KXHIGH model beat NOAA on Brier (walk-forward PASS). No sharp book. Half-life
   minutes-to-hours → relaxed/standard tier; sub-2ms irrelevant.
3. **Resolution** — Kalshi daily-high brackets settle to the official station
   ASOS/observed daily max. **Ambiguity checklist:** station-source mismatch,
   late observation, sensor outage.
4. **Data & failure** — Open-Meteo (HRRR/NBM, free) → `ExternalSignalEvent`;
   `max_signal_age_seconds=180` → stale forecast forces NoAction. NWS/NOAA CDO as
   cross-checks.
5. **Feature/label/null** — `[mid_implied, model_prob, temp_delta_to_threshold,
   high_so_far, ensemble_spread]`; missing forecast → `model_prob=null` → NoAction.
   Label = bracket hit at end of market day; censor pre-noon pauses / station
   offline > 2h.
6. **Policy/sizing/exec** — quote- and signal-triggered; `min_edge_bps=150`,
   `near_binary_min_edge_bps=600`; Kelly-fraction sizing capped by
   `max_ladder_*`/`max_trade_capital_fraction`; `taker_if_edge`; standard tier,
   `max_delay_ms=1000`.
7. **Risk & kills** — paper sleeve caps; **kills:** station settlement-source
   mismatch, negative CLV by bracket bucket, stale forecast, calibration drift.
8. **CLV focus** — by **station × bracket-distance × lead-time**; split maker vs
   taker (maker fills are adverse-selected).
9. **Promotion gate** — ≥100 paper candidates with positive post-fee/-spread CLV;
   positive settled PnL by station+bracket; no single station/regime explains all
   PnL.
10. **Run** — *runs today*:
    ```bash
    python -m eventcontracts.cli live-paper \
      --strategy configs/strategies/weather-temperature-arbitrage.toml \
      --sleeve   configs/sleeves/weather-kalshi-paper-a.toml \
      --out runs/weather-$(date +%Y%m%dT%H%M%SZ) \
      --patterns "KXHIGH*" --max-duration-seconds 3600
    ```
    Rust dry-run: same spec via `eventcontracts-live-runner --pattern KXHIGH`.

---

## 2. Entertainment — Box Office + Awards

**2a Box office** — `entertainment-box_office-v2` / `entertainment_box_office` ·
sleeve `entertainment-box-office-kalshi-paper-a` · patterns `KXBOXOFFICE*`,
`KXMOVIE*`.
**2b Awards** — `entertainment-awards-v1` / **`external_edge`** (generic archetype)
· sleeve `entertainment-awards-kalshi-paper-a` · patterns `KXOSCAR*`, `KXEMMY*`,
`KXGRAMMY*` *(confirm via discovery)*.

1. **Identity** — see above; awards runs on the shared `external_edge` runtime
   (Python `plugins/strategies/external_edge.py` + Rust archetype fallback).
2. **Edge & latency** — **no sharp reference anywhere**; softest crowd, highest
   edge density. Awards is now liquid (Oscars ~$48M in 2026). Resolves over
   days-to-weeks → relaxed tier; ~100ms wholly irrelevant.
3. **Resolution** — box office: weekend domestic gross threshold (official studio
   numbers). Awards: named winner per category. **Ambiguity:** revised gross,
   category renames, tie rules — avoid ambiguous-resolution markets.
4. **Data & failure** — box office: Fandango/AMC seat-occupancy + ticket velocity
   (Apify) + TMDB. Awards: an `awards-model` producer publishing per-nominee YES
   probability from precursor awards / critics aggregates → `ExternalSignalEvent`
   `{market_id, probability, confidence}`. Missing/low-confidence signal → NoAction.
5. **Feature/null** — box office: `[seat_occupancy_pct, ticket_velocity_per_hour]`
   → implied gross → prob. Awards `external_edge`: producer supplies `probability`
   directly; payload missing prob → NoAction; prob ∉ [0,1] → censored.
6. **Policy/sizing/exec** — buy YES/NO when `|prob − mid|·1e4 ≥ min_edge_bps`
   (box office 500, awards 400) and `confidence ≥ confidence_floor` (0.80 / 0.70);
   fixed `size`; relaxed tier, signal-triggered. Fee is tiny on award longshots →
   bias to away-from-0.5.
7. **Risk & kills** — small paper caps (order $100 / gross $500 for awards);
   **kills:** signal staleness, source outage on release night, resolution-rule
   change.
8. **CLV focus** — vs Kalshi mid at entry → settlement; per category / per film;
   maker vs taker.
9. **Promotion gate** — positive CLV after the official/source timestamp;
   settlement-ambiguity checklist clean; no single event drives all PnL.
10. **Run** — quotes flow today; live decisions need the producer. Paper capture:
    ```bash
    python -m eventcontracts.cli live-paper \
      --strategy configs/strategies/entertainment-awards.toml \
      --sleeve   configs/sleeves/entertainment-awards-kalshi-paper-a.toml \
      --out runs/awards-$(date +%Y%m%dT%H%M%SZ) \
      --patterns "KXOSCAR*,KXEMMY*,KXGRAMMY*" --max-duration-seconds 3600
    ```
    Awards also dry-runs on the Rust `external_edge` archetype with the same spec.

---

## 3. Tennis — Sharp-Reference Repricing

1. **Identity** — `sports-tennis-xgboost-v1` / `sports_tennis_xgboost` · sleeves
   `sports-tennis-kalshi-paper-a`, `...-live-a` · patterns `*TENNIS*`, `*ATP*`.
2. **Edge & latency** — **re-spec:** fair value is the **de-vigged sharp consensus**
   (Pinnacle or a robust multi-book blend); the XGBoost model is a *residual*, not
   primary alpha — the standalone winner model is dominated by the closing line.
   Pre-match half-life seconds-to-minutes → relaxed; news shocks 100–500ms help but
   source quality dominates.
3. **Resolution** — match winner per Kalshi rules (retirement handling matters).
4. **Data & failure** — Kalshi tennis quotes + a **sharp-odds feed merged into the
   scored snapshot**. `require_odds_present=true` makes the sleeve a **no-op unless
   real bookmaker odds are present** (both players, decimal > 1.0) — missing odds
   fail loud, by design.
5. **Feature/null** — v2 34-feature vector (Elo blend, serve/return, fatigue, form,
   **odds block**); `feature_schema_version="2"` must match the promoted bundle.
6. **Policy/sizing/exec** — buy when model/sharp fair − mid ≥ `min_edge_bps=250`
   and `min_model_confidence=0.62`; `trailing_stop_loss=0.12` is drawdown control,
   not alpha; relaxed tier.
7. **Risk & kills** — committed live sleeve is an $8 first-live envelope; **kills:**
   odds-source absence, schema-version mismatch, model-feature leakage.
8. **CLV focus** — **CLV vs the sharp close at the actual Kalshi fill price**; split
   by odds-source presence, tournament level, surface, liquidity bucket.
9. **Promotion gate** — positive CLV vs sharp close at real fills; no feature
   leakage / schema mismatch; performance separable by the buckets above.
10. **Run** — via the tennis automation pipeline (snapshot + merged odds), not the
    generic live-paper. See [`docs/runbooks/tennis-automation-pipeline.md`](runbooks/tennis-automation-pipeline.md).

---

## 4. Macro — CDF Ladders (CPI / Fed / NFP)

1. **Identity** — `macro_cpi_predictor` (`KXCPI*`,`KXINFLATION*`), `macro_fed_gnn`
   (`KXFED*`), `macro_nfp_absorber` (`KXNFP*`,`KXU3*`); paper sleeves
   `macro-{cpi,fed,nfp}-kalshi-paper-a`.
2. **Edge & latency** — price the **whole bracket set as one implied distribution**,
   not independent binaries; edge lives in the **tails/bins** vs a calibrated
   nowcast, on **second-tier** releases pros ignore. **Pre-release only** (release
   sniping is a latency game and is excluded). Headline CPI/Fed/S&P are efficient —
   stay off them.
3. **Resolution** — official BLS/BEA/FOMC print per bracket; settle to the released
   number. Censor on delayed releases / mid-window amendments.
4. **Data & failure** — Cleveland Fed nowcast + component alt-data (Truflation,
   Apify retail) + CME FedWatch (Fed); FRED baselines. Producer publishes per-bracket
   probabilities. The **joint-CDF / no-arb** enforcement (`ladder_no_arb=true`,
   declared in `macro-cpi-predictor.toml`) is the **deferred Rust-ladder-archetype**
   step; the **per-bracket** path runs today.
5. **Feature/null** — `[alt_data_mom, cleveland_fed_nowcast, kalshi_implied_mean]`;
   > 5% missing components → drop. Label = bracket hit at release.
6. **Policy/sizing/exec** — shift implied mean → reprice brackets; trade when shift
   clears `min_shift_bps`/`min_edge_bps`; relaxed tier. NFP absorber is the
   complementary **release-day spread-widener** (maker management), not a CDF model.
7. **Risk & kills** — severe event concentration → hard per-release cap; **kills:**
   release delay, nowcast feed gap, post-release timestamp leakage.
8. **CLV focus** — pre-release entry vs mid → settlement; per release, per bracket;
   **ladder no-arb** monotonicity as a separate clean signal.
9. **Promotion gate** — walk-forward event study over many releases; real
   entry-price replay (not closing candles); no post-release leakage; stress vs
   one-tick / two-tick / no-fill.
10. **Run** — quotes/timer flow today; live decisions need the nowcast producer.
    Paper capture e.g. `--strategy configs/strategies/macro-cpi-predictor.toml
    --sleeve configs/sleeves/macro-cpi-kalshi-paper-a.toml --patterns "KXCPI*"`.

---

## 5. Equity Index — Daily Range Ladders

1. **Identity** — `equity-index-range-ladder-v1` / **`external_edge`** · sleeve
   `equity-index-kalshi-paper-a` · patterns `KXINX*`, `KXNASDAQ100*`, `KXSPX*`
   *(confirm via discovery)*.
2. **Edge & latency** — model all range contracts as **one terminal distribution**
   of the index close; the crowd prices the point, not the wings. Midday seconds /
   final minutes tighten — standard tier (`max_delay_ms=2000`), **not** a
   sub-100ms race (final-seconds auction sniping is excluded).
3. **Resolution** — official index close per range. No-arb: adjacent ranges sum
   sensibly, CDF monotone.
4. **Data & failure** — an `equity-terminal-dist` producer from ES/NQ futures +
   SPY/QQQ + intraday vol (Brownian bridge near close) publishing **per-bracket YES
   probability** → `external_edge`. Stale/missing → NoAction.
5. **Feature/null** — producer supplies `probability` per bracket; out-of-range →
   censored. Label = range hit at close.
6. **Policy/sizing/exec** — buy when `|prob − mid|·1e4 ≥ min_edge_bps=200`; fixed
   size; standard tier. Joint-CDF / no-arb ladder enforcement = deferred Rust
   archetype (per-bracket runs today).
7. **Risk & kills** — wide open-order headroom (many brackets), conservative gross;
   **kills:** vol-model divergence, stale futures feed, close-time drift.
8. **CLV focus** — by minute-to-close bucket; separate range-center vs wing
   behavior.
9. **Promotion gate** — real quote replay in the final 30 minutes; positive CLV by
   minute-to-close bucket; center vs wing separated.
10. **Run** — quotes flow today; live decisions need the producer. Paper:
    `--strategy configs/strategies/equity-index-range-ladder.toml --sleeve
    configs/sleeves/equity-index-kalshi-paper-a.toml --patterns "KXINX*,KXNASDAQ100*"`.
    Also dry-runs on the Rust `external_edge` archetype.

---

## 6. Intra-Kalshi No-Arb Scanner

1. **Identity** — `kalshi-noarb-scanner-v1` / `kalshi_noarb_scanner` · sleeve
   `noarb-scanner-kalshi-paper-a` · subscribes to the ladder family, filters to the
   configured `brackets`.
2. **Edge & latency** — **deterministic, no model**: related contracts must obey
   logical no-arbitrage; retail violates faster than it corrects. Logical edge, not
   a race → standard tier; the binding constraint is a fresh two-sided book on every
   leg.
3. **Resolution** — inherits each underlying market's rules; the scanner only needs
   the ladder grouping (`TICKER:lo:hi;…`).
4. **Data & failure** — Kalshi quotes only — **no external producer needed**.
   `max_quote_age_seconds=30` drops stale legs → incomplete ladder → NoAction.
5. **Constraints checked** — **exclusive** ladders: Σ YES-ask < 1 − fees ⇒ buy one
   YES on every bracket (risk-free $1 payout) → auto-emits N IOC legs. **cumulative**
   ladders: P(≥t) must be non-increasing; `ask(low) < bid(high)` ⇒ BUY low-YES +
   BUY high-NO — **flagged** (logged NoAction with the legs), because two-leg
   execution carries leg risk (see §7).
6. **Policy/sizing/exec** — act when fee-adjusted edge ≥ `min_edge_bps=100`; leg
   size = min(`size`, smallest available ask qty); IOC legs (fill-now-or-abandon).
7. **Risk & kills — headline blocker is LEG RISK:** legs are independent IOC orders,
   so a partial fill leaves an unhedged book. Atomic / two-leg execution is the
   promotion step. `arb` is a **reserved (unimplemented) Rust archetype** → runs on
   the Python path now.
8. **CLV focus** — realized lock capture vs the theoretical edge; partial-fill /
   leg-miss loss distribution.
9. **Promotion gate** — deterministic, so parity is the cleanest of the six; then
   **atomic leg execution** before anything past paper.
10. **Run** — *runs today (quote-only)* once placeholder tickers are replaced with
    live ones from discovery:
    ```bash
    python -m eventcontracts.cli live-paper \
      --strategy configs/strategies/kalshi-noarb-scanner.toml \
      --sleeve   configs/sleeves/noarb-scanner-kalshi-paper-a.toml \
      --out runs/noarb-$(date +%Y%m%dT%H%M%SZ) \
      --patterns "KXHIGH*" --max-duration-seconds 3600
    ```

---

## Excluded (latency-bound families)

Out of scope by the ~100ms constraint and the latency playbook — these reward
speed/arbitrage, not a prediction model: BTC/ETH 15-minute settlement, microstructure
OBI scalper / queue evader, golf hole-by-hole / cut-line in-play, and in-play tennis
(the Markov engine — real edge, but latency is the binding constraint).

## Appendix — what this pass added

- New runtimes: `external_edge` (generic prob-vs-mid; serves awards + equity),
  `kalshi_noarb_scanner` (deterministic no-arb).
- New configs: `entertainment-awards`, `equity-index-range-ladder`,
  `kalshi-noarb-scanner` (+ paper sleeves + parity stubs).
- Upgraded `research_doc` pointers + macro `ladder_no_arb`/`pre_release_only`
  declarations on existing configs.
- All configs pass `validate-config`; both new runtimes pass an `on_event` decision
  smoke. No new Rust; the `arb` and joint-CDF Rust archetypes are the named next
  promotion step.

### v7.1 — implemented from the brainstorm addendum

Added the **`ladder_cdf`** runtime (one latent distribution → coherent per-bracket
probabilities; the "full-ladder CDF engine") plus 7 strategy configs plundering the
existing runtimes: `commodity-brent-threshold-cdf`, `weather-ladder-cdf`,
`equity-close-range-cdf`, `macro-cpi-cdf` (all `ladder_cdf`);
`sports-sharp-lag-repricing`, `mlb-outright-residual` (both `external_edge`);
`range-ladder-noarb` (`kalshi_noarb_scanner` cumulative). Each has a paper sleeve +
parity stub; all `validate-config`-clean and `ladder_cdf` is ruff/mypy/`on_event`
clean. **Brent (`KXBRENTD`) is a genuinely new surface.** Full variant-by-variant
status (implemented / covered / overlaps / excluded / deferred) is in
[`docs/kalshi-strategy-brainstorm-addendum.md`](kalshi-strategy-brainstorm-addendum.md#implementation-status-2026-06-02).
