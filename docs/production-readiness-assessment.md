# Production-Readiness & Scalability Assessment

**Lens:** technical due-diligence as if a quant firm were acquiring and deploying
this system with real capital.
**Date:** 2026-06-03  **Reviewer:** Claude (automated)  **Scope:** whole repo
(`python/`, `rust/`, `configs/`, `contracts/`, `docs/`, `scripts/`).

This document (a) states a verdict, (b) enumerates *everything* I can find that
blocks live-capital deployment, ranked by severity, (c) assesses scalability, and
(d) lays out a logically-ordered implementation plan. The plan is then executed in
order; **Section 6** tracks live status.

---

## 0. Verdict

**Not deployable to live capital today. It is a strong research + paper stack with
a partially-built live execution spine and a genuinely differentiated
correctness/parity discipline.** As an acquisition: the *IP* is real (dual-language
parity harness, typed event lake, calibrated-vs-market edge philosophy, leakage
discipline). The *operational surface* needed to put money at risk — gateway
idempotency, OMS-as-truth, reconciliation, kill-switch, checkpoint/WAL, reconnect
recovery, observability, secrets isolation — is scaffolded but incomplete.

The team has already written the gating list itself:
[`docs/live-deployment-remaining-roadmap.md`](live-deployment-remaining-roadmap.md)
defines **12 non-negotiable live invariants** and **6 deployment stages**. The
honest status is **Stage 0→1** (paper research → no-trade live paper). Live order
placement is correctly hard-disabled (`AGENTS.md`, no `--live-submit`).

The single most important non-technical finding: **there is no proven edge yet.**
The weather ledger has 5 unsettled paper entries; tennis is market-anchored with no
demonstrated CLV. Per the team's own philosophy ("prove before expand"), *no amount
of engineering hardening creates a reason to deploy capital until a settled,
post-cost, market-beating edge exists.* An acquirer should price this as
"infrastructure + research platform," not "live alpha."

---

## 1. Methodology

Assessed against four axes: **(1) correctness & data integrity**, **(2) live
execution safety**, **(3) scalability/performance**, **(4) operability**. Evidence
is cited to files. Severity: **S0** (blocks any live use / capital-loss or
correctness risk), **S1** (blocks unattended/scaled live), **S2** (hardening /
tech-debt), **S3** (nice-to-have).

---

## 2. Findings — everything blocking production readiness

### A. Correctness & data integrity

| ID | Sev | Finding | Evidence | Status |
|----|-----|---------|----------|--------|
| A1 | S0 | Weather lead-time overconfidence leaked into the live path: the lead=0/miscalibration discipline lived only in the offline recorder, not the live producer → lead≥1 fake wing edges would trade. | `cli/live_paper.py` producer vs `scripts/weather_kxhigh_paper.py` | **FIXED** this session (producer lead=0 gate + strategy guard + tests) |
| A2 | S1 | No "no new trades near close" gate; producer ignored market `close_time`. | `_kxhigh_external_signal` | **FIXED** (contract `close_time` + producer suppress + strategy `min_seconds_to_close`, fresh-recompute) |
| A3 | S1 | Committed calibration artifact had no provenance (fit window, lead semantics, half-life) → not reproducible, not auditable for leakage. | `configs/weather/station_calibrations.json` | **FIXED** (file-level `_meta`, hash-safe; report-script stamps it; tests) |
| A4 | S0 | Producer external-signal payload omits a **top-level `market_id`**, which the Rust `parse_external` requires → the Rust runtime cannot parse the weather signal at all. | `rust/.../runner/src/lib.rs:parse_external` vs `_kxhigh_external_signal` payload | **IN PROGRESS** (P0) |
| A5 | S1 | Weather `weather_temperature_arbitrage` has a Rust twin but **no parity fixtures** → slips past the dual-language promotion gate entirely. | `contracts/parity/` (no dir); `test_promotion_manifests_require_nonempty_parity` | **IN PROGRESS** (P0: twin brought to parity incl. lead/close gates; fixtures + manifest pending) |
| A6 | S1 | Calibration is a trailing-window snapshot meant to be regenerated daily; there is no scheduled regeneration / staleness alarm. A stale fit silently misprices. | report-script docstring "regenerated each day" | OPEN |
| A7 | S2 | Settlement reconcile (GHCND-vs-CLI) is a manual script, not a wired pre-trade/kill check. | `scripts/weather_settlement_reconcile.py` | OPEN |
| A8 | S2 | No automated leakage/look-ahead test harness across strategies (each strategy re-derives discipline ad hoc). | repo-wide | OPEN |

### B. Live execution spine (roadmap invariants 5–10)

| ID | Sev | Finding | Evidence |
|----|-----|---------|----------|
| B1 | S0 | Python `live_paper` explicitly excludes **fill simulation, WS sequence-gap recovery on reconnect, persistent state checkpoints, multi-strategy/multi-sleeve**. These are precisely the unattended-live blockers. | `cli/live_paper.py` module docstring |
| B2 | S0 | Gateway idempotency, stale-intent rejection, rate-limit budgets, and final venue submit are roadmap-required but unproven end-to-end. The gateway crate exists but lacks recorded-fixture coverage for acks/rejects/partials/dup-COID/rate-limits (Stage 3 acceptance). | `rust/crates/gateway`; roadmap Stage 3 |
| B3 | S0 | **Operator kill-switch / cancel-all without code edits** (invariant 10) is not demonstrably wired into both the runner and a CLI. | roadmap invariant 10 |
| B4 | S1 | OMS-as-source-of-truth and ledger-as-truth (invariants 7–8) — crates exist (`oms`, …) but transition/settlement coverage and the runner↔OMS↔ledger wiring are not verified end-to-end. | `rust/crates/oms` |
| B5 | S1 | Reconciliation (local vs venue exposure; unexpected live orders/fills; fail-closed on 401/429/timeout/schema-change) is partially present (`live-runner/src/reconcile.rs`) but lacks the scheduled, durable, fail-closed Stage-2 acceptance tests. | `rust/crates/live-runner/src/reconcile.rs` |
| B6 | S1 | No write-ahead log / checkpoint recovery (invariant 11, Stage 5). A crash mid-session loses in-memory order/exposure state (`HashMap` in the strategy structs). | `runner/src/lib.rs` state is in-process `HashMap` |

### C. Risk engine

| ID | Sev | Finding | Evidence |
|----|-----|---------|----------|
| C1 | S0 | Dual risk-gate parity (runner-side + gateway-side, invariant 5) must be byte-aligned; divergence/dead checks are a known hazard ([[eventcontracts-dual-risk-gate-parity]]). No test asserts the two gates agree on a shared battery. | `rust/crates/risk`, `python/.../risk/policy.py` |
| C2 | S1 | Stale-data halt, provider-health halt, max-order-rate, max-cancel-rate (Workstream D) are specified, not verified. | roadmap Workstream D |
| C3 | S1 | Daily-loss ledger / kill-switch thresholds exist in spec form but lack an integration test that actually trips them and blocks dispatch. | roadmap Workstream D |

### D. Cross-language parity

| ID | Sev | Finding | Evidence |
|----|-----|---------|----------|
| D1 | S1 | Only a subset of strategies have parity fixtures; promotable strategies without them have no cross-language guarantee. | `contracts/parity/*` |
| D2 | S2 | Parity *is* run in CI via `make parity-check`, but that target was a **hardcoded list that omitted `weather_temperature_arbitrage`** despite its promoted manifest → silent rot for promoted-but-ungated strategies. | `Makefile` parity-check, `.github/workflows/quality.yml` | **FIXED** (P5): weather added to `parity-check`; new `test_promoted_manifests_are_gated_in_ci_parity_check` fails if any promoted manifest is not CI-gated |

### E. Observability & operability

| ID | Sev | Finding | Evidence |
|----|-----|---------|----------|
| E1 | S1 | No metrics/heartbeat/alerting surface (Prometheus/statsd/structured logs). Live runs print stderr snapshots; sequence-gap counters are not promoted to alerts (Workstream A). | `live_paper` stderr prints |
| E2 | S1 | No runbook-driven incident tooling / operator drills (Stage 4). | roadmap Stage 4 |
| E3 | S2 | Run manifests exist (good) but there's no data-quality report (gaps, reconnects, rejects, stale periods, depth stats) per run. | Workstream A acceptance |

### F. Security / secrets

| ID | Sev | Finding | Evidence |
|----|-----|---------|----------|
| F1 | S0 | Credentials live in `.env` / key files on the host; invariant 1 ("strategy code cannot access credentials, venue clients, storage writers, or gateway APIs directly") must be enforced by construction, not convention. | `AGENTS.md`, scripts read `.env` |
| F2 | S1 | No secret-rotation, no KMS/secrets-manager integration, no per-process credential scoping. | repo-wide |

### G. Scalability & performance — see Section 3.

### H. Testing & release engineering

| ID | Sev | Finding | Evidence |
|----|-----|---------|----------|
| H1 | S2 | CI (`quality.yml`) is actually fairly mature — `python` (compile/test/lint/typecheck), `rust` (`rust-check` + `parity-check` + bench-build), and `security` (pip-audit, cargo-audit, gitleaks, CycloneDX SBOM, Docker build). Gap was parity-list drift (D2, fixed). Remaining: a determinism replay-equality job (H2) and gating the no-trade smoke per promoted strategy. | `.github/workflows/quality.yml` |
| H2 | S2 | Determinism guarantee (invariant 11: fixed partitions replay deterministically) lacks a dedicated CI replay-equality job. | roadmap |
| H3 | S2 | Optional deps (`polars`) break full-suite collection locally; no documented canonical test entrypoint that's hermetic. | `pytest` collection errors on `test_tennis_*` |

### I. Capital / edge governance

| ID | Sev | Finding | Evidence |
|----|-----|---------|----------|
| I1 | S0 | **No proven, settled, post-cost, market-beating edge** on any sleeve. Deployment of capital is unjustified regardless of infra readiness. | weather ledger (5 unsettled); [[eventcontracts-edge-validation-philosophy]] |
| I2 | S1 | Promotion gate checks parity coverage but not "settled CLV/PnL evidence" — an operator could promote on green infra alone. | `strategy_tools.py` promotable |

---

## 3. Scalability assessment (acquirer lens)

**Concurrency model.** Live paper is a single-process `asyncio` loop (discovery,
forecast poll, WS stream, strategy loop) feeding one `StrategyRunner`. The Rust
hot-path (Stage 5) is the intended scale answer but is not the live path yet.

**State.** Per-strategy state is in-process `HashMap`/dict (open exposure, last
order, signal cache). This does not survive a restart (B6) and is not shareable
across processes → **no horizontal scale and no crash recovery** without the WAL/
checkpoint work.

**Throughput / latency.** The Rust `runtime-hot` projection is alloc-light for the
hot kinds (quote/trade/book) — good. But the live decision path today is Python;
per the team's own measurements network RTT (~33 ms Kalshi, ~34 ms Coinbase) is the
binding constraint, so latency-sensitive strategies are not viable until the Rust
runner + colocation are real (`AGENTS.md` BTC study). For the *non-latency* sleeves
(weather/entertainment) Python throughput is adequate at current market counts.

**Multi-sleeve / multi-venue.** `live_paper` is explicitly single-strategy/
single-sleeve. Scaling to N sleeves × M venues needs: an event multiplexer
(time+receipt ordering across sources), shared risk state (Workstream D), per-sleeve
isolation, and a supervisor. None exist yet.

**Capacity realism.** Depth is the real scaling ceiling for event contracts: live
probes showed median top-of-book depth **0** with thin tails on overnight
`KXTEMPNYCH`. Backtest PnL scales linearly with the synthetic depth cap (10→250),
i.e. *reported* edge is dominated by an unvalidated capacity assumption. Capacity
curves by family/side/price-bucket/time-to-close (Workstream C) are required before
any size claim is credible.

**Verdict on scale:** the architecture *can* scale (clean crate boundaries, typed
event lake, Rust hot-path design), but the scaling primitives (shared state,
checkpoint, multiplexer, capacity profiles, supervisor) are unbuilt. Today it is a
single-box, single-sleeve, restart-loses-state system.

---

## 4. Implementation plan (logically ordered)

Ordering principle: **correctness before safety before scale**, and never build
scale on an unproven edge. Phases map to roadmap stages.

- **P0 — Correctness & cross-language integrity (this session).**
  A1✅ A2✅ A3✅ + A4 (producer `market_id`) + A5 (weather Rust twin → byte-exact
  parity fixtures + promotion manifest). Acceptance: pytest/ruff/mypy green, `cargo
  test` green, Rust `parity_check` PASS, Python no-trade smoke ≥1 approved intent,
  danger scan clean.
- **P1 — No-trade live-paper hardening (Stage 1).** Stale-signal & stale-book
  refusal as explicit, tested NoAction; WS reconnect + sequence-gap counters →
  alerts; data-quality report per run; operator-halt stops new decisions. Calibration
  staleness alarm (A6).
- **P2 — Reconciliation, fail-closed (Stage 2).** Scheduled local-vs-venue exposure
  reconcile; fail-closed on 401/429/timeout/schema-change; durable reports; alert on
  unowned venue orders/positions. Harden `live-runner/src/reconcile.rs` + tests.
- **P3 — Risk engine completeness (Workstream D).** Dual-gate parity battery (C1);
  kill-switch + stale-data/provider-health halts + max order/cancel rate, each with a
  trip-and-block integration test.
- **P4 — Gateway & OMS truth (Stage 3 prerequisites).** Idempotency store;
  stale-intent rejection; recorded-fixture tests for ack/reject/partial/dup-COID/
  rate-limit; OMS transitions as truth; ledger entries. Operator cancel-all CLI (B3).
- **P5 — Observability, CI, determinism (H/E).** Metrics+heartbeat+alerting;
  CI runs full Rust workspace tests + `parity_check` + no-trade smoke + a determinism
  replay-equality job; secrets isolation hardening (F).
- **P6 — Scalability primitives (Stage 5).** WAL/checkpoint recovery; event
  multiplexer; multi-sleeve supervisor; Rust hot-path live runner once parity is
  broad. Capacity profiles (Workstream C) feeding sizing.
- **P7 — Capital/edge governance (gates capital, runs in parallel).** Settled CLV/
  PnL evidence loop per sleeve; promotion gate requires settled post-cost edge, not
  just green infra (I2). **This, not infra, is what authorizes capital.**

Each phase is independently shippable and leaves the tree green.

---

## 5. Acceptance definition of "deployment ready"

"Deployment ready" here means **ready to run the Stage-1 no-trade live-paper stack
unattended, and to pass the Stage-2/3 gates that precede any tiny-live order** — NOT
"authorized to trade size." Capital authorization is gated separately by P7 (proven
edge). This assessment and the P0 tranche move the system to a clean, parity-locked,
tested Stage-0→1 baseline; P1–P6 are the enumerated path to unattended live paper and
tiny-live; P7 is the capital gate.

---

## 6. Execution status (updated as work lands)

- **P0 — COMPLETE & TESTED.**
  - A1 (lead-time gate) ✅ — live producer emits lead=0 only; strategy defense-in-depth guard; tests.
  - A2 (near-close gate) ✅ — `close_time` on the contract; producer suppresses closed + tags `close_time`/`seconds_to_close`; strategy `min_seconds_to_close` (fresh recompute); live spec = 300s.
  - A3 (calibration provenance) ✅ — file-level `_meta` (hash-safe), report-script stamp, JSON backfill, round-trip test.
  - A4 (top-level `market_id`) ✅ — producer payload now carries `market_id`; test-locked.
  - A5 (weather Rust twin parity) ✅ — Rust twin extended with `lead_days`/`close_time` on `ExternalProbability` + lead & close gates; **3/3 byte-exact `parity_check` PASS**; **Python no-trade smoke = 1 risk-approved intent**; promotion manifest added → `weather_temperature_arbitrage` is now dual-language parity-covered and passes the promotion gate.
  - **Verification:** Python 79 passed / ruff clean / mypy clean on changed files; Rust runner+parity+live-runner `cargo test` green (44 lib + 18 + …); clippy clean; danger scan clean for changes.
- **P5 (CI parity drift) — DONE & TESTED.** `weather_temperature_arbitrage` added to
  the `make parity-check` CI target; new `test_promoted_manifests_are_gated_in_ci_parity_check`
  fails if any promoted manifest is not CI-gated (prevents future rot). 4/4 promotion
  guards pass.
- **P1 (calibration-staleness alarm A6) — DONE & TESTED.** `_calibration_staleness_warning`
  warns at live-runner startup when the KXHIGH fit is missing provenance or older than 7d
  (a stale trailing-window fit silently misprices); unit-tested for fresh/stale/missing/
  unparsable. (The committed artifact has `generated_at: null` → it correctly warns until
  regenerated.)
- **Regression after all of the above:** 522 Python tests pass / 16 skipped (0 fail);
  Rust runner+parity+live-runner `cargo test` green; clippy clean; ruff/mypy clean on changed
  files; Rust `parity_check` 3/3 PASS; Python no-trade smoke = 1 approved.
- **P2, P3, P4, P6, P7 — NOT STARTED (honest).** These are the team's own multi-stage
  roadmap (reconciliation fail-closed, kill-switch/rate-limit risk battery, gateway
  idempotency + OMS truth + recorded venue-fixture tests, WAL/checkpoint + multiplexer +
  multi-sleeve scaling, and the **proven-edge capital gate**). Each is a multi-PR workstream
  measured in weeks, several requiring live venue fixtures and operator drills; they cannot
  be honestly completed in one pass. **Capital authorization is gated on P7 (settled,
  post-cost, market-beating edge), which no sleeve currently has — this, not infrastructure,
  is the binding constraint on deploying money.**

---

## 7. Scaling considerations (expanded)

Section 3 sketched scale; this section is the full surface a quant firm must engineer
before this runs as a *fleet*, not a single box. The governing insight for **event
contracts specifically** is at the top because it inverts the usual scaling instinct.

### 7.0 The scaling axis is *breadth of uncorrelated edges*, not depth of capital

Event-contract books are thin (live probes: top-of-book depth **median 0** on overnight
`KXTEMPNYCH`). You cannot scale capital by sizing up a market — you move the book and eat
your own edge. **You scale by adding many small, uncorrelated edges**, each capped at its
market's realized capacity. Three consequences that drive the whole architecture:

1. **Capacity-bounded sizing, per market.** Sizing must be driven by *observed* depth
   profiles (Workstream C), never a static `--synthetic-candle-depth`. The unit of scale is
   "another sleeve," not "more contracts."
2. **Correlation is the real risk, not size.** "Uncorrelated" is the load-bearing word and
   it is mostly false by default here: every `KXHIGH*` settles on the **same NWS data
   source**; all weather across cities correlates under a heat dome; same-tournament tennis
   legs correlate; same-event ladders are mutually exclusive (a built-in correlation of −1).
   A single NWS outage or a model regime break hits *every* weather position at once.
   Portfolio risk must model **settlement-source, event, and regime correlation** — not treat
   markets as independent (see 7.4).
3. **Throughput scales faster than capital.** Adding breadth multiplies *market-data* load
   (subscriptions, ticks, settlements) far faster than it multiplies notional. The system
   gets data-bound and ops-bound long before it gets capital-bound. Plan compute/observability
   for 10–100× the instruments at roughly flat AUM.

### 7.1 Data-plane throughput & backpressure  *(attaches to P6, Workstream A)*

- **Current break:** the live path is one Python `asyncio` loop with an `asyncio.Queue`
  between the WS stream and the strategy ([`cli/live_paper.py`]). At thousands of markets ×
  orderbook-delta rate, Python won't keep up and the queue grows unbounded → memory blowup.
- **Required:** the Rust hot-path (`runtime-hot` is already alloc-light) as the *live* path;
  **bounded queues with per-instrument quote coalescing** (latest-top-of-book wins — safe for
  event contracts, which only need the current touch) so a slow consumer sheds staleness
  instead of memory; subscription **sharding across multiple WS connections** (Kalshi caps
  tickers/connection) with a connection supervisor.
- **Event multiplexing:** merge market-data + external signals by **event time with
  receipt-time watermarks** so determinism (invariant 11) survives out-of-order, multi-source
  delivery. Dedup on `(source, sequence)`.

### 7.2 State scale & consistency  *(attaches to P6; invariants 7–8, 11)*

- **Current break:** per-strategy state is in-process `HashMap`/dict (open exposure, last
  order, signal cache) — lost on restart, unshareable, unbounded as markets accumulate.
- **Required:** **WAL + periodic checkpoints** for crash recovery; state **partitioned by
  sleeve/market** so per-shard memory is bounded; **eviction** of settled/closed markets
  (daily contracts churn fast — without eviction the maps grow forever); OMS/ledger as the
  durable source of truth rather than the strategy's RAM.
- **Settlement fan-out:** thousands of daily markets settle in the **same minute** (NWS print,
  market close). Naïve per-market settlement + GHCND reconcile = a thundering herd on NOAA and
  the venue. Need **batched/staggered settlement** and rate-limited reconcile.

### 7.3 Horizontal compute & partitioning  *(attaches to P6)*

- **Current:** single process, single sleeve. Horizontal scale needs a partition scheme
  (which markets/strategies on which worker), the existing `bus` crate for event fan-out, and
  a supervisor for assignment + rebalancing on worker death.
- **Sleeve-affinity is mandatory for risk correctness.** A sleeve's risk budget must have **one
  authoritative owner**; do **not** split a sleeve across workers, or two workers race the same
  daily-loss/exposure budget and both trade through it. Either pin a sleeve to one worker
  (simple) or run a **central risk-authority service** that all workers consult pre-trade
  (scales further, adds a hop). This choice is the central distributed-systems decision.
- **Idempotency at scale:** the gateway dedup store (client_order_id) needs TTL/cleanup so it
  doesn't grow without bound; keys must survive a gateway restart (durable).

### 7.4 Risk at portfolio scale  *(expands P3)*

- Per-sleeve gates (today) are necessary but insufficient. Add **portfolio aggregation**:
  gross/net by settlement-source, by event, by venue, by correlation cluster; concentration
  caps; a **settlement-source kill** (halt *all* weather if the NWS feed is stale/disputed);
  stress/VaR under correlated-shock scenarios (heat dome, data outage, venue halt).
- **Real-time exposure aggregation** across many markets/sleeves is itself a scaling problem —
  it's a streaming reduce that must be fast enough to gate the next order. Likely a dedicated
  risk-aggregation service feeding both the runner-side and gateway-side gates (dual-gate
  parity, C1).

### 7.5 Market capacity, impact & adverse selection  *(expands Workstream C)*

- **Capacity curves** by venue × family × side × price-bucket × time-of-day × time-to-close ×
  day-of-week, sourced from live depth probes, feeding sizing. No size claim is creditable
  without its depth profile.
- **Queue-position modeling** for passive fills; **adverse-selection telemetry** (mid move
  right after your passive fill = you're being picked off) per execution mode. At scale, fills
  that look profitable in aggregate can be −EV maker / +EV taker — never pool execution modes.

### 7.6 Storage / data-lake scale  *(attaches to Workstream A)*

- Raw + normalized Parquet grows fast at 10–100× instruments. Need **partitioning** (date /
  venue / series), **compaction** (avoid the small-file problem from per-tick writes),
  **retention + tiering** to cold storage, and **schema-evolution** handling so replay
  (determinism) still works across schema versions. Write path needs batching/buffering.

### 7.7 Model & feature serving  *(attaches to P6; ML pipeline)*

- **Online feature state** (`feature-builder`) must use **bounded windows + eviction**, or it
  leaks memory as markets accumulate. **Inference in the hot path** (`model-runtime`, ONNX)
  needs a per-tick latency budget — batch where the strategy allows, keep models warm,
  **hot-swap artifacts by bundle version without restart**. GPU vs CPU placement; the HF text
  models are external-signal producers (offline), not hot-path scorers — keep that boundary.

### 7.8 Multi-venue scale  *(new workstream)*

- Kalshi-only today. Adding Polymarket/others needs: a **unified instrument identity** and
  clock discipline (NTP/PTP; exchange-ts vs receipt-ts), per-venue **rate-limit budgets** and
  credential isolation, per-venue settlement/fee models, and — for cross-venue arb —
  cross-venue risk and synchronized capacity. The `adapters` venue abstraction is the seam;
  each venue is a normalizer + venue-client + reconcile + fee model.

### 7.9 Parity & determinism at scale  *(expands P5)*

- Every strategy carrying a Rust twin makes **parity a tax**: `parity_check` + no-trade smoke
  CI time grows linearly with strategy count. Mitigations: **parallelize** parity in CI, gate
  **only-changed** strategies on PRs (full matrix nightly), and **generate fixtures with a
  tool** (today they're hand-authored/captured — see this session) rather than by hand.
- **Decide which strategies justify a Rust twin at all.** Latency-insensitive sleeves
  (weather, entertainment, macro) can run the **Python runner** or the config-only
  `external_edge` archetype — the Rust twin is only worth its maintenance cost for
  latency-sensitive families. Mandating a twin for everything does not scale; gate it on the
  strategy's latency class.

### 7.10 Observability, ops & cost  *(expands P5/E)*

- **Observability:** metrics (Prometheus), **distributed tracing across the tick-to-trade
  path** (receipt→normalize→feature→strategy→risk→gateway→ack), structured logs, SLOs,
  dashboards. Without per-hop tracing you cannot debug latency or drops at fleet scale.
- **Alerting:** data staleness, reconnect storms, risk-budget burn, kill-switch trips, PnL
  anomalies, settlement-source disputes, per-sleeve health. Sequence-gap counters → alerts
  (Workstream A).
- **Cost discipline is part of net edge.** Colocation, data egress, GPU, storage, and
  **signal-provider cost** all scale with breadth. Concretely: **Open-Meteo/NOAA free tiers
  rate-limit** — at N locations × frequent polls you need a **forecast-cache service** + a
  paid/self-hosted NWP source. Track cost-per-strategy and cost-per-market; a thin-book edge
  can be eaten by infra/data cost before fees.

### 7.11 Failure modes & resilience at scale  *(cross-cuts P2–P4)*

- **Fail-closed, partially:** one venue/provider/worker down must degrade gracefully, not halt
  everything (unless it's the risk authority — that must fail *closed*). Define per-dependency
  fallback policy.
- **Reconnect/discovery thundering herd:** jittered backoff; incremental, lifecycle-driven
  discovery instead of full REST re-list.
- **Poison messages / split-brain:** circuit-breakers on the normalizer reject rate; fencing
  tokens for the partitioned-state owners so a zombie worker can't double-trade.

### 7.12 Scale sequencing (do **not** scale before edge)

Scale is **P6**, deliberately late. Building a fleet on an unproven edge multiplies cost and
operational risk for negative expected value. The correct order: **prove one settled,
post-cost, market-beating edge (P7) → harden the single-box live path (P1–P4) → only then
invest in the breadth/fleet primitives (P6/7.x).** The breadth-not-depth thesis (7.0) means
the *first* scaling investment that pays off is **capacity profiling + correlation-aware
portfolio risk** (7.4/7.5), because those convert "more markets" into "more *independent*
edges" — which is the only kind of scale that actually grows risk-adjusted PnL here.

### 7.13 Scale-readiness scorecard (today)

| Axis | State | First move |
|------|-------|-----------|
| Data-plane throughput | single-process Python | Rust hot-path live + bounded coalescing queues |
| Durable/partitioned state | in-RAM HashMap | WAL + checkpoint + per-sleeve partition + eviction |
| Horizontal compute | single process | sleeve-affinity or central risk authority |
| Portfolio risk | per-sleeve only | settlement-source/correlation aggregation + kill |
| Capacity/impact | static depth caps | live depth-profile-driven sizing |
| Storage | Parquet, unpartitioned-at-scale | partition + compaction + retention |
| Model/feature serving | offline + per-event | bounded feature windows + warm hot-swap inference |
| Multi-venue | Kalshi-only | venue abstraction + clock discipline + per-venue risk |
| Parity/determinism | full matrix, hand fixtures | parallel + changed-only CI + fixture generator |
| Observability/cost | stderr snapshots | tracing + metrics + signal-cost cache |

