# Codex Workflow Hardening

Date: 2026-06-03

This repo benefits from a strict audit, implementation, verification, and
edge-validation loop.
The recurring failure modes are avoidable: data leakage, wrong Kalshi time
fields, Python/Rust parity drift, Windows script encoding issues, and declaring
work complete before tests run.

## Installed Codex Skills

Reusable skills were installed under `C:\Users\jachu\.codex\skills`:

- `eventcontracts-audit`: read-only audit for leakage, time-field, parity, and
  live-readiness risks.
- `eventcontracts-implement`: surgical build/fix workflow with intake, context,
  patching, and verification.
- `eventcontracts-verify`: focused verification gates for Python/Rust changes.
- `eventcontracts-edge-validation`: prove-or-kill workflow for expected-profit
  strategy experiments.

Use them by name in future prompts, for example:

```text
Use eventcontracts-audit on the weather KXHIGH path before changing code.
Use eventcontracts-implement to build the requested strategy change.
Use eventcontracts-verify after these edits.
Use eventcontracts-edge-validation to decide whether this strategy deserves tick logging.
```

## Repo Guardrails

- Before reporting model performance, check for point-in-time leakage. Do not
  use whole-match, whole-day, settlement, final-score, or future quote fields in
  live features.
- When filtering Kalshi markets by time, prefer `close_time` or
  `expected_expiration_time`; do not rely on an ambiguous expiration field.
- For known ladders, use series-scoped discovery before broad market scans.
- After Python/Rust feature, payload, or strategy-contract changes, update both
  sides and run parity fixtures.
- Run generated scripts through a fixture or `--no-network` path before calling
  them done.
- Keep PowerShell and generated scripts ASCII-safe; avoid em dashes and heredoc
  tricks that break Windows parsing.
- Preserve the repo safety boundary: no orders, no cancels, no live-submit
  unless a future task explicitly changes the policy and supplies promotion
  evidence.

## Verification Helper

Use `scripts\verify-eventcontracts.ps1` for changed-only focused checks:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\verify-eventcontracts.ps1 -ChangedOnly -RunDangerScan
```

Pass explicit `-PythonTests`, `-RuffTargets`, `-MypyTargets`, or
`-CargoWorkspace` when the current dirty worktree makes changed-only too broad.
Use `-ListOnly` first to preview planned gates without running them.
