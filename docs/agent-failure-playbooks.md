# Agent Failure Playbooks

## PowerShell Argument Parsing

Symptom: `ruff` or `mypy` receives one comma-joined path.

Fix: use `scripts\verify-eventcontracts.ps1`, which expands comma-separated
lists, or pass quoted paths as separate array items.

## Python Environment Drift

Symptom: imports fail even though code exists.

Fix:

```powershell
.venv\Scripts\python.exe -m pip install -e .\python --config-settings editable_mode=compat --no-deps
```

Then rerun the focused gate.

## Mypy Import Noise

Symptom: mypy fails outside the changed surface.

Fix: run focused targets first. Broaden only when shared contracts moved.

```powershell
.venv\Scripts\python.exe -m mypy python\scripts\some_script.py python\tests\test_some_script.py
```

## Rust Manifest Confusion

Symptom: cargo cannot find the workspace.

Fix from repo root:

```powershell
cargo test --manifest-path rust\Cargo.toml --workspace
```

From inside `rust\`, use:

```powershell
cargo test --workspace
```

## Parity Fixture Drift

Symptom: Python tests pass but Rust/runtime behavior diverges.

Fix: update the fixture under `contracts\parity\...`, then run both the Python
contract tests and relevant Rust runner tests.

## Stale Artifact

Symptom: a report claims edge but source files or manifests changed.

Fix: regenerate the artifact from current inputs and record the command. Treat
old reports as stale until hashes/manifests match.

## Network Probe Flakiness

Symptom: public REST/WS probe fails once.

Fix: retry with timestamps and record the failure. Do not convert a transient
network result into a trading conclusion without a ledger.

## Dangerous Action Scanner Trips

Symptom: `check-dangerous-actions.ps1` finds an order/live-submit line.

Fix: inspect the exact line. If it is documentation, rerun with a narrower
target or `-IncludeDocs` only when needed. If it is code/config, stop unless the
active repo policy and user explicitly allow that path.
