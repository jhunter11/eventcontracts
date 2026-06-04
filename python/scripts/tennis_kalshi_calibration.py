"""Are there meaningful gaps between Kalshi's tennis pricing and true probability?

Uses REAL Kalshi minute candlesticks + ground-truth settlement for the KXATPMATCH
series. Both Kalshi's price and our Markov model make the same claim — "this player
wins with prob p" — so we test them on identical ground truth (who actually won).

IMPORTANT data note: Kalshi tennis history starts 2026-03; our point-by-point data
(for the in-match Markov model) ends 2024, so there is NO match in both datasets —
a literal time-aligned "Kalshi vs our in-match curve" overlay is impossible without
fabricating data. Instead we measure each side's calibration against settlement:

  1. Kalshi IN-PLAY calibration: across all settled markets, at YES mid-price p,
     what fraction actually settle YES? (Perfect market => the diagonal.)
  2. Comeback rate at the first IN-PLAY crossing of a threshold T (mirrors the
     model's `--analyze thresholds`), Kalshi vs the model's slam-measured curve.
  3. A few example match price paths (Kalshi minute YES price, settlement marked).

In-play is isolated via each market's occurrence_datetime (scheduled match start).
Fetched candles are cached to parquet so re-runs are offline. Read-only (auth GET).
"""

from __future__ import annotations

import argparse
import asyncio
import io
import os
import statistics
import sys
from datetime import datetime
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
ROOT = Path(__file__).resolve().parents[2]
for _line in (ROOT / ".env").read_text().splitlines():
    _line = _line.strip()
    if _line and not _line.startswith("#") and "=" in _line:
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k, _v.strip().strip('"').strip("'"))
sys.path.insert(0, str(ROOT / "python" / "src"))

import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import polars as pl  # noqa: E402

from eventcontracts.adapters.venues.kalshi.client import KalshiPublicClient  # noqa: E402

SERIES = "KXATPMATCH"
# Model comeback rates (calibrated `best` engine, slam point-by-point) for overlay.
MODEL_COMEBACK = {0.70: 0.262, 0.75: 0.233, 0.80: 0.197, 0.85: 0.158, 0.90: 0.113, 0.95: 0.067}
THRESHOLDS = (0.70, 0.75, 0.80, 0.85, 0.90, 0.95)


def _f(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v


def _iso(s: str | None) -> int | None:
    if not s:
        return None
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


async def _fetch_markets(c: KalshiPublicClient) -> list[dict]:
    out: list[dict] = []
    cursor = None
    while True:
        r = await c.get_markets_payload(series_ticker=SERIES, status="settled", limit=1000, cursor=cursor)
        ms = r.get("markets", [])
        out.extend(ms)
        cursor = r.get("cursor")
        if not cursor or not ms:
            break
    return out


async def _fetch_candles(c: KalshiPublicClient, ticker: str, start_ts: int, end_ts: int, sem) -> list[dict]:
    async with sem:
        for attempt in range(3):
            try:
                r = await c._get(  # noqa: SLF001
                    f"/series/{SERIES}/markets/{ticker}/candlesticks",
                    params={"start_ts": start_ts, "end_ts": end_ts, "period_interval": 1},
                )
                return r.get("candlesticks", []) or []
            except Exception as exc:  # noqa: BLE001
                if "429" in str(exc) and attempt < 2:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                return []
    return []


async def _build_cache(cache: Path, max_markets: int | None) -> pl.DataFrame:
    c = KalshiPublicClient.from_env()
    markets = await _fetch_markets(c)
    markets = [m for m in markets if m.get("result") in ("yes", "no")]
    if max_markets:
        markets = markets[:max_markets]
    print(f"settled markets with a result: {len(markets)}")
    sem = asyncio.Semaphore(6)
    tasks, meta = [], []
    for m in markets:
        occ = _iso(m.get("occurrence_datetime"))
        close = _iso(m.get("close_time"))
        if close is None:
            continue
        start = (occ - 300) if occ else (close - 4 * 3600)
        tasks.append(_fetch_candles(c, m["ticker"], start, close + 120, sem))
        meta.append((m["ticker"], 1 if m["result"] == "yes" else 0, occ, close,
                     m.get("yes_sub_title", ""), m.get("title", "")))
    results = await asyncio.gather(*tasks)
    rows = []
    for (ticker, settled_yes, occ, close, yes_player, title), candles in zip(meta, results, strict=True):
        for cd in candles:
            ts = cd.get("end_period_ts")
            bid = _f((cd.get("yes_bid") or {}).get("close_dollars"))
            ask = _f((cd.get("yes_ask") or {}).get("close_dollars"))
            if ts is None or bid is None or ask is None or ask < bid:
                continue
            if not (bid > 0.0 and ask < 1.0):  # drop one-sided / settled-boundary candles
                continue
            mid = (bid + ask) / 2.0
            rows.append({"ticker": ticker, "ts": ts, "yes_mid": mid, "settled_yes": settled_yes,
                         "in_play": bool(occ and ts >= occ), "to_close_s": close - ts,
                         "yes_player": yes_player, "title": title})
    df = pl.DataFrame(rows)
    df.write_parquet(cache)
    print(f"cached {df.height:,} candle-minutes from {df['ticker'].n_unique()} markets -> {cache}")
    return df


def _calibration(df: pl.DataFrame) -> None:
    print("\n=== KALSHI IN-PLAY CALIBRATION (YES mid-price bucket -> realized YES rate) ===")
    ip = df.filter(pl.col("in_play"))
    print(f"  in-play candle-minutes: {ip.height:,} across {ip['ticker'].n_unique()} markets")
    for i in range(10):
        lo, hi = i / 10, (i + 1) / 10
        sel = ip.filter((pl.col("yes_mid") >= lo) & (pl.col("yes_mid") < hi))
        if not sel.height:
            continue
        print(f"    [{lo:.1f},{hi:.1f}) n={sel.height:7d}  price={sel['yes_mid'].mean():.3f}  "
              f"settled_yes={sel['settled_yes'].mean():.3f}")


def _comeback(df: pl.DataFrame) -> dict[float, tuple[int, float, float]]:
    """First IN-PLAY crossing of T by the leader; realized leader-win rate + avg price."""
    print("\n=== KALSHI COMEBACK at first IN-PLAY threshold crossing (vs MODEL) ===")
    out: dict[float, tuple[int, float, float]] = {}
    per_market = {tk: g.sort("ts") for tk, g in df.filter(pl.col("in_play")).group_by("ticker")}
    for t in THRESHOLDS:
        leader_won, prices = [], []
        for g in per_market.values():
            mids = g["yes_mid"].to_list()
            settled_yes = g["settled_yes"][0]
            idx = next((j for j, p in enumerate(mids) if p >= t or p <= 1 - t), None)
            if idx is None:
                continue
            p = mids[idx]
            leader_yes = p >= 0.5
            prices.append(p if leader_yes else 1 - p)
            leader_won.append(1 if (leader_yes == (settled_yes == 1)) else 0)
        if not leader_won:
            continue
        wr = statistics.fmean(leader_won)
        mp = statistics.fmean(prices)
        out[t] = (len(leader_won), wr, mp)
        kc, mc = 1 - wr, MODEL_COMEBACK[t]
        print(f"  ≥{t:.0%}: n={len(leader_won):4d}  avg entry {mp:.3f}  leader wins {wr:5.1%}  "
              f"Kalshi comeback {kc:5.1%}  | model comeback {mc:5.1%}  gap {kc - mc:+.1%}")
    return out


def _plots(df: pl.DataFrame, cb: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    ip = df.filter(pl.col("in_play"))
    # 1) calibration curve
    xs, ys = [], []
    for i in range(20):
        lo, hi = i / 20, (i + 1) / 20
        sel = ip.filter((pl.col("yes_mid") >= lo) & (pl.col("yes_mid") < hi))
        if sel.height >= 30:
            xs.append(sel["yes_mid"].mean())
            ys.append(sel["settled_yes"].mean())
    plt.figure(figsize=(6, 6))
    plt.plot([0, 1], [0, 1], "k--", lw=1, label="perfect (efficient market)")
    plt.plot(xs, ys, "o-", color="C0", label="Kalshi in-play")
    plt.xlabel("Kalshi YES price (implied prob)")
    plt.ylabel("realized YES settlement rate")
    plt.title("Kalshi in-play calibration (KXATPMATCH, 2026)")
    plt.legend()
    plt.grid(alpha=0.3)
    plt.savefig(out_dir / "kalshi_calibration.png", dpi=120, bbox_inches="tight")
    plt.close()
    # 2) comeback comparison
    ts = [t for t in THRESHOLDS if t in cb]
    plt.figure(figsize=(7, 5))
    plt.plot([t * 100 for t in ts], [(1 - cb[t][1]) * 100 for t in ts], "o-", label="Kalshi (real prices+settlement)")
    plt.plot([t * 100 for t in ts], [MODEL_COMEBACK[t] * 100 for t in ts], "s--", label="Markov model (slam pbp)")
    plt.plot([t * 100 for t in ts], [(1 - t) * 100 for t in ts], ":", color="grey", label="if perfectly calibrated")
    plt.xlabel("leader's in-play probability (%)")
    plt.ylabel("comeback rate of the trailing player (%)")
    plt.title("Comeback rate by confidence: Kalshi vs model")
    plt.legend()
    plt.grid(alpha=0.3)
    plt.savefig(out_dir / "comeback_compare.png", dpi=120, bbox_inches="tight")
    plt.close()
    # 3) example match paths (4 with the most in-play minutes)
    top = (ip.group_by("ticker").len().sort("len", descending=True))["ticker"].to_list()[:4]
    plt.figure(figsize=(10, 6))
    for tk in top:
        g = ip.filter(pl.col("ticker") == tk).sort("ts")
        mins = [(t - g["ts"].min()) / 60 for t in g["ts"].to_list()]
        won = g["settled_yes"][0]
        plt.plot(mins, g["yes_mid"].to_list(),
                 label=f"{g['yes_player'][0][:18]} ({'won' if won else 'lost'})")
    plt.axhline(0.5, color="grey", ls=":", lw=1)
    plt.xlabel("minutes into in-play window")
    plt.ylabel("Kalshi YES price")
    plt.title("Example Kalshi in-play price paths")
    plt.legend(fontsize=8)
    plt.grid(alpha=0.3)
    plt.savefig(out_dir / "example_paths.png", dpi=120, bbox_inches="tight")
    plt.close()
    print(f"\nwrote plots to {out_dir}: kalshi_calibration.png, comeback_compare.png, example_paths.png")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", type=Path, default=ROOT / "data" / "tennis-live" / "kalshi_candles.parquet")
    ap.add_argument("--out-dir", type=Path, default=ROOT / "data" / "tennis-live" / "kalshi_gap_plots")
    ap.add_argument("--max-markets", type=int, default=None)
    ap.add_argument("--refresh", action="store_true", help="re-fetch from Kalshi even if cache exists")
    args = ap.parse_args()

    if args.cache.exists() and not args.refresh:
        df = pl.read_parquet(args.cache)
        print(f"loaded cache: {df.height:,} candle-minutes, {df['ticker'].n_unique()} markets")
    else:
        df = asyncio.run(_build_cache(args.cache, args.max_markets))
    if not df.height:
        print("no candle data")
        return 1
    _calibration(df)
    cb = _comeback(df)
    _plots(df, cb, args.out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
