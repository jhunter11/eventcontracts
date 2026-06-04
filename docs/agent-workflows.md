# Agent Workflows

These workflows are shared by Codex and Claude Code for
`C:\QWS\eventcontracts`.

## Intake

Run at the start of a non-trivial task:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\codex-intake.ps1
```

Use the output to identify active safety constraints, dirty worktree state,
background capture processes, and the likely verification command.

## Context Pack

Use a surface-specific context pack before editing:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\codex-context-pack.ps1 -Surface weather
```

Valid surfaces: `all`, `weather`, `tennis`, `btc`, `macro`, `sports`, `rust`,
`docs`.

## Implementation

1. Read repo `AGENTS.md` and the relevant context pack.
2. Inspect before editing with `rg`, `git status --short`, and targeted file
   reads.
3. Patch surgically and preserve unrelated dirty files.
4. Update tests, configs, parity fixtures, and docs together when contracts move.
5. Run changed-only verification.

## Verification

```powershell
powershell -ExecutionPolicy Bypass -File scripts\verify-eventcontracts.ps1 -ChangedOnly -RunDangerScan
```

In a dirty worktree, preview planned gates first:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\verify-eventcontracts.ps1 -ChangedOnly -RunDangerScan -ListOnly
```

Add explicit targets when changed-only is too broad or too narrow:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\verify-eventcontracts.ps1 -PythonTests "python\tests\test_weather_kxhigh.py" -RuffTargets "python\src\eventcontracts\weather\distribution.py" -MypyTargets "python\src\eventcontracts\weather\distribution.py"
```

## Experiment Reports

Create a standard report and JSONL scaffold:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\new-experiment-report.ps1 -Name weather-ladder-cdf
```

Use the resulting report to decide: kill, continue research, start tick logging,
paper only, or promote later.

## References

- `docs\custom-agent-workflows-usage.md`
- `docs\agent-known-gotchas.md`
- `docs\agent-definition-of-done.md`
- `docs\agent-failure-playbooks.md`
- `docs\strategy-promotion-checklist.md`
