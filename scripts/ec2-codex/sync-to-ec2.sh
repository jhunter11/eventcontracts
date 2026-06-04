#!/usr/bin/env bash
# Sync the repo to the EC2 box via tar+scp, EXCLUDING every secret and heavy
# artifact. Run from the repo root in Git Bash:
#
#   bash scripts/ec2-codex/sync-to-ec2.sh ec2-user@<EC2_PUBLIC_DNS> ~/.ssh/your-key.pem
#
# Security: the exclude list below is the boundary that keeps live-money
# credentials OFF the cloud agent. Do not weaken it without a reason.

set -Eeuo pipefail

HOST="${1:?usage: sync-to-ec2.sh <user@host> <ssh_key.pem> [remote_dir]}"
KEY="${2:?provide path to your .pem ssh key}"
REMOTE_DIR="${3:-eventcontracts}"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"
cd "$repo_root"

# --- the exclusion boundary ---
# OPERATOR CHOICE: the Kalshi trading material (.env, ecmodel.txt = the private
# key, demokey.txt) IS shipped to the box on purpose. Still excluded: your SSH
# login keys, generic certs, and git history/tokens — a different secret class
# you did not ask to ship.
EXCLUDES=(
  --exclude='*.pem' --exclude='id_rsa*' --exclude='id_ed25519*'   # YOUR ssh login keys
  --exclude='.ssh'                       # ssh dir if any
  --exclude='.git'                       # git history / remote tokens
  --exclude='.venv'                      # rebuilt on the box
  --exclude='rust/target'                # huge build dir
  --exclude='**/__pycache__'
  --exclude='.mypy_cache' --exclude='.pytest_cache' --exclude='.ruff_cache'
  --exclude='.tmp-target' --exclude='.tmp-runpod'   # local scratch build dirs
  --exclude='*.onnx' --exclude='*.parquet'   # heavy model/data artifacts
  --exclude='data'                       # 170MB+ tennis/run data — irrelevant to BTC tasks
  --exclude='artifacts'                  # model artifacts — not needed
)

echo "==> Pre-flight: Kalshi material WILL ship; SSH/git material must NOT"
payload="$(tar "${EXCLUDES[@]}" -cf - . 2>/dev/null | tar -tf - 2>/dev/null)"
# Hard-fail only if a class we intend to exclude slips through.
if printf '%s\n' "$payload" | grep -iE '\.pem$|id_rsa|id_ed25519|(^|/)\.ssh/|(^|/)\.git/'; then
  echo "ERROR: an SSH/git secret survived the exclude list — aborting." >&2
  exit 1
fi
# Confirm the Kalshi material IS present (so it's actually 'just there' after scp).
echo "    Kalshi material in payload:"
printf '%s\n' "$payload" | grep -iE '(^|/)\.env$|(^|/)ecmodel\.txt$|(^|/)demokey\.txt$' \
  | sed 's/^/      ship: /' \
  || echo "      WARN: expected .env/ecmodel.txt/demokey.txt not found in payload!"

echo "==> Ensuring remote dir ~/$REMOTE_DIR"
ssh -i "$KEY" -o StrictHostKeyChecking=accept-new "$HOST" "mkdir -p ~/$REMOTE_DIR"

echo "==> Streaming tar -> scp -> untar on $HOST"
# Stream directly (no temp file): tar locally, pipe over ssh, untar remotely.
tar "${EXCLUDES[@]}" -czf - . \
  | ssh -i "$KEY" "$HOST" "tar -xzf - -C ~/$REMOTE_DIR"

echo "==> Locking down secret file permissions on the box (private key was shipped)"
ssh -i "$KEY" "$HOST" "cd ~/$REMOTE_DIR && chmod 600 .env ecmodel.txt demokey.txt 2>/dev/null; \
  chmod 700 . 2>/dev/null; ls -l .env ecmodel.txt demokey.txt 2>/dev/null"

echo "==> Verifying the key path resolves from the repo root on the box"
ssh -i "$KEY" "$HOST" "cd ~/$REMOTE_DIR && kp=\$(grep -E '^KALSHI_PRIVATE_KEY_PATH=' .env | cut -d= -f2- | tr -d '\r\"'); \
  if [ -f \"\$kp\" ]; then echo \"   OK: KALSHI_PRIVATE_KEY_PATH=\$kp resolves (\$(wc -c < \"\$kp\") bytes)\"; \
  else echo \"   WARN: KALSHI_PRIVATE_KEY_PATH=\$kp does NOT resolve from \$(pwd) — app must run from repo root\"; fi"

cat <<NEXT

==> Sync complete. Your Kalshi keys (.env + ecmodel.txt private key) are on the box,
    chmod 600, resolving relative to ~/$REMOTE_DIR. On the box:
    ssh -i $KEY $HOST
    cd ~/$REMOTE_DIR
    bash scripts/ec2-codex/bootstrap-ec2.sh
    # then point your running Codex at this repo (it reads AGENTS.md)

    NOTE: KALSHI_PRIVATE_KEY_PATH=ecmodel.txt is repo-root-relative, so always
    run from ~/$REMOTE_DIR (or set it to an absolute path in .env).
NEXT
