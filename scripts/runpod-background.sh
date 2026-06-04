#!/usr/bin/env bash
# Run setup or any repo command detached on a RunPod with compute-friendly priority.
#
# Usage:
#   bash scripts/runpod-background.sh
#   bash scripts/runpod-background.sh "python python/scripts/weather_kxhigh_paper.py --record data/weather-paper/kxhigh_ledger.jsonl"
#
# The default command runs the Ubuntu setup script. Output goes to logs/runpod/.

set -Eeuo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
cd "$repo_root"

mkdir -p logs/runpod
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
log_file="logs/runpod/run-$stamp.log"
pid_file="logs/runpod/run-$stamp.pid"

cmd="${*:-bash scripts/runpod-setup.sh}"

runner_prefix=""
if command -v ionice >/dev/null 2>&1; then
  runner_prefix="ionice -c2 -n0"
fi

if [ "${RUNPOD_NICE:-1}" = "1" ] && command -v nice >/dev/null 2>&1; then
  # Negative nice needs root/CAP_SYS_NICE. RunPod containers are often root; if
  # not, we retry without failing the job.
  runner_prefix="$runner_prefix nice -n ${RUNPOD_NICE_LEVEL:--5}"
fi

cat > "logs/runpod/run-$stamp.command.sh" <<EOF
#!/usr/bin/env bash
set -Eeuo pipefail
cd "$repo_root"
export PYTHONUNBUFFERED=1
export CARGO_INCREMENTAL=\${CARGO_INCREMENTAL:-0}
export CARGO_TARGET_DIR=\${CARGO_TARGET_DIR:-.tmp-target}
$cmd
EOF
chmod +x "logs/runpod/run-$stamp.command.sh"

start_command() {
  if [ -n "$runner_prefix" ]; then
    # shellcheck disable=SC2086
    $runner_prefix bash "logs/runpod/run-$stamp.command.sh"
  else
    bash "logs/runpod/run-$stamp.command.sh"
  fi
}

(
  set +e
  start_command
  code=$?
  if [ "$code" -eq 126 ] || [ "$code" -eq 127 ]; then
    echo "priority wrapper failed with exit $code; retrying without nice/ionice"
    bash "logs/runpod/run-$stamp.command.sh"
    code=$?
  fi
  echo "exit_code=$code"
  exit "$code"
) > "$log_file" 2>&1 &

pid="$!"
echo "$pid" > "$pid_file"

if command -v renice >/dev/null 2>&1; then
  renice "${RUNPOD_NICE_LEVEL:--5}" -p "$pid" >/dev/null 2>&1 || true
fi

cat <<EOF
Started background RunPod job.

pid:  $pid
log:  $repo_root/$log_file
cmd:  $repo_root/logs/runpod/run-$stamp.command.sh

Watch it:
  tail -f "$repo_root/$log_file"

Check process:
  ps -p $pid -o pid,ni,stat,etime,cmd

EOF
