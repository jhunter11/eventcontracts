#!/usr/bin/env bash
# Runs ON the EC2 box, invoked by sync-to-ec2.ps1 right after the tarball lands.
# Extracts the payload, locks credential perms, and checks the key path resolves.
# Kept as its own file so the PowerShell side never has to embed/escape bash.
set -Eeuo pipefail

remote_dir="${1:-eventcontracts}"
cd "$HOME/$remote_dir"

if [ -f _payload.tar.gz ]; then
  tar -xzf _payload.tar.gz
  rm -f _payload.tar.gz
fi

chmod 600 .env ecmodel.txt demokey.txt 2>/dev/null || true
chmod 700 . 2>/dev/null || true
echo "   files:"
ls -l .env ecmodel.txt demokey.txt 2>/dev/null || true

kp="$(grep -E '^KALSHI_PRIVATE_KEY_PATH=' .env 2>/dev/null | cut -d= -f2- | tr -d '\r"')"
if [ -n "$kp" ] && [ -f "$kp" ]; then
  echo "   OK: KALSHI_PRIVATE_KEY_PATH=$kp resolves ($(wc -c < "$kp") bytes)"
elif [ -n "$kp" ]; then
  echo "   WARN: KALSHI_PRIVATE_KEY_PATH=$kp does NOT resolve from $(pwd) - run from repo root or set an absolute path"
fi
echo "   remote setup done."
