# AGENTS.md — eventcontracts BTC settlement-arb validation (EC2 us-east-1)

You are a Codex agent running on an **AWS EC2 instance in us-east-1**, the same
region Kalshi's API is hosted in. Your job is a **latency + edge-existence
measurement** for a 15-minute Kalshi BTC settlement-arbitrage thesis. You are
here because us-east-1 is physically close to Kalshi (~16 ms from the dev's home
box; likely low-single-digit ms from here) — proving/refuting that gain is task 1.

## HARD BOUNDARIES (non-negotiable)

1. **NO TRADING. NO ORDERS. EVER.** Kalshi credentials ARE present on this box
   (`.env` + `ecmodel.txt` private key) — but their presence does NOT authorize
   trading. Every task below uses **public, read-only** data; the keys are here
   only so authenticated *reads* are possible if a task explicitly calls for one.
   You must **never** call any order-submit / cancel / place endpoint, and never
   run the live-runner with `--live-submit`. There is no proven edge yet (that is
   what these tasks measure); placing an order is out of scope and forbidden. If a
   task seems to need an order, **STOP and report**.
2. **Read-only market data.** Coinbase/Kraken/Kalshi *public* REST + WS need no
   auth — prefer them. Authenticated Kalshi *reads* (balance, positions, fills)
   are permitted ONLY if a task explicitly needs them; authenticated *writes*
   (orders) are never permitted.
3. **Prove before expand.** Calibration ≠ edge. A model-vs-market gap is NOT edge
   until proven against the *tradable* moment, net of fees + spread, with a
   non-stale input. Apparent edges are first-guess **measurement defects** (stale
   `c`, or Coinbase≠CFB-RTI basis) — see the staleness bug that faked a +$101
   "edge" last session. Treat every gap as guilty until proven.
4. **Verify your own work.** `ruff check` + `mypy` clean and `pytest` green on
   anything you touch, before you claim it works. Report real exit codes, not
   intentions.

## CONTEXT: what's already built and proven (under `python/`)

- `src/eventcontracts/research/btc_settlement.py` — the settlement convergence
  kernel. Asian-average model `V ~ N(mu_V, s_V)`, `s_V = sigma·tau^1.5/(T·sqrt3)`,
  `forecast_at(seconds_to_expiry, c, sigma, partial_sum=None)` spans the whole
  15-min life (before-window level diffusion `u>=60` seam→ in-window average
  `u<60`), `prob_yes_at`, `implied_sigma_per_sec`, `delta_to_index` (dP/dc ∝
  1/sqrt(tau)). Verified by `tests/test_btc_settlement.py` (7 tests: seam
  continuity, tau^1.5 lock, Monte-Carlo agreement, implied-vol round-trip).
- `scripts/btc_settlement_gap.py` — model-vs-market gap recorder vs the live
  `KXBTC15M` market. Already hardened: real-time spot (not stale candle),
  `spot_age_sec`/`spot_stale` gating, `index_basis=coinbase_spot_vs_cfb_rti_unmeasured`
  tag on every row. Appends `live-test/btc15m_gap_ledger.jsonl`.
- `scripts/btc_settlement_bench.py` — latency decomposition (compute vs network).
  From the dev's home box: compute ~0.6 us/reprice; Coinbase ticker ~34 ms;
  Kalshi markets ~33 ms. Conclusion there: compute is free, **network is the
  binding constraint**, cache/thread-pinning is the WRONG lever.

## YOUR TASKS (in order — each gates the next; do not skip ahead)

### Task 1 — Does us-east-1 co-location actually cut the latency?
Run `python/scripts/btc_settlement_bench.py` from THIS box and compare the
network legs to the home-box baseline (Coinbase ~34 ms, Kalshi ~33 ms).
- Report median/min RTT to Kalshi and Coinbase from us-east-1.
- **Honesty caveat to preserve:** the Kalshi REST number is a *read-RTT proxy*,
  CDN/anycast-fronted — it measures distance to the nearest edge PoP, not the
  matching engine, and not the authenticated+rate-limited write path. State this;
  do not overclaim it as "submit latency."
- **Decision gate:** if us-east-1 does NOT materially beat the home box, the
  co-location premise is dead — say so plainly and stop hyping the cloud angle.

### Task 2 — Is there even a race to win? (the c-lead recorder — THE decisive build)
Build `python/scripts/btc_clead_recorder.py` (new):
- Subscribe to **Coinbase + Kraken public WebSocket** trade/ticker feeds (push,
  not REST poll — the REST 34 ms ceiling is exactly what you're escaping).
- Compute a **synthetic BTC index** `c_synth` from the constituents you can reach
  (document which CFB constituents are US-accessible and which are not).
- Log `c_synth` and its update timestamps to a JSONL ledger. Where an official CF
  Benchmarks RTI print is obtainable, log the **lead** (`c_synth` timestamp vs
  RTI-print timestamp, in ms). If the official RTI print is NOT obtainable
  read-only from here, **say so** and record the best available proxy, clearly
  labeled — do not fabricate a lead number.
- ruff + mypy clean, with a `--no-network` / short self-test path.
- **The one number that decides everything:** is the synthetic-index lead over
  the official print reliably positive AND larger than the residual submit RTT
  from Task 1? If not, there is no edge and the execution thread is never worth
  building. Report this conclusion explicitly, with data.

### Task 3 — Re-run the gap recorder from us-east-1, honestly
Run `btc_settlement_gap.py --series KXBTC15M` against the live market from here.
A flagged gap is a **candidate, not edge**, until the Coinbase-vs-CFB-RTI basis
(Task 2) is measured. Keep that label. Accumulate a ledger; do not declare edge.

## REPORTING
Write findings to `live-test/ec2-findings.md` as you go: each task's real numbers,
the decision-gate verdict, and what (if anything) the next rung justifies. Honest
negative results are the goal here — "co-lo doesn't help" or "no positive lead"
are valuable, publishable conclusions. Do not manufacture an edge to please anyone.

## ENV / RUNNING
- Repo synced to `~/eventcontracts`. Python 3.11 venv at `.venv`.
- `pip install -e python[dev]` already run by bootstrap. Activate: `source .venv/bin/activate`.
- Sanity: `pytest python/tests/test_btc_settlement.py -q` must be green before you start.
