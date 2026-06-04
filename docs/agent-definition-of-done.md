# Agent Definition Of Done

Use the smallest section that matches the task. Repo-local `AGENTS.md` remains
authoritative.

## Research Script

- Has fixture or no-network path.
- Logs source time, received time, and stale-source reason when relevant.
- Produces append-only evidence or a deterministic report.
- Focused `pytest`, `ruff`, and `mypy` pass.
- Generated script was run once.

## Trading Strategy Or Model

- Contract target and settlement source are explicit.
- Point-in-time leakage check is documented.
- OOS score and baseline comparison are reported.
- Probability edge is translated to executable touch with fees/spread/liquidity.
- Config, tests, and parity fixtures are updated when behavior crosses runtime
  boundaries.
- Paper mode remains easy to run.

## Rust Runtime Change

- Relevant crate or workspace tests pass.
- Risk and safety behavior is covered by tests.
- Python/Rust contract changes are reflected in fixtures.
- No accidental order/cancel/live-submit path is introduced.

## Config-Only Change

- Strategy spec or config parser accepts it.
- The referenced strategy exists and loads.
- Paper path is clear.
- Dangerous-action scan is clean.

## Docs Or Runbook Change

- Commands are syntactically plausible on Windows PowerShell or explicitly
  marked otherwise.
- Safety boundaries are not weakened.
- Any live-submit/order examples are clearly forbidden or gated by policy.

## Tick Logging Or Capture Change

- Markets/series are explicit.
- Output path and ledger format are documented.
- PID/process registry entry is created when background capture starts.
- Capture can be stopped without touching order paths.
