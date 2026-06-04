# Claude Code Guidance For Eventcontracts

Read repo `AGENTS.md` first. It is authoritative for safety boundaries. If it
forbids orders, cancels, authenticated writes, or `--live-submit`, do not touch
those paths.

Use these Claude Code skills when they match:

- `eventcontracts-audit` for audits, readiness checks, leakage review, Kalshi
  timing semantics, parity gaps, and live-readiness risk.
- `eventcontracts-implement` for build/fix/remediation tasks.
- `eventcontracts-verify` after edits or before reporting completion.
- `eventcontracts-edge-validation` for expected-profit research, tick logging,
  paper promotion, or kill reports.

Useful workflows:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\codex-intake.ps1
powershell -ExecutionPolicy Bypass -File scripts\codex-context-pack.ps1 -Surface weather
powershell -ExecutionPolicy Bypass -File scripts\verify-eventcontracts.ps1 -ChangedOnly -RunDangerScan
```

Read these docs when relevant:

- `docs\agent-workflows.md`
- `docs\agent-known-gotchas.md`
- `docs\agent-definition-of-done.md`
- `docs\agent-failure-playbooks.md`
- `docs\strategy-promotion-checklist.md`
