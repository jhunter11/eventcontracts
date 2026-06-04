#!/usr/bin/env bash
# Provision an Ubuntu RunPod for the eventcontracts repo.
#
# This script intentionally uses Python 3.11 and the repo-root `.venv`.
# Older setup attempts used other interpreters / nested venvs; those are moved
# aside if they do not match Python 3.11.

set -Eeuo pipefail

log() {
  printf '\n==> %s\n' "$*"
}

warn() {
  printf '\nWARN: %s\n' "$*" >&2
}

die() {
  printf '\nERROR: %s\n' "$*" >&2
  exit 1
}

as_bool() {
  case "${1:-}" in
    1|true|TRUE|yes|YES|y|Y|on|ON) return 0 ;;
    *) return 1 ;;
  esac
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
cd "$repo_root"

if [ "${EUID:-$(id -u)}" -eq 0 ]; then
  SUDO=""
else
  SUDO="sudo"
fi

PYTHON_BIN="${PYTHON_BIN:-python3.11}"
VENV_DIR="${VENV_DIR:-.venv}"
INSTALL_DEV="${INSTALL_DEV:-1}"
RUN_TESTS="${RUN_TESTS:-1}"
RUN_RUST_TESTS="${RUN_RUST_TESTS:-1}"
SKIP_RUST="${SKIP_RUST:-0}"
BUILD_RUST_RELEASE="${BUILD_RUST_RELEASE:-0}"
SETUP_APT="${SETUP_APT:-1}"

log "eventcontracts RunPod setup"
printf 'repo_root=%s\n' "$repo_root"
printf 'python=%s venv=%s install_dev=%s run_tests=%s run_rust_tests=%s\n' \
  "$PYTHON_BIN" "$VENV_DIR" "$INSTALL_DEV" "$RUN_TESTS" "$RUN_RUST_TESTS"

install_apt_deps() {
  if ! as_bool "$SETUP_APT"; then
    warn "SETUP_APT=0, skipping apt dependency installation"
    return
  fi

  log "Installing Ubuntu dependencies"
  export DEBIAN_FRONTEND=noninteractive
  $SUDO apt-get update -y
  $SUDO apt-get install -y \
    ca-certificates \
    curl \
    wget \
    git \
    build-essential \
    pkg-config \
    libssl-dev \
    libffi-dev \
    software-properties-common \
    htop \
    jq \
    rsync \
    unzip

  if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    log "Python 3.11 not found; trying Ubuntu packages"
    if ! apt-cache policy python3.11 2>/dev/null | grep -q 'Candidate: [^(]'; then
      log "Adding deadsnakes PPA for Python 3.11 packages"
      $SUDO add-apt-repository -y ppa:deadsnakes/ppa
      $SUDO apt-get update -y
    fi
    $SUDO apt-get install -y python3.11 python3.11-venv python3.11-dev
  else
    $SUDO apt-get install -y python3.11-venv python3.11-dev || true
  fi
}

ensure_python311() {
  log "Checking Python 3.11"
  command -v "$PYTHON_BIN" >/dev/null 2>&1 || die "$PYTHON_BIN is not installed"
  "$PYTHON_BIN" - <<'PY'
import sys
if sys.version_info[:2] != (3, 11):
    raise SystemExit(f"expected Python 3.11, got {sys.version}")
print(sys.version)
PY
}

ensure_rust() {
  if as_bool "$SKIP_RUST"; then
    warn "SKIP_RUST=1, skipping Rust setup"
    return
  fi

  log "Checking Rust toolchain"
  if ! command -v cargo >/dev/null 2>&1; then
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
  fi
  # shellcheck disable=SC1091
  [ -f "$HOME/.cargo/env" ] && source "$HOME/.cargo/env"
  if command -v rustup >/dev/null 2>&1; then
    rustup default stable
  else
    warn "cargo exists but rustup does not; using the existing Rust toolchain"
  fi
  cargo --version
}

venv_python_version() {
  local py="$1"
  [ -x "$py" ] || return 1
  "$py" - <<'PY'
import sys
print(f"{sys.version_info.major}.{sys.version_info.minor}")
PY
}

create_venv() {
  log "Creating Python 3.11 virtualenv at $VENV_DIR"
  local venv_py="$VENV_DIR/bin/python"
  if [ -d "$VENV_DIR" ]; then
    local current_version=""
    current_version="$(venv_python_version "$venv_py" 2>/dev/null || true)"
    if [ "$current_version" != "3.11" ]; then
      local backup="${VENV_DIR}.old.$(date -u +%Y%m%dT%H%M%SZ)"
      warn "Existing $VENV_DIR is Python ${current_version:-unknown}; moving to $backup"
      mv "$VENV_DIR" "$backup"
    fi
  fi

  if [ ! -d "$VENV_DIR" ]; then
    "$PYTHON_BIN" -m venv "$VENV_DIR"
  fi

  # shellcheck disable=SC1091
  source "$VENV_DIR/bin/activate"
  python -m pip install --upgrade pip setuptools wheel

  if as_bool "$INSTALL_DEV"; then
    python -m pip install -r python/requirements-dev.txt
  else
    python -m pip install -r python/requirements.txt
  fi
  python -m pip install -e ./python
  python --version
}

prepare_env_file() {
  log "Preparing .env"
  if [ ! -f .env ]; then
    if [ -f .env.example ]; then
      cp .env.example .env
      warn "Created .env from .env.example; edit it before live/paper runs that need credentials"
    else
      {
        echo "EVENTCONTRACTS_ENV=runpod"
        echo "KALSHI_API_KEY_ID="
        echo "KALSHI_PRIVATE_KEY_PATH="
        echo "NOAA_TOKEN="
      } > .env
      warn "Created placeholder .env; edit it before live/paper runs that need credentials"
    fi
  else
    printf '.env already exists; leaving it untouched\n'
  fi
}

run_smoke_tests() {
  if ! as_bool "$RUN_TESTS"; then
    warn "RUN_TESTS=0, skipping Python tests"
    return
  fi

  log "Running Python smoke tests"
  # shellcheck disable=SC1091
  source "$VENV_DIR/bin/activate"
  if ! python - <<'PY'
import importlib.util
raise SystemExit(0 if importlib.util.find_spec("pytest") else 1)
PY
  then
    warn "pytest is not installed; skipping Python smoke tests"
    return
  fi
  python -m pytest python/tests/test_weather_calibration.py \
    python/tests/test_weather_kxhigh.py \
    python/tests/test_weather_models.py
}

run_rust_checks() {
  if as_bool "$SKIP_RUST"; then
    return
  fi
  # shellcheck disable=SC1091
  [ -f "$HOME/.cargo/env" ] && source "$HOME/.cargo/env"

  if as_bool "$RUN_RUST_TESTS"; then
    log "Running Rust tests"
    CARGO_INCREMENTAL=0 CARGO_TARGET_DIR=.tmp-target cargo test --manifest-path rust/Cargo.toml --quiet
  fi

  if as_bool "$BUILD_RUST_RELEASE"; then
    log "Building Rust release binaries"
    CARGO_INCREMENTAL=0 CARGO_TARGET_DIR=.tmp-target cargo build --manifest-path rust/Cargo.toml --release
  fi
}

print_next_steps() {
  cat <<EOF

Setup complete.

Use this environment:
  cd "$repo_root"
  source $VENV_DIR/bin/activate
  python --version

Useful verification commands:
  python -m pytest python/tests
  CARGO_INCREMENTAL=0 CARGO_TARGET_DIR=.tmp-target cargo test --manifest-path rust/Cargo.toml --quiet
  python python/scripts/weather_kxhigh_paper.py --settle data/weather-paper/kxhigh_ledger.jsonl

Before any authenticated run, edit:
  $repo_root/.env

EOF
}

install_apt_deps
ensure_python311
ensure_rust
create_venv
prepare_env_file
run_smoke_tests
run_rust_checks
print_next_steps
