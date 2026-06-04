#!/usr/bin/env bash
# ============================================================================
# eventcontracts — one-shot Ubuntu provisioner (run FROM INSIDE the box)
# ============================================================================
# This is the single source of truth for bringing a fresh Ubuntu instance
# (EC2, RunPod, bare VM) up to ~90% ready. The remaining ~10% is operator
# secrets that must never live in git (Kalshi key, NOAA/FRED tokens, etc.) —
# the script scaffolds .env and tells you exactly what to fill in.
#
# It installs EVERYTHING by default:
#   * system build deps (apt)
#   * Python 3.11 + venv
#   * core + dev + tabular-ML deps (requirements-dev.txt -> pulls requirements.txt)
#   * the heavy HuggingFace/torch ML stack (requirements-hf.txt)
#   * the editable eventcontracts package (CLI: `ec` / `eventcontracts`)
#   * the Rust executors (cargo release build of the workspace)
#
# Run in place from a repo that is ALREADY on the box (clone/scp it first):
#
#   cd ~/eventcontracts
#   bash scripts/setup-ubuntu.sh
#
# It is idempotent — safe to re-run. Steps that are already done are skipped.
#
# ----------------------------------------------------------------------------
# Toggles (env vars, all optional):
#   PYTHON_BIN=python3.11      interpreter to build the venv with
#   VENV_DIR=.venv             venv location (repo-root relative)
#   INSTALL_DEV=1              1=dev+tabular-ML deps, 0=runtime-only
#   INSTALL_HF=1               1=install torch/transformers stack, 0=skip
#   HF_TORCH_CPU=auto          auto|1|0 — use CPU-only torch wheel (saves ~1.5GB
#                              on boxes with no GPU); auto detects via nvidia-smi
#   SKIP_RUST=0                1=skip the Rust toolchain + build entirely
#   BUILD_RUST_RELEASE=1       1=cargo build --release, 0=debug build only
#   RUN_TESTS=0                1=run the Python + Rust test suites after build
#   SETUP_APT=1                1=apt-get install system deps, 0=assume present
# ----------------------------------------------------------------------------
set -Eeuo pipefail

log()  { printf '\n==> %s\n' "$*"; }
warn() { printf '\nWARN: %s\n' "$*" >&2; }
die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
as_bool() { case "${1:-}" in 1|true|TRUE|yes|YES|y|Y|on|ON) return 0;; *) return 1;; esac; }

# A clean failure message that points at the offending line.
trap 'die "step failed at line $LINENO (see output above)"' ERR

# --- Locate repo root (script lives in <repo>/scripts) ----------------------
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
cd "$repo_root"
[ -f python/pyproject.toml ] || die "can't find python/pyproject.toml under $repo_root — is this the repo root?"

if [ "${EUID:-$(id -u)}" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi

PYTHON_BIN="${PYTHON_BIN:-python3.11}"
VENV_DIR="${VENV_DIR:-.venv}"
INSTALL_DEV="${INSTALL_DEV:-1}"
INSTALL_HF="${INSTALL_HF:-1}"
HF_TORCH_CPU="${HF_TORCH_CPU:-auto}"
SKIP_RUST="${SKIP_RUST:-0}"
BUILD_RUST_RELEASE="${BUILD_RUST_RELEASE:-1}"
RUN_TESTS="${RUN_TESTS:-0}"
SETUP_APT="${SETUP_APT:-1}"

log "eventcontracts Ubuntu setup"
cat <<CFG
repo_root          = $repo_root
python_bin         = $PYTHON_BIN
venv_dir           = $VENV_DIR
install_dev        = $INSTALL_DEV     install_hf = $INSTALL_HF (torch_cpu=$HF_TORCH_CPU)
rust               = skip=$SKIP_RUST release=$BUILD_RUST_RELEASE
run_tests          = $RUN_TESTS       setup_apt  = $SETUP_APT
CFG

# ============================================================================
# 1. System dependencies
# ============================================================================
install_apt_deps() {
  if ! as_bool "$SETUP_APT"; then warn "SETUP_APT=0 — skipping apt"; return; fi
  log "[1/7] Installing system dependencies (apt)"
  export DEBIAN_FRONTEND=noninteractive
  $SUDO apt-get update -y
  $SUDO apt-get install -y \
    ca-certificates curl wget git \
    build-essential pkg-config cmake \
    libssl-dev libffi-dev \
    software-properties-common \
    htop jq rsync unzip

  if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    log "Python 3.11 not found — installing from deadsnakes PPA"
    if ! apt-cache policy python3.11 2>/dev/null | grep -q 'Candidate: [^(]'; then
      $SUDO add-apt-repository -y ppa:deadsnakes/ppa
      $SUDO apt-get update -y
    fi
    $SUDO apt-get install -y python3.11 python3.11-venv python3.11-dev
  else
    $SUDO apt-get install -y python3.11-venv python3.11-dev || true
  fi
}

# ============================================================================
# 2. Verify the interpreter
# ============================================================================
ensure_python() {
  log "[2/7] Checking interpreter ($PYTHON_BIN)"
  command -v "$PYTHON_BIN" >/dev/null 2>&1 || die "$PYTHON_BIN is not installed"
  "$PYTHON_BIN" - <<'PY'
import sys
maj, minor = sys.version_info[:2]
if (maj, minor) != (3, 11):
    raise SystemExit(f"expected Python 3.11, got {sys.version.split()[0]} "
                     "(set PYTHON_BIN to a 3.11 interpreter)")
print("interpreter:", sys.version.split()[0])
PY
}

# ============================================================================
# 3. Python venv + libraries (core + dev + tabular ML + HF/torch)
# ============================================================================
venv_py_version() {
  local py="$1"; [ -x "$py" ] || return 1
  "$py" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")'
}

create_venv() {
  log "[3/7] Python venv at $VENV_DIR"
  local venv_py="$VENV_DIR/bin/python"
  if [ -d "$VENV_DIR" ]; then
    local cur; cur="$(venv_py_version "$venv_py" 2>/dev/null || true)"
    if [ "$cur" != "3.11" ]; then
      local backup="${VENV_DIR}.old.$(date -u +%Y%m%dT%H%M%SZ)"
      warn "existing $VENV_DIR is Python ${cur:-unknown}; moving to $backup"
      mv "$VENV_DIR" "$backup"
    fi
  fi
  [ -d "$VENV_DIR" ] || "$PYTHON_BIN" -m venv "$VENV_DIR"
  # shellcheck disable=SC1091
  source "$VENV_DIR/bin/activate"
  python -m pip install --upgrade pip setuptools wheel
}

install_python_deps() {
  log "[4/7] Installing Python libraries"
  # core + dev + tabular ML (xgboost/onnx/onnxruntime/sklearn) — dev pulls runtime
  if as_bool "$INSTALL_DEV"; then
    python -m pip install -r python/requirements-dev.txt
  else
    python -m pip install -r python/requirements.txt
  fi

  if as_bool "$INSTALL_HF"; then
    log "      + HuggingFace / torch stack (heavy)"
    local use_cpu=0
    case "$HF_TORCH_CPU" in
      1) use_cpu=1 ;;
      0) use_cpu=0 ;;
      auto) command -v nvidia-smi >/dev/null 2>&1 || use_cpu=1 ;;
    esac
    if [ "$use_cpu" = "1" ]; then
      # Pre-seed the CPU-only torch wheel so requirements-hf.txt finds it
      # already satisfied and pip doesn't pull the ~2GB CUDA build.
      log "      no GPU detected — installing CPU-only torch wheel"
      python -m pip install torch==2.5.1 --index-url https://download.pytorch.org/whl/cpu
    else
      log "      GPU detected (or HF_TORCH_CPU=0) — installing default CUDA torch wheel"
    fi
    python -m pip install -r python/requirements-hf.txt
  else
    warn "INSTALL_HF=0 — skipping torch/transformers stack"
  fi

  log "      + editable eventcontracts package"
  python -m pip install -e ./python
  python -c "import eventcontracts; print('eventcontracts import OK')"
}

# ============================================================================
# 4. Rust toolchain + executors
# ============================================================================
ensure_rust() {
  if as_bool "$SKIP_RUST"; then warn "SKIP_RUST=1 — skipping Rust"; return; fi
  log "[5/7] Rust toolchain"
  if ! command -v cargo >/dev/null 2>&1; then
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --no-modify-path
  fi
  # shellcheck disable=SC1091
  [ -f "$HOME/.cargo/env" ] && source "$HOME/.cargo/env"
  command -v rustup >/dev/null 2>&1 && rustup default stable >/dev/null || \
    warn "rustup not present; using existing cargo"
  cargo --version
}

build_rust() {
  if as_bool "$SKIP_RUST"; then return; fi
  # shellcheck disable=SC1091
  [ -f "$HOME/.cargo/env" ] && source "$HOME/.cargo/env"
  if as_bool "$BUILD_RUST_RELEASE"; then
    log "[6/7] Building Rust executors (release)"
    cargo build --manifest-path rust/Cargo.toml --release
  else
    log "[6/7] Building Rust executors (debug)"
    cargo build --manifest-path rust/Cargo.toml
  fi
}

# ============================================================================
# 5. Secrets scaffold + file hygiene  (this is the manual "last 10%")
# ============================================================================
prepare_secrets() {
  log "[7/7] Secrets scaffold + permission hygiene"
  if [ ! -f .env ]; then
    if [ -f .env.example ]; then
      cp .env.example .env
      warn "created .env from .env.example — fill in credentials before any authed run"
    else
      warn "no .env.example found; create .env manually"
    fi
  else
    printf '    .env already present — left untouched\n'
  fi

  # Lock down anything that looks like a credential. Never world-readable.
  for f in .env ecmodel.txt demokey.txt *.pem; do
    [ -e "$f" ] || continue
    chmod 600 "$f" 2>/dev/null && printf '    chmod 600 %s\n' "$f" || warn "could not chmod $f"
  done

  # Sanity: does the Kalshi key path resolve from the repo root?
  if [ -f .env ]; then
    local kp
    kp="$(grep -E '^KALSHI_PRIVATE_KEY_PATH=' .env | cut -d= -f2- | tr -d '\r"' || true)"
    if [ -n "$kp" ] && [ -f "$kp" ]; then
      printf '    OK: KALSHI_PRIVATE_KEY_PATH=%s resolves (%s bytes)\n' "$kp" "$(wc -c < "$kp")"
    elif [ -n "$kp" ]; then
      warn "KALSHI_PRIVATE_KEY_PATH=$kp does NOT resolve from $(pwd) — fix the path before live/paper"
    fi
  fi
}

# ============================================================================
# 6. Optional verification
# ============================================================================
run_tests() {
  if ! as_bool "$RUN_TESTS"; then
    log "Skipping test suites (RUN_TESTS=0). Smoke import + cargo build already passed."
    return
  fi
  # shellcheck disable=SC1091
  source "$VENV_DIR/bin/activate"
  if python -c 'import importlib.util,sys; sys.exit(0 if importlib.util.find_spec("pytest") else 1)'; then
    log "Running Python test suite"
    python -m pytest python/tests -q
  else
    warn "pytest not installed (INSTALL_DEV=0?) — skipping Python tests"
  fi
  if ! as_bool "$SKIP_RUST"; then
    # shellcheck disable=SC1091
    [ -f "$HOME/.cargo/env" ] && source "$HOME/.cargo/env"
    log "Running Rust test suite"
    cargo test --manifest-path rust/Cargo.toml --quiet
  fi
}

print_next_steps() {
  local rust_bin="release"; as_bool "$BUILD_RUST_RELEASE" || rust_bin="debug"
  cat <<EOF

============================================================================
 SETUP COMPLETE — box is ~90% ready.
============================================================================
 Activate the environment:
     cd "$repo_root"
     source $VENV_DIR/bin/activate
     ec --help                 # Python CLI (alias of: eventcontracts)

 Rust executors built at:
     rust/target/$rust_bin/     (e.g. live-runner)

 -------------------------------------------------------------------------
 THE REMAINING ~10% (operator must do — secrets are not in git):
 -------------------------------------------------------------------------
   1. Edit .env and fill in what your workload needs:
        KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH   (live/paper trading)
        NOAA_TOKEN / FRED_API_KEY / BLS_API_KEY        (weather / econ data)
        TMDB_API_KEY, DATAGOLF_API_KEY, ...            (per-strategy feeds)
        KALSHI_ENV=demo|prod                           (default: demo)
   2. Drop in the Kalshi private key file referenced by
      KALSHI_PRIVATE_KEY_PATH (kept chmod 600), and any model/data bundles
      a strategy expects under data/ that aren't tracked in git.

 Verify everything (optional, slower):
     RUN_TESTS=1 bash scripts/setup-ubuntu.sh
   or directly:
     python -m pytest python/tests -q
     cargo test --manifest-path rust/Cargo.toml --quiet
============================================================================
EOF
}

# ---- Sequential run --------------------------------------------------------
install_apt_deps
ensure_python
create_venv
install_python_deps
ensure_rust
build_rust
prepare_secrets
run_tests
print_next_steps
