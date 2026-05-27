# Overnight no-trade live paper run

`eventcontracts live-paper` is the Stage 1 deliverable from
[live-deployment-remaining-roadmap.md](live-deployment-remaining-roadmap.md):
a long-running process that drives the existing
`WeatherTemperatureArbitrageStrategy` against **live Kalshi market data**
and **live Open-Meteo forecasts** in dry-run mode. No orders are ever
submitted to the venue.

The runner is intended to be launched alongside
[`capture-weather`](overnight-capture.md). They are complementary:

| Process | Outputs | Use for |
|---|---|---|
| `capture-weather` | Parquet event lake | backtests, parameter sweeps, parity tests |
| `live-paper` | decisions/risk/signals JSONL + manifest | live decision quality, latency, fill-rate planning |

## What it does

1. **Discovers** open weather markets every N seconds (`--rediscover-interval-seconds`).
2. **Subscribes** to Kalshi WS for those markets (ticker, trade, orderbook_delta, market_lifecycle_v2).
3. **Polls Open-Meteo** for each unique location of the discovered markets every N seconds (`--forecast-interval-seconds`).
4. **Converts** each forecast snapshot into per-market `ExternalSignalEvent`s
   using the existing `TemperatureThresholdModel` (Gaussian rules model).
5. **Feeds** both event streams (normalized Kalshi events + forecast signals)
   into `WeatherTemperatureArbitrageStrategy` (resolved from the spec via the
   strategy registry).
6. **Records** every `IntentEnvelope` and risk verdict to JSONL.
7. **Snapshots** stderr progress every N seconds (`--snapshot-interval-seconds`).
8. **Shuts down** cleanly on Ctrl-C / SIGTERM, flushes files, writes a manifest.

No orders are ever sent. There is no live `VenueGateway` imported by this
module by design.

## Launch (overnight)

```powershell
cd C:\QWS\eventcontracts
.venv\Scripts\eventcontracts.exe live-paper `
  --strategy configs\strategies\weather-temperature-arbitrage.toml `
  --sleeve   configs\sleeves\weather-kalshi-paper-a.toml `
  --out      data\live-paper-overnight `
  --max-duration-seconds 43200 `
  --rediscover-interval-seconds 600 `
  --forecast-interval-seconds 900 `
  --snapshot-interval-seconds 60 `
  2> data\live-paper-overnight\live-paper.log
```

`--max-duration-seconds 43200` = 12 hours hard cap.

Tail the log in another terminal to watch progress:

```powershell
Get-Content data\live-paper-overnight\live-paper.log -Wait -Tail 20
```

## Running both processes overnight

```powershell
# Terminal A: raw market data capture (for tomorrow's backtests)
.venv\Scripts\eventcontracts.exe capture-weather `
  --out data\weather-overnight `
  --max-duration-seconds 43200 `
  --rediscover-interval-seconds 600 `
  --snapshot-interval-seconds 60 `
  2> data\weather-overnight\capture.log

# Terminal B: live paper (decisions on live data)
.venv\Scripts\eventcontracts.exe live-paper `
  --strategy configs\strategies\weather-temperature-arbitrage.toml `
  --sleeve   configs\sleeves\weather-kalshi-paper-a.toml `
  --out data\live-paper-overnight `
  --max-duration-seconds 43200 `
  --rediscover-interval-seconds 600 `
  2> data\live-paper-overnight\live-paper.log
```

Both subscribe to the same markets; running them in parallel is intentional.
The capture is the durable record (replayable, deterministic); the live-paper
is the in-flight strategy log.

## Fast launch for known weather series

For the NYC hourly temperature ladder, use direct series discovery so
`initialized` markets are included before they turn active:

```powershell
.venv\Scripts\eventcontracts.exe live-paper `
  --strategy configs\strategies\weather-temperature-arbitrage.toml `
  --sleeve   configs\sleeves\weather-kalshi-paper-a.toml `
  --out      data\live-paper-nyc `
  --patterns "KXTEMP*" `
  --series-tickers "KXTEMPNYCH" `
  --rediscover-interval-seconds 60 `
  --forecast-interval-seconds 60 `
  --snapshot-interval-seconds 30 `
  --max-duration-seconds 43200 `
  --discover-timeout-seconds 45 `
  --discover-max-pages 1
```

## Output layout

```
data/live-paper-overnight/
└── run-20260527T140000000000Z/
    ├── manifest.json
    ├── decisions.jsonl          # one row per approved IntentEnvelope
    ├── risk_verdicts.jsonl      # one row per envelope, with allowed + reasons
    ├── external_signals.jsonl   # one row per forecast-derived signal
    └── forecast_snapshots.jsonl # one row per Open-Meteo poll
```

The manifest aggregates run-wide counters and the args used to launch.

## What "no weather markets open right now" looks like

Like the capture runner, at startup right now no `KXHIGH*` / `KXTEMP*` /
`KXLOW*` / `KXWX*` markets are open. The runner logs:

```
[live-paper] no markets match ['KXTEMP*', 'KXHIGH*', 'KXLOW*', 'KXWX*']; sleeping 600s before re-poll
[live-paper] elapsed=60s discoveries=1 markets=0 raw_ws=0 normalized=0 (0.0/s) signals=0 forecasts=1 ...
```

Open-Meteo continues polling the catalog's fallback locations
(currently NYC) so when markets open in the morning, the runner already
has fresh forecast state.

## Stopping

- **Ctrl-C** — graceful: finishes the in-flight event, flushes JSONL,
  writes manifest, exits 0.
- **`--max-duration-seconds`** — hard backstop (default 12h).

## MVP limits (called out in `manifest.json["limits"]`)

- **No fill simulation.** Decisions are recorded; no `PaperBroker` runs yet.
  After data is in tomorrow we can attach the existing `ExecutionSimulator`
  via `PaperIntentSink` for accurate fill-rate accounting.
- **No sequence-gap recovery on WS reconnect.** The underlying
  `KalshiWebSocketClient` reconnects with backoff; sequence gaps are logged
  in raw metadata (and visible in the manifest under `by_channel`), not
  surgically repaired.
- **No state checkpoints.** A restart loses in-process strategy state
  (mid by instrument, ladder accounting, retrade gate). For overnight this
  is fine; for multi-day continuous operation, add checkpointing.
- **Allow-all risk gate.** The sleeve's `risk` block (max order notional,
  daily loss, etc.) is **not enforced** by this MVP — every decision is
  recorded as "allowed". The next iteration wires a real risk gate.
- **Single venue (Kalshi).** No Polymarket support.

## After the run — what to do with the data

```powershell
# View the manifest
cat data\live-paper-overnight\run-*\manifest.json

# Count decisions by kind
cat data\live-paper-overnight\run-*\decisions.jsonl `
  | ConvertFrom-Json | Group-Object decision_kind | Select-Object Name,Count

# Verify Open-Meteo polling cadence
cat data\live-paper-overnight\run-*\forecast_snapshots.jsonl | Measure-Object -Line

# Backtest the same period using the captured raw data
.venv\Scripts\eventcontracts.exe backtest `
  --strategy configs\strategies\weather-temperature-arbitrage.toml `
  --sleeve   configs\sleeves\weather-kalshi-paper-a.toml `
  --data     data\weather-overnight `
  --out      artifacts\reports\overnight-weather-backtest.json
```

The captured raw lake + the live decisions JSONL together let you
parity-check: "does replaying yesterday's market data produce the same
decisions the live-paper recorded?" That's the foundation of the runner
parity gate in
[docs/live-rust-runner-roadmap.md](live-rust-runner-roadmap.md).
