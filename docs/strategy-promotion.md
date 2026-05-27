# Promoting a Python-researched strategy to the Rust live-runner

This is the single-page reference for the cross-language strategy seam. The
goal: a researcher edits a TOML spec; the same TOML drives both the Python
research stack and the Rust live-runner.

## The contract

A strategy is a `(name, version)` pair plus a parameter map. Both pieces live
in a single TOML file under `configs/strategies/` (or a bundle's
`strategy_spec.toml`). The schema is
[`contracts/schemas/strategy_spec.schema.json`](../contracts/schemas/strategy_spec.schema.json).

Minimal spec:

```toml
strategy_id = "weather-threshold-v1"
name = "weather_threshold"
version = "0.1.0"

[subscription]
venues = ["kalshi"]
instrument_patterns = ["KXHIGH*"]
event_kinds = ["quote", "trade"]

[parameters]
buy_below = "0.40"
sell_above = "0.60"
size = "10"
```

`name` is the registry key. Python looks it up in
`python/src/eventcontracts/strategy/registry.py`. Rust looks it up in
[`rust/crates/runner/src/registry.rs`](../rust/crates/runner/src/registry.rs).
Both sides must register the same `name` for a strategy to be promotable.

## Adding a new strategy (the simple path)

### Step 1 — research in Python

Write the strategy under `python/src/eventcontracts/plugins/strategies/`
following the existing pattern (subclass `StrategyBase`, implement `on_event`,
register with `@register("your_name")`). Validate it with backtests and the
existing `make quality` gate.

### Step 2 — author the TOML spec

Drop a file in `configs/strategies/your_name.toml` with the parameters your
strategy reads. Python validates it via `contracts/schemas/strategy_spec.schema.json`.

### Step 3 — port to Rust

Add a Rust impl under `rust/crates/runner/src/` (or a new crate). It needs
two trait impls:

```rust
use eventcontracts_runner::{FromSpec, StrategyRuntime, StrategySpecArtifact, SpecError};

pub struct MyStrategy { /* state */ }

impl FromSpec for MyStrategy {
    fn from_spec(spec: &StrategySpecArtifact) -> Result<Self, SpecError> {
        let buy_below = spec.param_f64("buy_below")?;
        // ...
        Ok(MyStrategy { /* ... */ })
    }
}

impl StrategyRuntime for MyStrategy {
    fn strategy_id(&self) -> &str { /* ... */ }
    fn sleeve_id(&self) -> &str { /* ... */ }
    fn on_event(&mut self, event: &NormalizedEventRecord)
        -> Result<Vec<DecisionPayload>, RunnerError> { /* ... */ }
}
```

### Step 4 — register it

One line in [`rust/crates/runner/src/registry.rs`](../rust/crates/runner/src/registry.rs)
inside `default_registry()`:

```rust
r.register("your_name", |spec| Ok(Box::new(MyStrategy::from_spec(spec)?)));
```

That's it.

### Step 5 — run it live

```bash
cargo build -p eventcontracts-live-runner --release
./rust/target/release/eventcontracts-live-runner \
  --strategy-spec configs/strategies/your_name.toml \
  --pattern KXHIGH \
  --max-markets 5 \
  --duration-secs 60
```

Live Kalshi WS data flows through your strategy → risk gate → `DryRunGateway`.
No orders are placed.

## What's already plumbed

- [`ThresholdStrategy`](../rust/crates/runner/src/lib.rs) — Rust impl for the
  `weather_threshold` and `example_threshold` spec names. Reads
  `buy_below`, `sell_above`, `size`.
- [`StrategySpecArtifact`](../rust/crates/runner/src/spec.rs) — TOML loader.
- [`default_registry()`](../rust/crates/runner/src/registry.rs) — built-in
  factories.
- [`DryRunGateway`](../rust/crates/gateway/src/lib.rs) — risk + OMS +
  idempotency + scheduler, with `RecordingVenueClient` so no orders leave the
  process.
- [`KalshiWsClient`](../rust/crates/kalshi/src/ws.rs) — authenticated WS
  subscriber.
- [`KalshiRest`](../rust/crates/kalshi/src/rest.rs) — authenticated REST for
  market discovery.
- [`KalshiAuth`](../rust/crates/kalshi/src/auth.rs) — RSA-PSS-SHA256 signing,
  matches the Python adapter byte-for-byte.

## Boundaries the contract preserves

- **Strategies return values; they never touch the network.** This is true on
  both sides. `StrategyRuntime::on_event` returns `Vec<DecisionPayload>`.
- **Risk runs in the runner AND in the gateway** — twice, by design. Both
  evaluations use the same `SleeveState`/`RiskLimits` types.
- **The dry-run gateway is the only ack source until live credentials are
  approved.** Adding a real Kalshi `VenueClient` is gated behind explicit
  live-promotion review.
- **TOML is the cross-language wire format.** Anything more
  language-specific (Python class hierarchies, Rust trait objects) lives on
  one side and never leaks across.

## Parity expectations

The Rust impl is not authoritative — Python research is. When a strategy
graduates from paper to dry-run-live:

1. Generate parity cases by replaying a captured partition through Python
   and recording (event_id, decision) pairs.
2. Replay the same partition through Rust via the live-runner against fixture
   data (Phase 2 of [docs/live-rust-runner-roadmap.md](live-rust-runner-roadmap.md)).
3. Diff. Tolerances belong in the bundle manifest, not in code.

No strategy goes to tiny-live until parity is green.

## Limits today (May 2026)

- Only `quote`-kind events drive `ThresholdStrategy`. Most weather strategies
  also need `external` events (Open-Meteo forecasts); the Rust runner has no
  Open-Meteo poller yet — that's Workstream B in
  [docs/live-deployment-remaining-roadmap.md](live-deployment-remaining-roadmap.md).
- Rust decimal math uses `f64`. Replace with `rust_decimal` once parity tests
  exist and reveal a tolerance gap.
- Strategy state is in-memory only. Snapshots/recovery come in Phase 8 of the
  runner roadmap.
