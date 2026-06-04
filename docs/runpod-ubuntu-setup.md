# RunPod Ubuntu Setup

This is the clean path for moving the repo to a RunPod Ubuntu pod and setting it
up with **Python 3.11** in the repo-root `.venv`.

The important constraints:

- use `runpodctl send` locally and `runpodctl receive` on the pod;
- do not use the old nested `python/.venv`;
- do not use Python 3.12 for this environment unless you are deliberately
  testing a separate interpreter;
- do not transfer `.env` or private keys unless you explicitly opt in.

## 1. Send From Your Local Machine

From the repo root on Windows PowerShell:

```powershell
.\scripts\runpod-send.ps1 -Code eventcontracts-setup
```

From Git Bash, WSL, macOS, or Linux:

```bash
bash scripts/runpod-send.sh --code eventcontracts-setup
```

The sender creates `.tmp-runpod/eventcontracts-runpod-*.tar.gz`, prints the
commands to run on the pod, then starts `runpodctl send`.

By default, the package includes tracked/untracked repo files plus the small
weather calibration files under:

- `configs/weather`
- `data/weather-calib`
- `data/weather-paper`

Secrets are excluded. To include `.env` anyway:

```powershell
.\scripts\runpod-send.ps1 -Code eventcontracts-setup -IncludeEnv
```

## 2. Receive On The RunPod

Open the RunPod web terminal or SSH session. While the local sender is waiting,
run:

```bash
cd /workspace
runpodctl receive eventcontracts-setup
tar -xzf eventcontracts-runpod-*.tar.gz -C /workspace
cd /workspace/eventcontracts
bash scripts/runpod-setup.sh
```

The setup script installs Ubuntu dependencies, ensures `python3.11`, creates
`.venv`, installs Python requirements, installs the editable package, installs
Rust if needed, and runs focused weather smoke tests plus Rust tests by default.

## 3. Setup Options

Runtime-only Python dependencies:

```bash
INSTALL_DEV=0 bash scripts/runpod-setup.sh
```

Skip tests:

```bash
RUN_TESTS=0 RUN_RUST_TESTS=0 bash scripts/runpod-setup.sh
```

Skip Rust entirely:

```bash
SKIP_RUST=1 bash scripts/runpod-setup.sh
```

Build Rust release binaries:

```bash
BUILD_RUST_RELEASE=1 bash scripts/runpod-setup.sh
```

Run setup detached so it keeps going after your browser/SSH session disconnects:

```bash
cd /workspace/eventcontracts
bash scripts/runpod-background.sh
tail -f logs/runpod/run-*.log
```

Run a specific command detached with compute-friendly priority:

```bash
cd /workspace/eventcontracts
bash scripts/runpod-background.sh "source .venv/bin/activate && python python/scripts/weather_kxhigh_paper.py --record data/weather-paper/kxhigh_ledger.jsonl"
```

`runpod-background.sh` uses `nohup`-style detaching via a background process,
captures logs under `logs/runpod/`, and attempts `nice -n -5` plus `ionice -c2
-n0`. If the container does not allow negative niceness, the job still runs.

Use an already-installed Python binary:

```bash
PYTHON_BIN=/usr/bin/python3.11 bash scripts/runpod-setup.sh
```

If `.venv` already exists but is not Python 3.11, the setup script moves it to
`.venv.old.<timestamp>` and creates a fresh Python 3.11 venv.

## 4. Verify

On the pod:

```bash
cd /workspace/eventcontracts
source .venv/bin/activate
python --version
python -m pytest python/tests
CARGO_INCREMENTAL=0 CARGO_TARGET_DIR=.tmp-target cargo test --manifest-path rust/Cargo.toml --quiet
```

For the weather paper harness:

```bash
python python/scripts/weather_kxhigh_paper.py
python python/scripts/weather_kxhigh_paper.py --settle data/weather-paper/kxhigh_ledger.jsonl
```

## 5. Credentials

The package does not include `.env` by default. On the pod:

```bash
cp .env.example .env
nano .env
```

Transfer private keys separately, or paste them through the RunPod secret
manager / terminal. Avoid packaging key files into the transfer archive.

## Notes

RunPod documents this transfer model as `runpodctl send <fileOrFolder>` on the
source machine and `runpodctl receive <code>` on the destination machine. Pods
normally have `runpodctl` preinstalled; local machines may need the RunPod CLI
installed first.
