#!/usr/bin/env bash
# Receive an eventcontracts archive on a RunPod, extract it, and optionally run setup.

set -Eeuo pipefail

CODE="${1:-}"
DEST="${2:-/workspace}"
RUN_SETUP="${RUN_SETUP:-1}"

if [ -z "$CODE" ]; then
  cat >&2 <<'EOF'
Usage: scripts/runpod-receive.sh CODE [DEST]

Example:
  bash scripts/runpod-receive.sh eventcontracts-20260531 /workspace
EOF
  exit 2
fi

command -v runpodctl >/dev/null 2>&1 || {
  echo "runpodctl not found on this pod" >&2
  exit 127
}

mkdir -p "$DEST"
cd "$DEST"

echo "Receiving archive with code: $CODE"
runpodctl receive "$CODE"

archive="$(ls -t eventcontracts-runpod-*.tar.gz 2>/dev/null | head -n 1 || true)"
if [ -z "$archive" ]; then
  echo "No eventcontracts-runpod-*.tar.gz archive found in $DEST" >&2
  exit 1
fi

echo "Extracting $archive into $DEST"
tar -xzf "$archive" -C "$DEST"

cd "$DEST/eventcontracts"
if [ "$RUN_SETUP" = "1" ]; then
  bash scripts/runpod-setup.sh
else
  echo "RUN_SETUP=0, skipping setup. Repo is at: $DEST/eventcontracts"
fi
