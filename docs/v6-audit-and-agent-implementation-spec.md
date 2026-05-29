# V6 Hyper-Comprehensive Audit + Agent Implementation Spec

**Date:** 2026-05-29
**Builds on / supersedes for implementation purposes:** `docs/v5-audit-and-agent-implementation-spec.md` (its P1–P9 landed; verified in §1). Prior docs (`v3`, `v4`, `hyper-comprehensive-codebase-audit-agent-spec`, `post-implementation-audit-and-engineering-plan`, `live-readiness-audit-report`, `logic-audit-report`) are historical.
**Audience:** A single implementation agent. Read §0, then work top-to-bottom. Every finding cites `file:line` as read in this tree on 2026-05-29, and several are backed by **empirical runs** (§6).

This pass was done from the angles the brief named: **assumptions, speed, redundancy, data-security, strategy-integration / dev-ergonomics, and — weighted most heavily — the trader's perspective.** It also actually *ran* the system: full Python + Rust test suites, the capture→normalize→backtest data path on real captured data, the Rust criterion benchmarks, and a direct risk-gate probe.

> **Implementation status (2026-05-29).** Phases **P1–P5 are implemented and verified** in this tree (Python 321 passed, Rust fmt/clippy/test green, all 5 parity checks pass, mypy clean):
> - **P1 (V6-T1):** `risk/limits.py` cash/exposure are now side-aware (sells never blocked); Rust `risk/src/lib.rs` lets risk-reducing sells through the daily-loss gate. Tests: `test_gross_exposure_allows_risk_reducing_sell`, `test_available_cash_allows_risk_reducing_sell`, Rust `allows_risk_reducing_sell_during_daily_loss_breach`.
> - **P2 (V6-T2):** `risk/state.py::DrawdownHalt` + `risk/policy.py` — realized loss latches the one-way kill switch; unrealized drawdown is an auto-clearing soft-halt that blocks new buys but allows exits. Tests: `test_unrealized_drawdown_soft_halts_buys_without_tripping_kill_switch`, `..._still_allows_risk_reducing_exit`, `..._auto_clears_on_recovery`, `test_realized_daily_loss_latches_kill_switch`.
> - **P3 (V6-D1):** `storage/parquet_store.py` version-tolerant reader (upcast legacy/older, hard-fail newer) + `ec migrate-data`. Verified on the 258 real capture files. Test: `test_legacy_parquet_without_schema_version_reads_and_migrates`, `test_raw_parquet_newer_schema_version_raises`.
> - **P4 (V6-T4):** OBI scalper now crosses the spread (price at the ask) so the IOC taker fills. Test: `test_obi_scalper_buy_crosses_the_spread_and_fills`.
> - **P5 (V6-C3/T5):** `strategy/pricing.py` + `strategy/sizing.py` and Rust `runner::pricing` with parity-aligned semantics (buy floors, sell ceils). Tests: `tests/test_strategy_pricing.py`, Rust `runner::pricing::tests`. *Helpers shipped; refactoring the parity-covered strategies onto them is a follow-up requiring simultaneous Rust+Python edits + parity regeneration.*
>
> **Implementation status (2026-05-29, continued).** Phases **P6, P8, C4 and the §5-gate items in P10 are now implemented and verified** (Python 331 passed, Rust fmt/clippy/test green, all 5 parity checks pass — flu now 4 cases incl. a half-cent, ruff + mypy clean):
> - **P8 / V6-C4 (reconciliation):** Rust `live-runner/src/reconcile.rs` seeds venue positions + balance into risk state, writes a durable diff report, and **halts before submitting** if the adopted baseline breaches risk; daily loss stays venue-authoritative (idempotent re-sum of fills). Kalshi REST `get_balance` + `list_positions` added. Tests in `reconcile.rs` + `kalshi/rest.rs`.
> - **P10 / V6-D2 (ws-lag gate):** `live-runner/tests/ws_lag.rs` asserts a slow submit never starves WS ingest (max reader gap < 100ms vs a 200ms stall). **V6-C6 (observability):** periodic metrics snapshot file written on the 1s tick (uptime, kill-switch, daily realized loss, counters).
> - **P5/P9 follow-up — V6-C3 completed:** fixed a real **scale mismatch** in Rust `runner::pricing` (`PRICE_ONE` was 1e4 vs the runner's 1e6 `PRICE_SCALE`; helpers were unused/unusable), unified `PRICE_SCALE = pricing::PRICE_ONE`, and wired edge-preserving **cent-flooring** into `ExternalEdgeStrategy` (Rust) and `crop`/`flu`/`weather` (Python). Half-cent parity case `flu_…/04_signal_halfcent.json` + Python pin `tests/test_strategy_pricing_discretization.py` + AST lint `test_promotable_mid_pricing_strategies_discretize_with_shared_helpers`.
> - **P6 / V6-S1+S2 (promotion gate made real):** `verify-strategy` already enforced parity-file presence + Rust-runtime archetype + `parity_check` (S1). Added the **no-trade smoke (S2)**: `cli/strategy_smoke.py` replays the strategy's parity stream through the **real** runner + risk gate and requires ≥1 risk-APPROVED intent; wired into `verify-strategy` (`--skip-smoke` escape hatch) and gated in `tests/test_live_readiness_smoke.py`.
>
> **§5 Final Live-Readiness Gate: all 7 conditions now hold.** `verify-strategy sports-tennis-xgboost` and `flu-hospitalization-surge` pass the full gate (parity + smoke).
>
> **New findings surfaced while closing S2 (back with real runs):**
> - **flu/crop were silently 100% risk-rejected (`missing_market_snapshot`)** — they emit on External signals and never attached executable BBO evidence, so they passed parity yet dispatched **zero intents**. Fixed: both now cache the latest quote and attach `market_snapshot_from_quote_event(..., side=order_side)` to their orders.
> - **Python↔Rust risk-gate model divergence:** Python requires an order-attached `market_snapshot`; Rust records per-instrument BBO into `SleeveState` (`record_quote_bbo`) and checks freshness there. Same strategy → 0 trades in Python backtest, trades in Rust live. The snapshot-attach fix aligns them.
> - **crop confidence-gate divergence — CLOSED (2026-05-29):** Python `crop` requires `confidence >= 0.55`; the generic Rust `external_edge` runtime ignored `confidence` entirely, so crop would have traded live on low-confidence signals its author meant to suppress. Fixed: `confidence` is now plumbed into the Rust `StrategyEvent::ExternalProbability` (via `parse_external`) and `ExternalEdgeStrategy` enforces a `min_confidence` gate (0 == no gate, so flu is unaffected; a missing confidence is treated as zero, matching Python). Locked by Rust unit test `external_edge_confidence_gate_suppresses_low_confidence` and a low-confidence parity case `crop_…/03_signal_lowconf.json` (confidence 0.40 < 0.55 → both languages emit `[]`).
> - **flu/crop emit unbounded GTC** (no `expires_at`); the S2 smoke uses `allow_unbounded_gtc=true`. A real live sleeve must either allow it or the strategies must set an expiry.
>
> **Remaining (NOT required for a single quote-/prediction-triggered taker like tennis):** P7 (Rust ReplaceOrder, atomic arb groups — arb only), P9 (archetypes-for-scale, per-series fees, event-group correlation), P10 leftovers (hot-path profiling V6-Sp1/2/3, Prometheus HTTP endpoint beyond the snapshot file, account-wide self-cross V6-C5 — multi-sleeve only). The live deploy itself needs operator action: **tennis alpha refinement** (per operator) + a **live sleeve config** with real capital + credentials (not author-supplied).

The most dangerous class of bug here is the **dead/divergent safety check**: a gate that exists, passes review, and either never fires or fires differently in Python vs Rust. V5 fixed three of those. This pass found more, plus the first **mark-to-market kill-switch failure mode that V5's own fix introduced.**

---

## §0. Ground Rules For The Agent

1. **Baseline before every phase.** Record counts; a drop is a regression to fix in-phase:
   ```bash
   cd C:/QWS/eventcontracts
   cargo fmt --manifest-path rust/Cargo.toml --all -- --check
   cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
   cargo test --manifest-path rust/Cargo.toml --workspace
   cd python && python -m ruff check src tests && python -m mypy src/eventcontracts tests && python -m pytest tests -q && cd ..
   ```
   Current green baseline (2026-05-29): **Python 307 passed**, **Rust workspace tests pass**, mypy clean, ruff clean.
2. **One PR per phase (P1…P10).** Title references the phase id.
3. **A fix without a failing-then-passing test did not happen.**
4. **Dual-language invariants need dual-language coverage.** Parity cases in `contracts/parity/` are the cross-language contract. If a fix changes risk/exec/normalization semantics, it needs a parity case or it will silently diverge.
5. **Never bypass the gateway / never mutate framework state from a strategy.** Strategies return decisions only.
6. **Single source of truth:** `DryRunGateway.sleeve_state` (Rust) and `SleeveRiskGate + PnLTracker` sharing one `DailyLossLedger` (Python). No mirror state.
7. **The two risk gates must agree.** `python/src/eventcontracts/risk/` and `rust/crates/risk/` implement the same policy from two snapshots. Any check added to one must be added to the other, and the divergences in §2 must be closed.

---

## §1. State Of The Tree Today (V5 verification)

Confirmed **landed** by inspection on 2026-05-29:

- **T1 lifecycle:** `close_date_updated` and other pure-metadata events map to `METADATA_UPDATED`; the simulator returns early without a state change (`execution/market_simulator.py:325`). Only `CLOSED/DETERMINED/FINALIZED` cancel resting orders (`:328-333`).
- **T2 fee-edge gate is live in Rust.** `IntentSnapshot` carries `fair_price/min_executable_edge_ticks/fee_rate_bps` (`risk/src/lib.rs:104-114`); the gate fires (`:309-329`); the gateway threads them from `DecisionPayload::PlaceOrder` in `prepare_one` (`gateway/src/lib.rs:1330-1344`); the live-runner emits them (`live-runner/src/main.rs:851-864`). Unit tests cover reject/approve (`risk/src/lib.rs:879-909`).
- **T3 unrealized drawdown** feeds the daily-loss gate in both languages (`risk/state.py:53` `total_loss_for`; `risk/src/lib.rs:339-346` `daily_realized_loss + unrealized_drawdown_loss.max(0)`).
- **T4 liquidation marks:** `PnLTracker` keeps `liquidation_mark_price` (bid for longs) and `mark_mode` (`execution/pnl.py:40,60-66,148-149`).
- **T5 maker fee ≠ 0:** `KalshiFeeModel.maker_rate = 0.0175` (`adapters/venues/kalshi/fees.py:20,25`).
- **T6 cancel/replace latency:** modeled on an `effective_at` timeline (`execution/market_simulator.py:108-109,399-421,704-708,734-741`).
- **T7 book depletion within a tick:** `_with_opposite_levels_debited` debits consumed levels (`execution/market_simulator.py:132-156,632-638`).
- **Sp1/Sp2:** ONNX `OnnxScorerPool` + intra-op pinned (`model-runtime/src/lib.rs`); `remaining_quantity` now on own-fill events (`runtime-hot/src/event.rs:94`, `runner/src/lib.rs:263`).
- **Scaffolding:** `ec new-strategy` / `verify-strategy` exist (`cli/strategy_tools.py`); `external_edge` archetype is config-only in Rust (`runner/src/registry.rs:62-66`); flu + crop promoted with parity cases.
- **Security:** pinned deps, pip-audit/cargo-audit CI, redaction hints in parquet store.

**Still open (carried from the post-implementation audit; NOT in V5's scope), confirmed open today:**

- **R4** full reconciliation (positions, balances, durable checkpoint, diff report) — only `list_open_orders` + `list_fills_since` exist; restart can double-count fills.
- **R5** atomic execution group for cross-venue arb — no `AtomicExecutionGroup`/`LinkedIntents` anywhere (grep clean).
- **R6** Rust `ReplaceOrder` — `DecisionPayload` has only `PlaceOrder`/`CancelOrder` (`gateway/src/lib.rs:90-107`). (`remaining_quantity` *did* land; replace did not.)
- **R7** deterministic price-tick helpers — no `floor_to_tick`/`ceil_to_tick`/`pricing.py` anywhere (grep clean); strategies still round/clip ad hoc.
- **R8** account-global self-cross prevention — still process-local.
- **R9** live Prometheus metrics endpoint — no `--metrics-port`/`/metrics` (grep clean).

These are folded into §2/§3 with current evidence so the agent works one document.

---

## §2. New Findings (code-verified + empirically reproduced, this pass)

Severity: **C**ritical / **H**igh / **M**edium / **L**ow. `[tag]` is the audit angle. Findings prefixed `V6-`.

### V6-T1 [C] [trading-logic / parity] The Python risk gate blocks risk-reducing SELLs

`check_available_cash` (`risk/limits.py:104-109`) and `check_gross_exposure` (`risk/limits.py:89-101`) compute `order_notional(order)` and add it **for every order regardless of side**. `order_notional` is `price * quantity` with no sign (`:32-36`). So a **closing SELL** — the exit that *reduces* risk and *returns* cash on Kalshi — is rejected when available cash is low or gross exposure is near the cap.

**Empirically reproduced** (`scripts/audit_probe.py`, §6): a 100-lot SELL at 0.90 with $5 available cash → `('available_cash',)`; the same SELL at 90% of the gross cap → `('max_gross_exposure',)`.

This is also a **Python↔Rust divergence**: the Rust gate computes a *signed* projected position (`signed_qty = -qty` for sells) and notional from `projected_qty.abs()`, so a sell that reduces a long correctly *lowers* projected notional — proven by `rust/crates/risk/src/lib.rs:798` `sell_reduces_position_notional`. Rust has **no available-cash check at all**, and `PortfolioRiskAllocator.reserve` *only* reserves BUYs (`allocation/capital.py:114`). Three capital layers, three different behaviors.

**Why it matters (trader):** the one order you must never block is the exit. A sleeve that's long, near its cap, and bleeding cannot de-risk. In a fast adverse move this converts a manageable loss into a max loss.

**Fix:** make cash/exposure checks side-aware and position-aware.
- `check_available_cash`: a SELL that does not exceed the held quantity consumes no cash → no rejection. (A SELL beyond held quantity would open a short, which the venue forbids; treat the excess as the cash/again-buy-NO case or reject only the excess.)
- `check_gross_exposure`: compute projected gross from the *signed* delta against the current position for that `(instrument, outcome_side)` exactly like Rust, so a closing sell reduces projected gross. Reuse the same math as `check_position_notional`.
- Add a Python test mirroring `sell_reduces_position_notional`, and a parity case so the two gates agree on a buy-then-sell sequence.

### V6-T2 [H] [trading-logic / risk] A transient unrealized dip permanently trips the one-way kill switch

V5-T3 made `total_loss_for = realized_loss + unrealized_drawdown` feed the daily-loss check (`risk/state.py:53`). The Python gate then **permanently** trips the one-way `KillSwitch` whenever `max_daily_loss` appears (`risk/policy.py:145-147`; `KillSwitch.trip` is irreversible for process lifetime, `risk/state.py:78-83`).

`unrealized_drawdown` is a **liquidation mark-to-market** number that moves with the bid. On wide event-contract books (5–10¢ spreads), a momentary spread-widening or a single thin quote can push liquidation-marked unrealized below `−max_daily_loss` for one tick and then fully recover.

**Empirically reproduced** (`scripts/audit_probe.py`, §6): record −$60 unrealized with a $50 cap → `total_loss_for = 60 ≥ 50` (trips, permanently); record $0 unrealized (full recovery) → `total_loss_for = 0`, **but the switch is already latched.** The sleeve is dead for the rest of the process over a blip.

This is **also a Python↔Rust divergence**: Rust re-evaluates `total_daily_loss` every `evaluate` call and does **not** latch a permanent kill on daily-loss (`risk/src/lib.rs:339-346` just returns a rejection). So the same drawdown halts Python forever and Rust only while it persists.

**Fix:** split the two concepts.
- **Realized** daily loss crossing the cap → permanent latch (correct; an operator must review).
- **Unrealized** liquidation drawdown crossing the cap → a *soft halt* that (a) auto-clears when the mark recovers (with hysteresis to avoid flapping, e.g. clear only below `0.8 × cap`) and (b) **still allows risk-reducing exits** (see V6-T1). Never let a recoverable mark permanently kill a sleeve.
- Make Rust and Python agree: pick one policy and encode it in both, with a parity case driving an unrealized dip-then-recover sequence.

### V6-T3 [H] [trading-logic / dead-check] Fee-edge gate is inert for 23 of 27 Python strategies

`check_fee_adjusted_edge` only fires when `fair_price` is present in `PlaceOrder.metadata` (`risk/limits.py:121-123`). Only **4** strategies emit it: `weather_temperature_arbitrage`, `sports_tennis_xgboost`, `flu_hospitalization_surge`, `crop_drought_yield_reversion` (grep §6). The other ~23 — including every microstructure/momentum/novel sleeve — place orders with **no post-fee edge protection.** This is the exact dead-check failure mode V5-T2 fixed in Rust, still live in Python for most of the book.

**Fix:**
- For model/edge archetypes, **require** `fair_price` (enforce in `verify-strategy`, see V6-S1; a model-edge strategy with no fair value is a bug).
- For genuinely non-edge strategies (pure microstructure/scalping), document that they trade without the gate and give them a minimum-spread-capture floor (expected capture ≥ round-trip fee) so they cannot systematically trade negative-EV after fees.

### V6-T4 [H] [trading-logic / strategy-bug] OBI scalper emits IOC BUY at the bid — it can never fill

`MicrostructureObiScalperStrategy._buy` places `price = best_bid`, `time_in_force = IOC` (`microstructure_obi_scalper.py:155,165-170`). An IOC buy priced at the bid is **not marketable** (bid < ask), and IOC cannot rest, so the simulator cancels it with zero fill (`market_simulator.py:163-179` `_is_marketable` returns False; `:452-457` cancels non-marketable IOC). The strategy's own hypothesis — imbalance precedes a *spread crossing* — requires either crossing (buy at the ask within a slippage cap) or posting passively and waiting. As written, the scalper is a no-op that emits cancel-bound intents forever.

This pattern likely recurs in other "rules-mode" strategies that reuse `best_bid` for a buy. Audit each strategy's `_buy`/`_sell` price+TIF pairing.

**Fix:** for a taker scalp, price at the opposite touch (`best_ask` for a buy) with a max-slippage guard; for a maker scalp, use `post_only` GTC/GTD and model the queue. Add a test that runs the scalper against a moving synthetic book and asserts ≥1 fill.

### V6-T5 [M] [trading-logic / sizing] No edge-proportional sizing; constant `size` everywhere

Every strategy uses a fixed `size`/`clip_size` param (e.g. `microstructure_obi_scalper.py:64`). Sizing ignores edge magnitude, bankroll, price, and correlated exposure. For binary event contracts, the rational stake scales with edge and is capped by ruin/correlation constraints. A flat clip over-bets thin edges and under-bets fat ones.

**Fix:** add a shared `strategy/sizing.py` (fractional-Kelly or edge-proportional, hard-capped by `RiskProfile` limits and by available cash), usable by all archetypes. Keep it pure and parity-testable. This is also a prerequisite for the archetype work (V6-S3).

### V6-T6 [M] [trading-logic / correlation] No per-event-group exposure netting in the sleeve gate

The Python `SleeveRiskGate` caps per-instrument and per-sleeve gross only. Multiple markets on one underlying event (temperature buckets, the YES/NO legs, sequential brackets) are capped independently, so a sleeve can stack many correlated bets that are effectively one large position. `PortfolioRiskAllocator` *has* `group_budgets` (`allocation/capital.py:87,133-135`) but it is not wired into the backtest/live-paper risk path.

**Fix:** thread an `event_group_id` exposure aggregate into the sleeve gate (or run the allocator in-path), so correlated outcomes share a budget. At minimum, document the unmanaged-correlation assumption prominently.

### V6-T7 [M] [trading-logic / freshness × latency tier] The 1 s default freshness window starves slow strategies

`RiskProfile.max_market_data_age_ms` defaults to **1000** (`domain/spec.py:56`). `check_market_snapshot` rejects `stale_market_snapshot` when the snapshot age exceeds it (`risk/limits.py:236-240`). External-signal strategies (weather forecast every 10 min, macro on a print) cache a quote snapshot and act minutes later; their cached snapshot is almost always >1 s old → the order is rejected stale. So the same architecture that V5 made "live" can still reject ~100% of a slow strategy's orders depending on sleeve config.

**Fix:** freshness must scale with the strategy's `LatencyTier` (FAST = tens of ms, STANDARD = ~1 s, RELAXED = seconds–minutes). Either derive the default from the tier or require each sleeve to set it; surface a `verify-strategy` warning when a RELAXED strategy ships with a sub-second freshness window. Confirm the weather/macro sleeves set an appropriate value.

### V6-T8 [M] [trading-logic / fees] Flat 1.75% maker fee may over-penalize maker-free series

V5-T5 correctly removed the always-zero maker assumption, but a *flat* 1.75% maker over all Kalshi series over-states cost where a series is genuinely maker-free, biasing maker strategies pessimistic. Fees are per-series on Kalshi.

**Fix:** make taker+maker rates a per-series fee config loaded from the sleeve (default conservative, overridable). Re-run microstructure sweeps under the per-series schedule before any promotion.

### V6-D1 [H] [data-security / ops] Strict parquet schema-version gate bricks all previously-captured data

**Empirically reproduced** (§6): `ec normalize --data data/weather-overnight` and `ec inspect-data` both abort with
`ValueError: parquet schema_version mismatch ... : missing != 1` from `_read_table_checked` (`storage/parquet_store.py:629-633`). The data was captured **two days ago**; a schema-version bump to `1` with a strict equality check made the **entire historical lake unreadable**, and there is no migration path. The capture→normalize→backtest loop — the core research workflow — is broken for any real prior capture.

**Fix:**
- Make the reader version-*tolerant*: accept `missing`/older versions and route them through an explicit upcast, only hard-failing on a *newer*-than-supported version.
- Add an `ec migrate-data` CLI that rewrites old partitions with the current schema metadata.
- Add a regression fixture: a parquet file written without `schema_version` must read (via upcast), not crash.

### V6-D2 [M] [observability / speed] The `ws_lag_under_load` safety benchmark produced no output

`cargo bench --workspace` ran `external_replay` (see Sp1) but emitted **no results** for `live-runner/benches/ws_lag_under_load.rs` (§6 transcript). This is the single most important *safety* benchmark — it measures WS ingest lag while a slow REST submit runs (the R1 head-of-line-blocking scenario). If it is silently not executing, the async-executor protection is unbenchmarked.

**Fix:** ensure the bench is a wired `[[bench]]` target that actually runs, assert a p99 ingest-lag threshold, and run it in CI so a regression fails the build.

### V6-Sp1 [M] [speed] Hot-path normalize+strategy regressed ~27%

`external_kalshi_ws_normalize_strategy` measured **26.96 µs** with criterion reporting **change +25.2%…+28.5% (p = 0.00), "Performance has regressed."** (§6). The V5 lifecycle/fee-edge/book-depletion additions added measurable cost to the per-event hot path. 27 µs/event ≈ ~37k events/s single-thread, which is fine for Kalshi volumes, but an unexplained 27% regression must be understood, not absorbed.

**Fix:** profile the delta (flamegraph the bench), eliminate avoidable per-event allocation/cloning introduced by V5, then **intentionally re-baseline** criterion (`cargo bench -- --save-baseline v6`) and gate future runs with `--baseline v6` in CI so "regressed" is a build failure, not a log line.

### V6-Sp2 [L] [speed] Python runner rebuilds the full context per decision

`StrategyRunner.process_event` calls `context_provider.context()` once for the strategy (`runner/base.py:164`) and again per decision for risk (`:174`). Each call materializes positions/cash/exposure. For multi-decision events over large position maps this is O(decisions × positions) per event and slows backtests/sweeps.

**Fix:** build the context once per event; pass an immutable snapshot to risk; rebuild only when a reservation/fill mutates state mid-event.

### V6-Sp3 [L] [speed] Confirm V5 carryover micro-allocations are gone

Re-verify the V4/V5 hot-path items (interned `last_quote_epoch_secs` keys, single `now` decode per batch, no per-decision `format!`). Fix opportunistically while addressing Sp1.

### V6-S1 [H] [dev-ergonomics] `verify-strategy` gives false greens — the promotion gate is cosmetic

`cli/strategy_tools.py` + `make verify-strategy` are the intended promotion gate. Today they pass when they must fail:

- **(a) Phantom archetypes.** `KNOWN_ARCHETYPES = {threshold, external_edge, model_edge, scalper, arb}` (`strategy_tools.py:10`), but Rust `instantiate()` implements only `threshold` (named) and `external_edge` (`runner/src/registry.rs:54-70`). A strategy scaffolded `--archetype scalper|model_edge|arb` **passes `verify-strategy`** (archetype is "known") yet **cannot instantiate in Rust** → `UnknownStrategy` at live start.
- **(b) Empty parity passes.** `verify-strategy` only checks the parity *directory* exists (`strategy_tools.py:56` `is_dir()`); `new-strategy` creates it with a `.gitkeep` (`:43`), so a strategy with **zero parity cases** passes.
- **(c) Python-only check.** It calls `ensure_registered` (Python) but never checks Rust registration/instantiability (`:60-66`).
- **(d) No parity run.** `make verify-strategy` (`Makefile:49-51`) never runs `parity_check`; the Makefile `parity-check` covers only the 5 hand-listed strategies.

**Fix:** `verify-strategy` must, and CI must enforce for any non-paper sleeve:
1. Restrict `KNOWN_ARCHETYPES` to archetypes with a real Rust runtime (or add the runtimes — see V6-S3).
2. Require ≥1 parity case *file* in the parity dir (not just the dir).
3. Confirm the `name` is instantiable in **both** languages — add a `parity_check`/registry `--check <spec>` mode in Rust that returns nonzero on `UnknownStrategy`.
4. Run `parity_check` and require it pass within tolerance.

### V6-S2 [H] [dev-ergonomics / safety] Nothing warns that a strategy's orders will be 100% risk-rejected

The runner backfills `market_snapshot` from the triggering event only for **Quote/OrderBook** triggers (`runner/base.py:240-268`). A strategy that emits a `PlaceOrder` in response to an **External/Timer/Settlement** event without caching+attaching its own snapshot gets `missing_market_snapshot` on **every** order (`risk/limits.py:198-200`) — silently. ~18 strategies don't set `market_snapshot` themselves; whether they're safe depends entirely on which event they react to, and **nothing checks this.** The strategy "runs," emits decisions, and dispatches zero intents.

**Fix:** add a `verify-strategy` smoke that replays a tiny synthetic stream (quote → book → external signal → timer) through the real runner+risk gate and asserts the strategy produces **≥1 risk-APPROVED intent** (or has explicitly declared "no-trade"). A 100%-rejection strategy fails verify with the dominant rejection reason printed.

### V6-S3 [M] [strategy-integration / scale] 3 Rust runtimes + 1 archetype for 27 Python strategies

`default_registry()` hand-registers `weather_threshold`, `example_threshold`, `sports_tennis_xgboost` (`runner/src/registry.rs:89-106`); `external_edge` is the one config-only archetype (`:62-66`). Promotion still means hand-porting Rust + hand-keeping parity for each strategy. This is both the biggest "easiest to develop" barrier and a silent-divergence risk for everything not promoted.

**Fix:** implement the remaining high-value archetypes as parameterized Rust runtimes so promotion is config-only:
- `model_edge` (fair-value vs book, fee-aware, the generalization of weather/tennis/flu/crop),
- `passive_quoter` / `scalper` (post-only quoting with queue + protective cancel),
- `cross_venue_arb` (paired legs — depends on V6-C2 atomic groups).
Then migrate Python strategies onto archetypes where they fit, and make every archetype carry a parity case. Document the archetype contract in `docs/strategy-promotion.md`.

### Carryover findings (still open, current evidence)

- **V6-C1 [H] [execution] Rust live cannot replace an order.** `DecisionPayload` = `PlaceOrder | CancelOrder` only (`gateway/src/lib.rs:90-107`). A partial-fill tail can only be canceled, not repriced, in Rust live. (`remaining_quantity` is available — `runtime-hot/src/event.rs:94` — so the data to drive a replace exists.) Add `DecisionPayload::ReplaceOrder` with cancel-then-new emulation + fill-race abort.
- **V6-C2 [H] [execution] No atomic execution group for cross-venue arb.** No `AtomicExecutionGroup`/`LinkedIntents` (grep clean). `arbitrage_cross_venue.py` emits independent legs; one leg fills, the other rejects → directional exposure the strategy math assumed was hedged. Add a linked-intent group with legging-risk reservation and auto-unwind.
- **V6-C3 [H] [trading-logic] Ad-hoc price discretization consumes edge.** No `floor_to_tick`/`ceil_to_tick` anywhere (grep clean); strategies round/clip raw prices (e.g. `crop_drought_yield_reversion.py`, `weather_temperature_arbitrage.py`, Rust `runner/src/lib.rs` `.round()`). Rounding a 0.556 fair to a 0.56 buy limit can flip a positive model edge negative. Add `strategy/pricing.py` (buy floors, sell ceils to the venue tick) + Rust `runner::pricing`, refactor promoted strategies onto them, and lint promoted strategies against raw `round()`/`_clip` on prices.
- **V6-C4 [H] [reconciliation] Restart truth is incomplete.** Only `list_open_orders` + `list_fills_since` exist; no positions/balance fetch, no durable `last_reconcile_checkpoint`, no diff report. A second restart in the same UTC day can re-process fills and double-count daily loss. Add positions/balance REST, a persisted checkpoint `{last_fill_epoch, cursor, restored_at}`, and a startup diff that halts live submit unless clean or operator-resolved.
- **V6-C5 [M] [compliance] Self-cross protection is process-local.** Two sleeves in two processes can self-match at the venue. Needs an account-wide CEG or shared order service before multi-sleeve live.
- **V6-C6 [M] [observability] No live metrics endpoint.** No `--metrics-port`/Prometheus (grep clean); operators see counters only at exit. Add a scrape endpoint (gateway decision latency, last-look rejects by reason, stale-intent drops, reconnects, kill-switch state, daily realized loss) + alert rules.

---

## §3. Phased Implementation Plan

Ordered so trading-safety correctness lands first, then research-loop integrity, then execution completeness, then ergonomics/scale, then speed/observability. Each phase is independently shippable and depends only on earlier phases.

| Phase | Theme | Findings | Sev |
| --- | --- | --- | --- |
| **P1** | Risk-gate correctness: side-aware cash/exposure; exits always allowed | V6-T1 | C |
| **P2** | Kill-switch failure mode: split realized latch vs unrealized soft-halt | V6-T2 | H |
| **P3** | Data-lake migration: version-tolerant reader + `migrate-data` | V6-D1 | H |
| **P4** | Strategy correctness: OBI fill bug; fee-edge coverage; freshness×tier | V6-T4, V6-T3, V6-T7 | H |
| **P5** | Deterministic pricing + sizing helpers (both languages) | V6-C3, V6-T5 | H |
| **P6** | Promotion gate made real: verify-strategy both-language + parity-required + no-trade smoke | V6-S1, V6-S2 | H |
| **P7** | Execution completeness: Rust ReplaceOrder; atomic arb groups | V6-C1, V6-C2 | H |
| **P8** | Reconciliation completeness: positions/balance/checkpoint/diff | V6-C4 | H |
| **P9** | Archetypes for scale + per-series fees + event-group correlation | V6-S3, V6-T8, V6-T6 | M |
| **P10** | Speed + observability: profile/re-baseline hot path; ws_lag bench; metrics endpoint; self-cross | V6-Sp1, V6-Sp2, V6-Sp3, V6-D2, V6-C6, V6-C5 | M/L |

### P1 — Risk-gate correctness (V6-T1)
- Rewrite `check_available_cash` and `check_gross_exposure` (`risk/limits.py`) to be side- and position-aware, reusing the signed-projection math already in `check_position_notional`. A SELL that does not exceed held quantity consumes no cash and reduces gross.
- Guarantee exits are never blocked: a risk-reducing order must pass cash/exposure regardless of cap headroom.
- Add a Python test mirroring Rust `sell_reduces_position_notional`, plus a `contracts/parity/` case running buy→sell so the gates agree.

### P2 — Kill-switch (V6-T2)
- In `risk/policy.py`, latch the **permanent** kill switch only on **realized** daily-loss. Move **unrealized**-drawdown breaches to a separate, auto-clearing soft-halt (`RiskHaltState`) with hysteresis (clear below `0.8 × cap`).
- While soft-halted, reject new risk-*increasing* orders but allow risk-*reducing* exits (depends on P1's side-awareness).
- Mirror the policy in Rust `SleeveState` and add a parity case driving unrealized dip→recover and asserting identical halt/clear behavior in both languages.

### P3 — Data-lake migration (V6-D1)
- Make `_read_table_checked` (`storage/parquet_store.py`) tolerant: `missing`/older → upcast; newer → hard fail.
- Add `ec migrate-data --data <root>` to rewrite legacy partitions with current metadata.
- Fixture test: a no-`schema_version` parquet reads via upcast; a future-version parquet still errors.

### P4 — Strategy correctness (V6-T4, V6-T3, V6-T7)
- T4: fix OBI scalper price/TIF (cross at ask with slippage cap, or post-only); audit every strategy's `_buy/_sell` price+TIF; test ≥1 fill against a moving book.
- T3: require `fair_price` for model/edge archetypes via verify-strategy; give non-edge strategies a spread-capture-≥-fee floor.
- T7: derive `max_market_data_age_ms` from `LatencyTier`; warn in verify-strategy on tier/freshness mismatch; fix weather/macro sleeve configs.

### P5 — Pricing + sizing (V6-C3, V6-T5)
- Add `python/src/eventcontracts/strategy/pricing.py` (`floor_to_tick`, `ceil_to_tick`, `buy_limit_from_fair`, `sell_limit_from_fair`) and Rust `runner::pricing`.
- Add `strategy/sizing.py` (edge-proportional / fractional-Kelly, capped by `RiskProfile` + cash).
- Refactor the 4 fair-value strategies onto these helpers; add an AST lint that fails promoted strategies using raw `round()`/`_clip` on a price; parity-test the helpers.

### P6 — Promotion gate (V6-S1, V6-S2)
- Restrict `KNOWN_ARCHETYPES` to implemented archetypes; add a Rust `parity_check`/registry `--check <spec.toml>` that exits nonzero on `UnknownStrategy`.
- `verify-strategy`: require ≥1 parity case file; instantiate in both languages; run `parity_check`; run a synthetic no-trade smoke asserting ≥1 risk-APPROVED intent.
- Wire `verify-strategy` (full form) into CI as the gate for promoting any sleeve from paper to live.

### P7 — Execution completeness (V6-C1, V6-C2)
- Add `DecisionPayload::ReplaceOrder` (Rust) with cancel-then-new emulation that aborts the new order if a fill lands during the cancel window; release the old reservation only on terminal cancel.
- Add `AtomicExecutionGroup { group_id, legs, legging_risk_cap, cancel_window_ms }`; gateway executes legs with reservation, auto-unwinds on partial/timeout. Wire `arbitrage_cross_venue` onto it.

### P8 — Reconciliation (V6-C4)
- Add REST positions + balance fetchers; persist a reconciliation checkpoint; produce a startup diff (local-only / venue-only / qty / position / balance); block live submit unless clean or operator-resolved. Idempotent across double restarts (no fill double-count).

### P9 — Archetypes + fees + correlation (V6-S3, V6-T8, V6-T6)
- Implement `model_edge`, `passive_quoter`/`scalper`, `cross_venue_arb` Rust runtimes (config-only promotion); migrate Python strategies where they fit; parity-case each.
- Per-series taker+maker fee config from the sleeve; re-run microstructure sweeps.
- Thread `event_group_id` correlated exposure into the sleeve gate.

### P10 — Speed + observability (V6-Sp1/2/3, V6-D2, V6-C6, V6-C5)
- Profile and explain the 27% hot-path regression; remove avoidable per-event work; re-baseline criterion and gate CI with `--baseline`.
- Build the StrategyContext once per event (Sp2).
- Make `ws_lag_under_load` actually run + assert p99 in CI (D2).
- Add `--metrics-port` Prometheus endpoint + alert rules (C6).
- Begin account-wide CEG / self-cross prevention design for multi-sleeve live (C5).

---

## §4. Per-Fix Test-Efficacy Recipes

- **V6-T1** — Python unit: long 100 @ avg 0.50; SELL 100 @ 0.90 with available cash $5 → **approved** (no `available_cash`); SELL at 95% of gross cap → **approved** (no `max_gross_exposure`); a *new* BUY in the same state → still correctly capped. Parity: buy→sell sequence yields identical verdicts in Python and Rust.
- **V6-T2** — Build a 1-position portfolio; push a quote whose bid drives liquidation unrealized below `−cap` for one tick, then a quote that recovers it. Assert: (a) realized-loss latch never engaged, (b) soft-halt engaged then auto-cleared, (c) a risk-reducing SELL was allowed throughout, (d) Rust parity case behaves identically.
- **V6-T3** — verify-strategy fails a `model_edge` spec that emits no `fair_price`; a non-edge strategy with capture < round-trip fee is flagged.
- **V6-T4** — Run OBI scalper against a synthetic stream where imbalance flips then the spread crosses; assert ≥1 fill and that fills are at/through the ask (taker) or filled-while-resting (maker), not zero.
- **V6-D1** — `_read_table_checked` reads a no-`schema_version` fixture via upcast; `ec migrate-data` rewrites it; a future-version fixture still raises. End-to-end: `normalize`→`backtest` on `data/weather-overnight` completes.
- **V6-S1** — `ec new-strategy foo --archetype scalper` then `verify-strategy foo` **fails** until a Rust runtime exists; a strategy with an empty parity dir **fails**; verify runs `parity_check`.
- **V6-S2** — A strategy that only reacts to external signals without attaching a snapshot **fails** the no-trade smoke with `missing_market_snapshot` as the dominant reason.
- **V6-C1** — Partial fill 10/100; `ReplaceOrder` reprices the 90 tail; a fill arriving during the cancel window aborts the new order; reservation released only on terminal cancel.
- **V6-C2** — Leg A fills, leg B rejects → leg A auto-cancel/hedge; group timeout releases reservation.
- **V6-C3** — buy from fair 0.556 floors to 0.55; sell from 0.556 ceils to 0.56; promoted-strategy lint fails on raw `round()`.
- **V6-C4** — Offline fill + venue position; after reconcile local == venue; restart twice with same fills → daily loss not double-counted.
- **V6-Sp1** — `cargo bench` shows no "regressed" vs the committed v6 baseline; CI fails on >X% regression.
- **V6-D2** — `cargo bench -p eventcontracts-live-runner ws_lag_under_load` emits results; p99 ingest lag < threshold while a 200 ms REST submit runs.

### Full minimum-efficacy suite (run after every phase)
```bash
cd C:/QWS/eventcontracts
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace
make parity-check
cd python && python -m ruff check src tests && python -m mypy src/eventcontracts tests && python -m pytest tests -q && cd ..
# Research-loop smoke (must succeed end-to-end after P3):
python -m eventcontracts.cli normalize --data ../data/weather-overnight --source "*" --normalizer kalshi
python -m eventcontracts.cli backtest --strategy ../configs/strategies/weather-temperature-arbitrage.toml \
  --sleeve ../configs/sleeves/weather-kalshi-paper-a.toml --data ../data/weather-overnight
```

---

## §5. Final Live-Readiness Gate

A strategy may move from paper to live only when ALL hold:

1. Risk-reducing exits are never blocked by cash/exposure, in **both** languages (V6-T1), proven by a parity case.
2. Unrealized drawdown soft-halts (auto-clearing) and only realized loss permanently latches the kill switch (V6-T2), parity-proven.
3. The strategy actually fills against a moving book (no IOC-at-bid / phantom orders — V6-T4) and produces ≥1 risk-APPROVED intent in the verify smoke (V6-S2).
4. `verify-strategy` is green for real: instantiates in both languages, ≥1 parity case, `parity_check` passes (V6-S1).
5. Prices are discretized with the shared tick helpers and survive fees + the corrected sim (V6-C3, P4/P9).
6. Restart reconciliation is complete and idempotent (positions/balance/checkpoint/diff — V6-C4).
7. The research loop works on real captured data (V6-D1) and the WS-lag safety bench passes (V6-D2).

## §6. Empirical Run Log (this audit)

- **Test suites:** Python `307 passed` (7.6 s); Rust `cargo test --workspace` exit 0.
- **Research loop on real data — FAILS (V6-D1):**
  `ec normalize --data data/weather-overnight` →
  `ValueError: parquet schema_version mismatch ...: missing != 1` (`parquet_store.py:631`). 258 captured parquet files (2026-05-27) are unreadable by current code.
- **Risk-gate probe (`scripts/audit_probe.py`):**
  - Finding A (V6-T1): closing SELL, $5 cash, $90 notional → `REJECTED ('available_cash',)`.
  - Finding B (V6-T1): closing SELL at 90% gross cap → `REJECTED ('max_gross_exposure',)`.
  - Finding C (V6-T2): −$60 unrealized vs $50 cap → `total_loss_for = 60` (trips one-way latch); recover to $0 → `total_loss_for = 0` but switch stays tripped.
- **Benchmarks:** `external_kalshi_ws_normalize_strategy` = **26.96 µs**, `change +25.2%…+28.5% (p=0.00)`, criterion: *"Performance has regressed."* `ws_lag_under_load` emitted **no results** under `cargo bench --workspace` (V6-D2).
- **Coverage counts that drive findings:** `fair_price` emitted by 4/27 strategies (V6-T3); `market_snapshot` emitted by ~9/27 (rest rely on runner backfill — V6-S2); Rust `default_registry` = 3 named + 1 archetype for 27 Python strategies (V6-S3); `KNOWN_ARCHETYPES` lists 5, Rust implements 2 (V6-S1).

> The probe script `scripts/audit_probe.py` is a scratch artifact; convert its three assertions into real `tests/test_risk.py` cases during P1/P2, then delete it.

## §7. Out Of Scope For V6
New alpha research; Polymarket live execution; multi-account netting beyond the CEG design sketch (V6-C5); ML model retraining. Track separately.
