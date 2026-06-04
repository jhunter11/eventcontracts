# MLB Spread Production Readiness

Date: 2026-06-04.

## Status

Not production ready. The read-only MLB spread producer can generate paper
signals and the June 3 snapshot had positive hold-to-settlement PnL, but the
promotion gate still fails.

Current evidence:

| Evidence | Result | Interpretation |
| --- | ---: | --- |
| Fee-net candidates | 17 | Enough to justify more paper capture. |
| Theoretical expected profit | +$210.5450 at 100 paper contracts each | Model-vs-market gap only; not executable edge. |
| Immediate markout | -$61.2182 at 100 paper contracts each | Blocks promotion. Every candidate marked out negative. |
| Markout horizon | 21.1709 seconds after entry | Too early for the 60-second minimum horizon gate. |
| Settlement | +$137.7818 at 100 paper contracts each | Encouraging, but only 17 rows. |
| Upstream source timestamps | 0/17 candidates | ESPN odds timestamp is received-at/proxy-only. |
| ESPN timestamp audit | 0 payload timestamp fields | Only HTTP Date/cache headers were present. |
| Source-gated producer | 0 candidates, 0 signal rows | 33 positive-edge rows were blocked as `source_timestamp_missing`. |
| Source-gated readiness gate | not ready, 4 blockers | Safe no-signal state; not promotion evidence. |
| Compute median | 0.0497 ms | Compute is effectively free. |
| Full public-read loop with depth | 4248.0342 ms | Bounded concurrency helps, but full-series depth reads fail the 1000 ms gate. |
| Selective public-read loop with depth | 1784.9924 ms | Fetches 25 candidate orderbooks, better but still over budget. |
| Source-gated selective loop | 745.1031 ms | Fetches 0 orderbooks because source freshness blocks candidates. |
| Public-read loop without depth | 703.9952 ms | Lower bound only; not enough for production because executable size is missing. |

The generated gate artifact is
`live-test/mlb-spread-production-readiness.md`. Its blockers are:

- `source_timestamp_required_by_validation`
- `upstream_source_timestamps_present`
- `markout_horizon_covered`
- `positive_markout`
- `positive_settlement`
- `latency_budget`

The gate separately checks `executable_depth_bench_present`, so a fast
no-depth benchmark cannot satisfy production readiness by omitting tradability
evidence.

The source timestamp evidence is recorded in
`live-test/mlb-spread-espn-odds-timestamp-audit.md`. It found 0 timestamp-like
fields in the ESPN odds payload for event `401815621`; HTTP `Date` and
`Cache-Control` were present but are transport/cache metadata rather than
bookmaker source freshness.

The source-gated validation artifact is
`live-test/mlb-spread-source-gated-validation.md`. It was run with
`--require-source-timestamp --selective-orderbooks`, evaluated 51 markets across
9 reports, found 33 positive fee-net rows, blocked all of them as
`source_timestamp_missing`, fetched no orderbook depth, and wrote an empty signal
JSONL.

The source-gated readiness artifact is
`live-test/mlb-spread-source-gated-production-readiness.md`. In this packet the
empty signal JSONL is compatible because validation emitted 0 candidates, and
the selective benchmark's 0 orderbooks are compatible because source freshness
blocked every preliminary candidate. The gate still fails closed with blockers
`fee_net_candidates_present`, `upstream_source_timestamps_present`,
`positive_markout`, and `positive_settlement`. This is a safe paper/shadow
no-signal state, not proof of edge.

## Required Before Promotion

1. Source freshness must be real. Record upstream odds source timestamps or
   update sequence IDs, plus local received timestamps. Missing or proxy-only
   timestamps must fail candidate generation.
2. Markout must turn positive out of sample. Measure at minimum immediate,
   +60s, +180s, +300s, close, and settlement. Bucket by sport, time remaining,
   spread threshold, side, net edge, odds provider, and liquidity.
3. Markout horizons must be explicit. The current strict pass has a 21.1709s
   markout, which is useful but below the 60s readiness gate; append future
   markouts to `live-test/mlb-spread-live-edge-strict-100c-markout-ledger.jsonl`
   or a session-specific ledger.
4. Settlement sample must be larger and walk-forward. The current positive
   settlement result is 17 rows, below the 20-row minimum gate and far below a
   production sample.
5. Latency path must be rebuilt or bounded. Current public REST/orderbook reads
   are network-bound even with bounded orderbook concurrency. Selective depth
   reduces the exploratory path from 4248.0342 ms to 1784.9924 ms, but the
   production answer still needs streaming or batched public market data before
   CPU, host placement, or thread settings matter.
6. Fillability must be measured in paper. The model should compare executable
   touch, displayed depth, queue/fill assumptions, fees, slippage, and market
   moves after the decision timestamp.
7. Baselines must be reported. Compare against market-implied no-trade,
   scoreboard probability, simple persistence, and provider-consensus baselines
   using Brier/log loss, calibration/ECE, and tradable markout.
8. Runtime parity must stay explicit. Python signal payloads and Rust
   external-signal consumption must preserve market IDs, probability, source,
   timestamp, max age, and paper-only operating mode.

## Latency And Compute Notes

The 2026-06-04 live public-read benchmark with executable depth and
`--orderbook-concurrency 16 --orderbook-pause-seconds 0`:

- ESPN scoreboard: 149.2216 ms
- ESPN odds total: 716.5910 ms
- Kalshi markets: 77.5735 ms
- Kalshi orderbooks total: 3299.6983 ms
- End-to-end: 4248.0342 ms
- Median local valuation: 0.0497 ms

The 2026-06-04 selective-depth benchmark with
`--selective-orderbooks --orderbook-concurrency 16 --orderbook-pause-seconds 0`:

- Preliminary reports: 9
- Preliminary candidates: 25
- Orderbooks requested: 25
- End-to-end: 1784.9924 ms
- Kalshi orderbooks total: 1172.7247 ms
- Median local valuation: 0.3102 ms

The source-gated selective benchmark with `--require-source-timestamp`:

- Preliminary reports: 9
- Preliminary candidates: 0
- Orderbooks requested: 0
- End-to-end: 745.1031 ms
- Median local valuation: 0.2969 ms

This is a read-path measurement, not submit latency and not a matching-engine
measurement. The result says the current workflow is dominated by public data
fetching, especially per-market orderbook reads. The no-depth lower bound was
703.9952 ms, but that omits executable-size evidence and cannot support a
production decision. Faster compute will not fix the edge question. The next
engineering work should prioritize source freshness, streaming/batched market
data, and measured markout.

## Next Validation Packet

Produce one packet per live session:

- validation JSON/MD and signal JSONL
- source timestamp coverage summary
- ESPN/source timestamp audit JSON/MD when a provider endpoint is used for the
  first time or its payload shape changes
- public quote/orderbook snapshot at decision time
- markout report across multiple horizons
- final settlement report
- latency report from the same session
- readiness report with the default fail-closed gate

The validation command for production-mode packets must require upstream source
timestamps:

```powershell
.venv\Scripts\python.exe python\scripts\mlb_spread_edge.py validate-once --espn-date 20260604 --series-ticker KXMLBSPREAD --min-net-edge 0.05 --min-executable-size 50 --paper-contracts 100 --require-source-timestamp --selective-orderbooks --orderbook-concurrency 16 --orderbook-pause-seconds 0 --report-json live-test\mlb-spread-source-gated-validation.json --report-md live-test\mlb-spread-source-gated-validation.md --signals-jsonl-out live-test\mlb-spread-source-gated-validation-signals.jsonl
```

Run the readiness gate without `--allow-not-ready` when a packet is being used
as a promotion gate; it must exit non-zero until every blocker clears:

```powershell
.venv\Scripts\python.exe python\scripts\mlb_spread_edge.py readiness --validation-report-json live-test\mlb-spread-source-gated-validation.json --markout-report-json live-test\mlb-spread-source-gated-markout-missing.json --settlement-report-json live-test\mlb-spread-source-gated-settlement-missing.json --bench-report-json live-test\mlb-spread-bench-source-gated-selective.json --signals-jsonl live-test\mlb-spread-source-gated-validation-signals.jsonl --report-json live-test\mlb-spread-source-gated-production-readiness.json --report-md live-test\mlb-spread-source-gated-production-readiness.md
```

After a live validation pass, collect production-relevant markouts with:

```powershell
.venv\Scripts\python.exe python\scripts\mlb_spread_edge.py markout-horizons --entry-report-json live-test\mlb-spread-live-edge-strict-100c.json --espn-date 20260604 --series-ticker KXMLBSPREAD --horizons-seconds 60,180,300 --max-wait-seconds 300 --orderbook-concurrency 16 --orderbook-pause-seconds 0 --markout-ledger-jsonl-out live-test\mlb-spread-live-edge-strict-100c-markout-ledger.jsonl --report-dir live-test --report-prefix mlb-spread-live-edge-strict-100c-markout-horizon --report-json live-test\mlb-spread-live-edge-strict-100c-markout-horizons.json --report-md live-test\mlb-spread-live-edge-strict-100c-markout-horizons.md
```

The command remains public/read-only. It writes one report per horizon plus a
bundle summary and append-only ledger. The fixture smoke artifacts are
`live-test/mlb-spread-fixture-markout-horizons.md` and
`live-test/mlb-spread-fixture-markout-horizons-ledger.jsonl`.

Only after those packets show positive, timestamp-clean, post-cost markout and
settlement across enough games should this move beyond paper/shadow validation.
