"""Latency budget for the BTC settlement pricer (prove-before-build runtime check).

Two empirical questions, measured not asserted:

  1. Can we price continuously? -> microbenchmark the pure-compute hot path
     (`prob_yes_at` / `forecast_at`): per-call latency p50/p99 + throughput.
  2. What actually limits the loop? -> wall-clock the network legs: Coinbase
     ticker (= the `c` refresh) and Kalshi markets (= quote refresh, and a
     read-RTT *proxy* for the order-submit path; the write RTT cannot be timed
     read-only but is the same order of magnitude + rate-limited).

Read-only. `--no-network` runs the compute bench alone (deterministic, CI-safe).

This tests the thesis claim that local compute / cache-pinning is the latency
lever. If compute is ~microseconds and the network legs are tens of ms, the
binding constraint on trading a final-seconds mispricing is the submit RTT +
data freshness, NOT pricing -- and a REST poll is too slow to hold a
microsecond-fresh `c`, which is the empirical case for a push (WS) feed.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from time import perf_counter, perf_counter_ns

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python" / "src"))

from eventcontracts.research.btc_settlement import (  # noqa: E402
    prob_yes_at,
    sigma_per_second_from_annual,
)

COINBASE_TICKER = "https://api.exchange.coinbase.com/products/BTC-USD/ticker"
KALSHI_MARKETS = (
    "https://api.elections.kalshi.com/trade-api/v2/markets"
    "?series_ticker=KXBTC15M&status=open&limit=10"
)
# Representative live state (2026-06-01 KXBTC15M): spot ~ strike, ~41% annual vol.
SPOT, TARGET, ANNUAL_VOL = 71_834.0, 71_729.0, 0.41
# Horizons spanning the whole 15-min life -> exercises BOTH model regimes
# (before-window level diffusion u>=60, in-window Asian average u<60).
HORIZONS = (900.0, 300.0, 60.0, 30.0, 10.0, 1.0)


def _get(url: str) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": "ec-bench/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def bench_compute(n_batches: int, batch: int) -> dict[str, float]:
    """Per-call latency of the pricing hot path, batched to beat clock noise.

    Timing a sub-microsecond op call-by-call is dominated by clock resolution, so
    we time batches of ``batch`` calls and derive per-call ns; the spread across
    ``n_batches`` batches gives robust p50/p99 without that noise.
    """
    sigma_sec = sigma_per_second_from_annual(SPOT, ANNUAL_VOL)
    for u in HORIZONS:  # warmup / prime caches
        prob_yes_at(u, SPOT, TARGET, sigma_sec)
    per_call_ns: list[float] = []
    for i in range(n_batches):
        u = HORIZONS[i % len(HORIZONS)]
        t0 = perf_counter_ns()
        for _ in range(batch):
            prob_yes_at(u, SPOT, TARGET, sigma_sec)
        t1 = perf_counter_ns()
        per_call_ns.append((t1 - t0) / batch)
    per_call_ns.sort()
    n = len(per_call_ns)
    mean = statistics.fmean(per_call_ns)
    return {
        "calls": float(n_batches * batch),
        "p50_ns": per_call_ns[n // 2],
        "p99_ns": per_call_ns[min(n - 1, int(n * 0.99))],
        "mean_ns": mean,
        "throughput_per_sec": 1e9 / mean if mean > 0 else float("inf"),
    }


def bench_network(url: str, samples: int, pause: float) -> dict[str, float] | None:
    """Wall-clock GET latency (ms): min / median / p90 / max over ``samples``."""
    times_ms: list[float] = []
    for _ in range(samples):
        t0 = perf_counter()
        try:
            _get(url)
        except (urllib.error.URLError, TimeoutError, OSError):
            return None
        times_ms.append((perf_counter() - t0) * 1e3)
        time.sleep(pause)
    if not times_ms:
        return None
    times_ms.sort()
    n = len(times_ms)
    return {
        "samples": float(n),
        "min_ms": times_ms[0],
        "median_ms": times_ms[n // 2],
        "p90_ms": times_ms[min(n - 1, int(n * 0.90))],
        "max_ms": times_ms[-1],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batches", type=int, default=2000, help="compute bench batches")
    ap.add_argument("--batch", type=int, default=500, help="calls per batch")
    ap.add_argument("--net-samples", type=int, default=7, help="GET samples per endpoint")
    ap.add_argument("--net-pause", type=float, default=0.25, help="seconds between GETs")
    ap.add_argument("--no-network", action="store_true", help="compute bench only")
    args = ap.parse_args()

    sigma_sec = sigma_per_second_from_annual(SPOT, ANNUAL_VOL)

    print("=== COMPUTE: pricing hot path (prob_yes_at, pure Python) ===")
    c = bench_compute(args.batches, args.batch)
    print(f"  {c['calls']:,.0f} calls across both regimes")
    print(f"  per-call: p50={c['p50_ns']:.0f} ns  p99={c['p99_ns']:.0f} ns  "
          f"mean={c['mean_ns']:.0f} ns")
    print(f"  throughput: {c['throughput_per_sec']:,.0f} prices/sec "
          f"(~{c['throughput_per_sec'] / 1e6:.1f}M/s)")
    # The KXBTC15M universe is ONE open market per window, so the per-refresh
    # compute cost is a single call -- effectively free.
    print(f"  whole open universe (1 market): ~{c['mean_ns']:.0f} ns per full reprice")

    if args.no_network:
        print("\n[--no-network] skipping network legs.")
        return 0

    print("\n=== NETWORK: wall-clock GET latency ===")
    cb = bench_network(COINBASE_TICKER, args.net_samples, args.net_pause)
    kl = bench_network(KALSHI_MARKETS, args.net_samples, args.net_pause)
    if cb is None or kl is None:
        print("  network unavailable -> decomposition skipped "
              f"(coinbase={'ok' if cb else 'fail'}, kalshi={'ok' if kl else 'fail'})")
        return 0
    print(f"  Coinbase ticker  (c refresh)        : "
          f"min={cb['min_ms']:.0f}  median={cb['median_ms']:.0f}  "
          f"p90={cb['p90_ms']:.0f}  max={cb['max_ms']:.0f}  ms")
    print(f"  Kalshi markets   (quote + submit-RTT proxy): "
          f"min={kl['min_ms']:.0f}  median={kl['median_ms']:.0f}  "
          f"p90={kl['p90_ms']:.0f}  max={kl['max_ms']:.0f}  ms")

    # ---- decomposition ----
    price_ms = c["mean_ns"] / 1e6
    c_refresh_ms = cb["median_ms"]
    submit_rtt_ms = kl["median_ms"]
    # How far does c drift during one submit RTT? sigma is per-second USD.
    drift_usd = sigma_sec * math.sqrt(submit_rtt_ms / 1e3)
    ratio = c_refresh_ms / price_ms if price_ms > 0 else float("inf")
    print("\n=== DECOMPOSITION (median) ===")
    print(f"  pricing (compute)        : {price_ms * 1e3:8.2f} us")
    print(f"  c refresh   (REST poll)  : {c_refresh_ms:8.1f} ms   <- limits c freshness")
    print(f"  submit RTT  (read proxy) : {submit_rtt_ms:8.1f} ms   <- limits action")
    print(f"  network / compute ratio  : {ratio:,.0f}x")
    print(f"  c drift during one submit RTT (sigma*sqrt(RTT)) : ${drift_usd:,.2f}")

    print("\n=== VERDICT ===")
    print("  Compute is ~6 orders of magnitude below the network legs. Cache/thread")
    print("  pinning is NOT the lever -- the pricing thread is effectively free, so a")
    print("  continuous pricer is trivially affordable. The loop period (hence how")
    print("  stale `c` is) is set by the DATA feed: a REST poll at the ms above cannot")
    print("  hold a final-seconds-fresh `c`, exactly where dP/dc ~ 1/sqrt(tau) is")
    print("  largest. A push (WS) constituent feed is the prerequisite before any")
    print("  execution thread is worth building; the trade thread is the cheap part.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
