#!/usr/bin/env bash
# Package this repo and send it to a RunPod using runpodctl.

set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_DIR="$ROOT/.tmp-runpod"
CODE="eventcontracts-$(date -u +%Y%m%d%H%M%S)"
ARCHIVE=""
SEND=1
INCLUDE_ENV=0
INCLUDE_WEATHER_DATA=1

usage() {
  cat <<'EOF'
Usage: scripts/runpod-send.sh [options]

Options:
  --code CODE              runpodctl transfer code to use
  --archive PATH           archive path to create
  --no-send                only create the tarball
  --include-env            include .env (off by default; be careful)
  --no-weather-data        do not include small weather calibration/ledger files
  -h, --help               show this help

On the RunPod, run the receive commands printed by this script while the sender
is waiting.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --code)
      CODE="${2:?missing --code value}"
      shift 2
      ;;
    --archive)
      ARCHIVE="${2:?missing --archive value}"
      shift 2
      ;;
    --no-send)
      SEND=0
      shift
      ;;
    --include-env)
      INCLUDE_ENV=1
      shift
      ;;
    --no-weather-data)
      INCLUDE_WEATHER_DATA=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [ -z "$ARCHIVE" ]; then
  ARCHIVE="$TMP_DIR/eventcontracts-runpod-$(date -u +%Y%m%dT%H%M%SZ).tar.gz"
fi

cd "$ROOT"
mkdir -p "$TMP_DIR"
STAGE="$TMP_DIR/stage"
MANIFEST="$TMP_DIR/manifest.txt"
rm -rf "$STAGE"
mkdir -p "$STAGE/eventcontracts"

git ls-files --cached --others --exclude-standard > "$MANIFEST"

if [ "$INCLUDE_WEATHER_DATA" -eq 1 ]; then
  find configs/weather data/weather-calib data/weather-paper \
    -type f 2>/dev/null >> "$MANIFEST" || true
fi

if [ "$INCLUDE_ENV" -eq 1 ] && [ -f .env ]; then
  echo ".env" >> "$MANIFEST"
fi

sort -u "$MANIFEST" -o "$MANIFEST"

while IFS= read -r rel; do
  [ -n "$rel" ] || continue
  [ -f "$rel" ] || continue
  mkdir -p "$STAGE/eventcontracts/$(dirname "$rel")"
  cp -p "$rel" "$STAGE/eventcontracts/$rel"
done < "$MANIFEST"

tar -czf "$ARCHIVE" -C "$STAGE" eventcontracts

cat <<EOF
Created archive:
  $ARCHIVE

On the RunPod, open a terminal and run:

  cd /workspace
  runpodctl receive $CODE
  tar -xzf $(basename "$ARCHIVE") -C /workspace
  cd /workspace/eventcontracts
  bash scripts/runpod-background.sh
  tail -f logs/runpod/run-*.log

EOF

if [ "$SEND" -eq 1 ]; then
  command -v runpodctl >/dev/null 2>&1 || {
    echo "runpodctl not found. Install it first: https://docs.runpod.io/runpodctl/overview" >&2
    exit 127
  }
  runpodctl send "$ARCHIVE" --code "$CODE"
fi
