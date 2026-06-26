#!/bin/bash
# Persistent Kalshi WebSocket capture for WC2026 soccer markets only.
# Read-only market data; never submits/cancels/replaces orders.
#
# Kept separate from the global WS capture so WC data is guaranteed even when
# the liquid-ticker selector omits KXWCGAME/KXWCTOTAL/KXWCSPREAD.
cd /Users/jackhunter/.openclaw/workspace/engine || exit 1
set -a
. ./.env
set +a

exec .venv/bin/python python/scripts/kalshi_ws_capture.py \
  --out /Volumes/JHREMOVABLE/ws_ticks_wc \
  --series-tickers KXWCGAME,KXWCTOTAL,KXWCSPREAD \
  --channels orderbook_delta,trade,ticker,market_lifecycle_v2 \
  --rediscover-interval-sec 300 \
  --discover-max-pages 2 \
  --duration-sec 10800 \
  --manifest-every 100
