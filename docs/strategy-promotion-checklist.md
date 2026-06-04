# Strategy Promotion Checklist

Use this when deciding whether a Kalshi/event-contract strategy should move from
research to tick logging, paper, or any stronger consideration.

## Required Evidence

- Contract target is explicit: event, market, binary/bracket outcome, settlement
  source, and close/expiration semantics.
- Data is point-in-time clean with separate source and received timestamps.
- Baseline is present: market-implied, naive statistical, or simple persistence
  model as appropriate.
- Candidate model is evaluated OOS with chronological or walk-forward splits.
- Proper scoring is reported: Brier for binary/brackets, log loss when useful,
  calibration/ECE by confidence and time-to-close buckets.
- Probability gap is translated to executable touch, not midpoint.
- Fees, spread, slippage, size, position caps, and stale-source gates are
  included.
- Liquidity and book depth are measured from WS or equivalent evidence when
  tradability matters.
- CLV, markout, or settlement evidence supports the edge claim.
- Python/Rust parity and strategy payload contracts are updated when runtime
  behavior changes.
- Paper/no-network smoke path is easy to run.
- Dangerous-action scan is clean.

## Decision Ladder

- Kill: OOS loses to baseline, edge disappears at touch, or data is leaky/stale.
- Continue research: model improves scoring but executable edge is unproven.
- Start tick logging: fee-net candidate exists or liquidity/freshness is the
  remaining unknown.
- Paper only: executable candidate exists but settlement/markout sample is small.
- Promote later: paper evidence is stable, risk limits are explicit, and current
  repo/user policy allows the next step.

## Minimum Report Shape

- Hypothesis:
- Target contract:
- Data and leakage controls:
- OOS and calibration metrics:
- Fee/spread/liquidity-adjusted edge:
- Verification commands:
- Decision:
