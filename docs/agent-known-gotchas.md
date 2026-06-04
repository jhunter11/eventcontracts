# Agent Known Gotchas

Use this before trusting a result in `C:\QWS\eventcontracts`.

## Safety

- Repo-local `AGENTS.md` wins over all workflow docs. If it forbids orders,
  cancels, authenticated writes, or `--live-submit`, stop before those paths.
- Presence of Kalshi credentials is not permission to trade.
- Public REST and WS reads are preferred for research probes.

## Measurement

- Calibration is not edge. Convert probability gaps to executable touch, fees,
  spread, liquidity, stale-source gates, markout, and settlement evidence.
- Treat large model-vs-market center gaps as likely measurement defects until
  proven otherwise.
- Top-of-book midpoint is not tradable. Use bid/ask touch and size.
- Network freshness and source timestamps matter more than compute latency for
  most live-event strategies.

## Time And Leakage

- Check point-in-time integrity before reporting accuracy.
- Do not use whole-day, whole-match, final-score, settlement, or post-release
  fields as live features.
- Preserve source time and received time separately.
- For Kalshi market timing, use `close_time` or `expected_expiration_time` when
  appropriate. Do not rely on stale or wrong expiration fields.

## Repo-Specific

- Weather KXHIGH settles on integer highs; bracket pricing needs the 0.5
  continuity correction. High-so-far is a lower bound.
- Tennis payloads must preserve `player_1_win_probability`,
  `model_confidence`, and `odds_present` when crossing strategy/runtime
  boundaries.
- Python/Rust schema, parity fixture, and decision changes should move together.
- Generated scripts should run once through a fixture or no-network path before
  being called done.
- On Windows, prefer `.venv\Scripts\python.exe -m ...` and avoid heredocs.

## Fast Start

```powershell
powershell -ExecutionPolicy Bypass -File scripts\codex-intake.ps1
powershell -ExecutionPolicy Bypass -File scripts\verify-eventcontracts.ps1 -ChangedOnly -RunDangerScan
```
