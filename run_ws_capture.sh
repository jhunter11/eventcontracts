#!/bin/bash
# Persistent Kalshi WebSocket tick capture (read-only market data; never trades).
# Loads engine/.env (Kalshi prod auth) and streams orderbook_delta/trade/ticker
# into data/ws_ticks/ for hyper-accurate backtesting. DEPTH set is curated to the
# strategy book + likely-future families (weather, crypto settlement, sports,
# macro) so the backtest window exists when those strategies are ready.
cd /Users/jackhunter/.openclaw/workspace/engine || exit 1
set -a
. ./.env
set +a
# FULL legit universe at full depth (bounded couple-day capture). Every real
# tradeable series — crypto, all sports/esports, weather, golf, macro/commodity.
# EXCLUDES only the 3 MVE parlay-combination series (KXMVE*) = 59.8k illiquid
# permutation markets that would blow the WS subscription cap and carry no depth.
# Stored on the 2TB drive. --discover-max-pages bounds startup REST burst.
# Subscribe by explicit LIQUID+FAST tickers (built fresh each start). Skips the
# series-discovery burst, and avoids the ~30k illiquid prop/strike markets that
# choke the socket into snapshot-churn. Refreshed on restart (a cron restarts
# this every few hours to pick up newly-activated markets).
TICKERS="$(.venv/bin/python build_liquid_tickers.py 2>> wscapture.err.log)"
exec .venv/bin/python python/scripts/kalshi_ws_capture.py \
  --out /Volumes/JHREMOVABLE/ws_ticks \
  --channels orderbook_delta,trade,ticker,market_lifecycle_v2 \
  --rediscover-interval-sec 999999 \
  --tickers "$TICKERS"
