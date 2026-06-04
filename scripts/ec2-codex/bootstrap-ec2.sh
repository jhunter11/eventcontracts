#!/usr/bin/env bash
# Provision a fresh Ubuntu EC2 box for eventcontracts, run ON the box after the
# repo is present at ~/eventcontracts (clone or scp it first).
#
# This is now a thin wrapper over the canonical provisioner
# scripts/setup-ubuntu.sh — it installs EVERYTHING (Python core+dev+ML, the
# HuggingFace/torch stack, and the Rust executors) and scaffolds secrets. The
# only EC2-specific extra here is the BTC settlement-arb latency quick-read.
#
# Codex is NOT installed/launched here — the operator runs their own Codex,
# which reads AGENTS.md for the task plan. AGENTS.md forbids order submission.
#
# Usage (on the box):
#   cd ~/eventcontracts && bash scripts/ec2-codex/bootstrap-ec2.sh
#
# Pass any setup-ubuntu.sh toggle straight through, e.g.:
#   SKIP_RUST=1 bash scripts/ec2-codex/bootstrap-ec2.sh      # skip Rust
#   INSTALL_HF=0 bash scripts/ec2-codex/bootstrap-ec2.sh     # skip torch stack
#   RUN_TESTS=1 bash scripts/ec2-codex/bootstrap-ec2.sh      # full test suites
set -Eeuo pipefail

log()  { printf '\n==> %s\n' "$*"; }

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"
cd "$repo_root"

# Full provisioning (system deps, Python, ML, Rust, secrets scaffold + hygiene).
bash "$repo_root/scripts/setup-ubuntu.sh"

cat <<'NEXT'

================ EC2 EXTRA: BTC settlement-arb quick-read ================
Point your running Codex at this repo; it reads AGENTS.md for the task plan.
Manual latency read (no key needed):

    source .venv/bin/activate
    python python/scripts/btc_settlement_bench.py --net-samples 9

Compare the Kalshi/Coinbase ms to the ~33/34 ms home baseline (Task 1).
Findings land in live-test/ec2-findings.md.
=========================================================================
NEXT
