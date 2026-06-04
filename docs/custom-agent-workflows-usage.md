# Custom Agent Workflows Usage

This guide explains the custom Codex and Claude Code workflows for
`C:\QWS\eventcontracts`. Use it when starting a new agent session, handing work
to another agent, or deciding which workflow should handle a task.

Repo-local `AGENTS.md` is always authoritative. If it forbids trading, orders,
cancels, authenticated writes, or `--live-submit`, do not touch those paths.

## Quick Start

Start most non-trivial sessions with:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\codex-intake.ps1
```

Then get a surface-specific file map:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\codex-context-pack.ps1 -Surface weather
```

Before running broad verification in a dirty worktree, preview the gates:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\verify-eventcontracts.ps1 -ChangedOnly -RunDangerScan -ListOnly
```

Then run focused verification or the changed-only gate when appropriate.

## Codex Skills

The Codex skills live under `C:\Users\jachu\.codex\skills`.

### eventcontracts-audit

Use for audits, reviews, readiness checks, deployment risk, leakage checks,
Kalshi time-field mistakes, parity gaps, or strategy safety.

Example prompt:

```text
Use eventcontracts-audit to review the weather KXHIGH strategy for leakage,
Kalshi timing-field mistakes, parity gaps, and whether it is ready for tick
logging.
```

Expected output:

- findings first, ordered by severity;
- file/line references;
- impact and minimal fix;
- tested/untested areas and residual risk.

### eventcontracts-implement

Use when asking an agent to build, fix, wire, remediate, or make a repo change.

Example prompt:

```text
Use eventcontracts-implement to add a pre-round golf model research script and
tests. Run intake, inspect relevant files, patch surgically, preserve unrelated
dirty files, then run focused verification.
```

Expected behavior:

- inspect before editing;
- update tests/configs/parity/docs together when contracts move;
- preserve unrelated dirty files;
- verify before reporting completion.

### eventcontracts-verify

Use after edits or before reporting completion.

Example prompt:

```text
Use eventcontracts-verify on the current diff. Run the planned gate list first,
then run only the focused gates that match this task.
```

Default command:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\verify-eventcontracts.ps1 -ChangedOnly -RunDangerScan
```

Dirty-worktree preview:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\verify-eventcontracts.ps1 -ChangedOnly -RunDangerScan -ListOnly
```

### eventcontracts-edge-validation

Use for expected-profit research, Kalshi/event-contract experiments, tick
logging decisions, paper promotion, or kill reports.

Example prompt:

```text
Use eventcontracts-edge-validation to decide whether the tennis sharp-lag model
has enough fee-net executable edge to start tick logging or should be killed.
```

Required discipline:

- calibration is not edge;
- use executable touch, not midpoint only;
- include fees, spread, liquidity, stale-source gates, CLV/markout, and
  settlement evidence.

## Claude Code Skills

Mirrored Claude Code skills live under `C:\Users\jachu\.claude\skills`:

- `eventcontracts-audit`
- `eventcontracts-implement`
- `eventcontracts-verify`
- `eventcontracts-edge-validation`

They mirror the Codex workflows so Claude Code and Codex agents use the same
operational habits.

## Claude Slash Commands

Repo-local slash command prompts live under `.claude\commands`.

### /eventcontracts-intake

Use at the start of a session. It tells Claude to read `AGENTS.md`, run intake,
and summarize safety constraints, dirty state, background capture processes, and
the recommended verification command.

### /eventcontracts-context

Use before editing a specific surface.

Examples:

```text
/eventcontracts-context weather
/eventcontracts-context tennis
/eventcontracts-context rust
```

Valid surfaces come from `scripts\codex-context-pack.ps1`: `all`, `weather`,
`tennis`, `btc`, `macro`, `sports`, `rust`, and `docs`.

### /eventcontracts-implement

Use when Claude should make a change. The command points Claude at intake,
context pack, surgical patching, and verification.

Example:

```text
/eventcontracts-implement Add a pre-round golf research scaffold and no-network tests.
```

### /eventcontracts-verify

Use after edits. It defaults to changed-only verification plus the dangerous
action scan, with `-ListOnly` as the planning mode for dirty worktrees.

### /eventcontracts-experiment

Use when starting or assessing an edge experiment. It points Claude at the
edge-validation workflow and the experiment report scaffold.

Example:

```text
/eventcontracts-experiment weather-ladder-cdf
```

## Repo Scripts

### scripts\codex-intake.ps1

Prints the session starting state:

- branch and dirty files;
- active `AGENTS.md` safety constraints;
- Python/Rust/git versions;
- background eventcontracts processes;
- process registry tail;
- recommended next verification command.

Use:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\codex-intake.ps1
```

### scripts\codex-context-pack.ps1

Prints relevant files for a surface so agents read the right amount of repo
context.

Use:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\codex-context-pack.ps1 -Surface tennis
```

### scripts\verify-eventcontracts.ps1

Routes verification to focused Python, Rust, parity, and dangerous-action gates.

Common usage:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\verify-eventcontracts.ps1 -ChangedOnly -RunDangerScan -ListOnly
```

Focused usage:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\verify-eventcontracts.ps1 `
  -PythonTests "python\tests\test_weather_kxhigh.py" `
  -RuffTargets "python\src\eventcontracts\weather\distribution.py" `
  -MypyTargets "python\src\eventcontracts\weather\distribution.py"
```

Rust workspace usage:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\verify-eventcontracts.ps1 -CargoWorkspace
```

### scripts\check-dangerous-actions.ps1

Scans diffs or explicit paths for suspicious order/cancel/live-submit additions.
This is not a replacement for review; it is a tripwire.

Use on the current diff:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\check-dangerous-actions.ps1
```

Use on explicit paths:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\check-dangerous-actions.ps1 `
  -Path "scripts\my_script.ps1","python\scripts\my_model.py"
```

### scripts\new-experiment-report.ps1

Creates a standard markdown report and JSONL ledger scaffold for an experiment.

Preview:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\new-experiment-report.ps1 -Name tennis-residual-model -NoWrite
```

Create:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\new-experiment-report.ps1 -Name tennis-residual-model
```

## Supporting Docs

- `docs\agent-workflows.md`: short shared workflow reference.
- `docs\agent-known-gotchas.md`: common repo traps and safety reminders.
- `docs\agent-definition-of-done.md`: completion criteria by task type.
- `docs\agent-failure-playbooks.md`: fixes for common validation failures.
- `docs\strategy-promotion-checklist.md`: kill / tick-log / paper / promote
  decision checklist.
- `docs\codex-workflow-hardening.md`: Codex-specific workflow hardening notes.

## Recommended Flows

### Audit Only

1. Run intake.
2. Use `eventcontracts-audit`.
3. Read only the context pack and relevant files.
4. Report findings first.

### Implement And Verify

1. Run intake.
2. Run a context pack.
3. Use `eventcontracts-implement`.
4. Patch surgically.
5. Run `verify-eventcontracts.ps1 -ChangedOnly -RunDangerScan -ListOnly`.
6. Run focused gates.
7. Report commands, exit codes, and residual risk.

### Edge Experiment

1. Use `eventcontracts-edge-validation`.
2. Create an experiment scaffold.
3. Define contract target, data, leakage controls, and baselines.
4. Score OOS probabilities with Brier/log loss/calibration.
5. Convert gaps to executable fee/spread/liquidity-adjusted EV.
6. Decide: kill, continue research, start tick logging, paper only, or promote
   later.

### Claude Code Session

1. Run `/eventcontracts-intake`.
2. Run `/eventcontracts-context <surface>`.
3. Run `/eventcontracts-implement ...` or `/eventcontracts-experiment ...`.
4. Run `/eventcontracts-verify`.

## Future Workflow Ideas

### Process Registry Writer

Add `scripts\register-process.ps1` and `scripts\stop-registered-process.ps1` to
write/read `live-test\process-registry.jsonl`. This would make WS capture and
paper loops easier to audit and stop safely.

### Market Discovery Workflow

Add `scripts\discover-kalshi-surface.ps1 -Surface sports` to standardize
read-only market discovery, liquidity summaries, resolution links, and candidate
ranking.

### Parity Case Generator

Add a workflow that creates Python/Rust parity fixture skeletons from a strategy
spec and one sample event. This would reduce schema drift when a strategy moves
toward runtime integration.

### Strategy Report Summarizer

Add a script that reads experiment JSONL ledgers and emits a compact promotion
report with OOS metrics, CLV/markout, liquidity, and remaining blockers.

### Data Freshness Auditor

Add a reusable freshness scan for ledgers and features: source timestamp,
received timestamp, model timestamp, quote timestamp, and maximum tolerated age.

### Paper Smoke Launcher

Add safe paper-only launchers that require explicit strategy/sleeve paths, write
a process registry entry, and refuse `--live-submit`.

### Test Selection Map

Promote the changed-only verifier's heuristics into a maintained
`docs\verification-matrix.md` or TOML map so new strategy families can declare
their expected tests without editing PowerShell.

### Experiment Dashboard

Build a small report/dashboard over `live-test\experiments` and tick ledgers to
compare candidate strategies by calibration, CLV, spread, capacity, and kill
reason.

### Agent Handoff Template

Add `docs\agent-handoff-template.md` for long-running tasks: objective, touched
files, commands run, background processes, open risks, and exact next command.

### Research Blindfold Protocol

For independent model rediscovery work, add a prompt template that prevents the
agent from reading existing model internals until after it has produced its own
market selection, feature groups, baselines, and OOS conclusion.
