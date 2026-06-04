# EC2 handoff — start sequence

Bring up a full eventcontracts box on Ubuntu EC2 with **one command run on the
box**. The provisioner installs everything — Python (core + dev + tabular ML),
the HuggingFace/torch stack, the editable `ec` CLI, and the Rust executors — and
scaffolds secrets. You finish the last ~10% (credentials) by hand.

## Prereqs (your side)
- An Ubuntu 22.04/24.04 EC2 instance. Because we now build the Rust workspace in
  release and install torch, size up from the old `t3.small`:
  - **CPU-only:** `t3.large` (8 GB RAM) is comfortable; `t3.medium` is the floor
    for the cargo release link step.
  - **GPU (only if doing real model training/inference):** any g-class box; the
    setup auto-detects the GPU and installs the CUDA torch wheel.
  - **Disk:** give it ~20–25 GB — torch + transformers + the cargo `target/` dir
    are several GB combined.
- The instance's SSH `.pem` and its public DNS.

## 1. Get the repo onto the box

The repo carries secrets that are **not in git** (`.env`, the Kalshi key, etc.),
so you either ship it with secrets or clone it and add them after.

**Option A — ship repo + secrets from your Windows box (Git Bash):**
```bash
bash scripts/ec2-codex/sync-to-ec2.sh ubuntu@<EC2_PUBLIC_DNS> ~/.ssh/<your-key>.pem
```
Ships the repo incl. your real `.env` + Kalshi key, excludes SSH/git secrets +
heavy data, and `chmod 600`s the creds on arrival.

**Option B — clone on the box, add secrets yourself:**
```bash
git clone https://github.com/jhunter11/eventcontracts.git ~/eventcontracts
# then scp your .env + key file into ~/eventcontracts before live/paper runs
```

## 2. Provision — one command, on the box
```bash
ssh -i ~/.ssh/<your-key>.pem ubuntu@<EC2_PUBLIC_DNS>
cd ~/eventcontracts
bash scripts/ec2-codex/bootstrap-ec2.sh
```
This delegates to the canonical [`scripts/setup-ubuntu.sh`](../setup-ubuntu.sh):
system deps → Python 3.11 venv → core+dev+ML+HF/torch → editable package → Rust
toolchain + release build → `.env` scaffold + permission hygiene. Idempotent —
safe to re-run. It then prints the BTC settlement-arb latency quick-read.

Common toggles (passed straight through to `setup-ubuntu.sh`):
```bash
SKIP_RUST=1   bash scripts/ec2-codex/bootstrap-ec2.sh   # no Rust toolchain/build
INSTALL_HF=0  bash scripts/ec2-codex/bootstrap-ec2.sh   # skip the torch stack
RUN_TESTS=1   bash scripts/ec2-codex/bootstrap-ec2.sh   # run full py + rust suites
```

## 3. The remaining ~10% (secrets)
Edit `~/eventcontracts/.env` and fill in what your workload needs —
`KALSHI_API_KEY_ID` / `KALSHI_PRIVATE_KEY_PATH`, `KALSHI_ENV=demo|prod`,
`NOAA_TOKEN`, `FRED_API_KEY`, per-strategy feed keys — and drop in the Kalshi
private key file (kept `chmod 600`). The setup script verifies the key path
resolves and warns if it doesn't.

## The task plan (your Codex reads `AGENTS.md`)
1. **Task 1** — latency bench from this box vs the ~33/34 ms home baseline.
2. **Task 2** — Coinbase/Kraken WS c-lead recorder (decides if any edge exists).
3. **Task 3** — gap recorder vs live `KXBTC15M`, honestly labeled.

Findings land in `live-test/ec2-findings.md`.

## Boundary
Kalshi keys are on the box for authenticated **reads** only. `AGENTS.md` forbids
order submission outright — there is no proven edge yet, so there is nothing to
trade.
