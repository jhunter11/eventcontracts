# Overnight Kalshi weather-market data capture

`eventcontracts capture-weather` is a long-running data recorder that:

- discovers open Kalshi markets matching weather ticker patterns,
- subscribes to WS `ticker`/`trade`/`orderbook_delta`/`market_lifecycle_v2`,
- writes raw envelopes to the standard Parquet event lake,
- re-discovers markets periodically (weather markets open through the morning),
- prints a progress snapshot to stderr on a fixed interval,
- shuts down cleanly on Ctrl-C / SIGTERM and writes a final manifest.

The output is read-compatible with the existing `eventcontracts inspect-data`,
`eventcontracts normalize`, `eventcontracts replay`, and `eventcontracts
backtest` commands. Once data is in, backtests work the same way as historical
weather research.

## Launch (overnight, 12-hour cap, prod read-only)

```powershell
cd C:\QWS\eventcontracts
.venv\Scripts\eventcontracts.exe capture-weather `
  --out data\weather-overnight `
  --max-duration-seconds 43200 `
  --rediscover-interval-seconds 600 `
  --snapshot-interval-seconds 60
```

(Or POSIX-style if you're in bash.)

That gives you 12 hours, re-discovering open weather markets every 10 minutes,
with a progress line on stderr every 60s.

## Fast launch for known weather series

When the exact weather series is known, prefer direct series discovery. This
avoids scanning unrelated open markets and also includes `initialized` markets,
so the WebSocket can subscribe before the market turns active:

```powershell
.venv\Scripts\eventcontracts.exe capture-weather `
  --out data\weather-overnight `
  --patterns "KXTEMP*" `
  --series-tickers "KXTEMPNYCH" `
  --rediscover-interval-seconds 60 `
  --max-duration-seconds 43200 `
  --snapshot-interval-seconds 30 `
  --idle-poll-seconds 5 `
  --discover-timeout-seconds 45 `
  --discover-max-pages 1
```

## Recommended launch (overnight)

```powershell
.venv\Scripts\eventcontracts.exe capture-weather `
  --out data\weather-overnight `
  --patterns "KXHIGH*,KXTEMP*,KXWX*,KXLOW*" `
  --rediscover-interval-seconds 600 `
  --max-duration-seconds 43200 `
  --snapshot-interval-seconds 60 `
  --idle-poll-seconds 60 `
  --discover-timeout-seconds 20 `
  --discover-max-pages 5 `
  2> data\weather-overnight\capture.log
```

That redirects the stderr snapshots into a log file. Tail it in another
terminal:

```powershell
Get-Content data\weather-overnight\capture.log -Wait -Tail 20
```

## What "no weather markets right now" looks like

Tonight at run-time none of `KXHIGH*` / `KXTEMP*` / `KXWX*` / `KXLOW*` are
open. The runner correctly reports:

```
[capture-weather] no markets match ['KXHIGH*', ...] right now; sleeping 60s before re-poll
[capture-weather] elapsed=60s sessions=0 envelopes=0 (0.0/s) discovered=0 ...
```

It'll keep re-discovering on the configured interval, so the moment the first
weather market opens tomorrow morning, it picks it up automatically.

## Stopping

- **Ctrl-C** in the terminal: graceful — finishes the current session, flushes
  the Parquet buffer, writes the manifest, exits 0.
- **Hard cap**: `--max-duration-seconds` (default 12h) is a backstop so the
  process can't leak indefinitely.
- **From a separate terminal**: `taskkill /PID <pid> /F` sends SIGTERM which
  the runner also handles. On Windows the PID prints implicitly if you started
  via `Start-Process`.

## Disk usage estimate

Per-event Parquet rows are small (snappy-compressed). On a typical weather
trading day with ~50 open markets and ~5 events/sec/market, expect:

- ~50 markets × 5 ev/s × 3600 s = 900k events/hr
- ~80 bytes/row compressed → ~70 MB/hr → **~700 MB for a 10-hour run**

For comfort, reserve **1.5 GB** of free disk under `data/weather-overnight/`.

## Output layout

```
data/weather-overnight/
├── raw/
│   └── venue=kalshi/
│       └── source=kalshi-ws/
│           └── date=2026-05-27/
│               ├── part-0000.parquet
│               ├── part-0001.parquet
│               └── ...
└── manifests/
    └── capture-weather-20260527T140000000000Z.json
```

Multiple capture sessions over the night accumulate into the same `raw/`
partition tree. The manifest summarizes the full run including per-channel
and per-ticker envelope counts.

## After the run — using the data

Inspect counts:

```powershell
.venv\Scripts\eventcontracts.exe inspect-data `
  --data data\weather-overnight `
  --source kalshi-ws
```

Normalize the raw lake (idempotent, can re-run):

```powershell
.venv\Scripts\eventcontracts.exe normalize `
  --data data\weather-overnight `
  --source kalshi-ws
```

Replay through an existing weather strategy spec:

```powershell
.venv\Scripts\eventcontracts.exe backtest `
  --strategy configs\strategies\weather-temperature-arbitrage.toml `
  --sleeve   configs\sleeves\weather-kalshi-paper-a.toml `
  --data     data\weather-overnight `
  --latency-ms 250 `
  --queue-fraction 1.0 `
  --starting-equity 10000 `
  --out artifacts\reports\weather-overnight-backtest.json
```

Strategies that depend on external forecast signals
(`weather_temperature_arbitrage`) need a separate Open-Meteo signal lake —
that's the **next deliverable**, the live-paper runner. Until then, this
capture gives you raw market microstructure data which is the substrate for
parameter tuning, depth profiles, and parity tests.

## Limits / known gaps

- **Single venue, Kalshi only.** Polymarket weather markets aren't covered.
- **No reconnect-resubscribe with sequence-gap recovery yet.** The underlying
  `KalshiWebSocketClient` reconnects with exponential backoff if the WS drops,
  but doesn't re-request missed sequence numbers — a multi-second gap is
  visible in the data as a hole in sequence numbers. For backtests this is
  recoverable noise; for production replay-determinism it isn't.
- **`status="open"` filter only.** Markets that flip to `paused` or `closed`
  during the run stay in the subscription set until the next re-discovery.
- **Re-discovery sends a fresh subscribe.** It disconnects and reconnects each
  cycle — a few seconds of gap on the rediscover boundary. For 10-minute
  cadence this is <1% data loss.
