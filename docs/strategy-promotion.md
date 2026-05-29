# Promoting a Python-Researched Strategy to Rust Live Runner

This is the single-page reference for the cross-language strategy contract.
The goal is that a researcher edits TOML specs and parity fixtures, and the
same TOML drives both the Python research stack and the Rust live runner.

## The Contract

A strategy is a `(name, version)` pair plus a parameter map. Both pieces live
in a single TOML file under `configs/strategies/` or a bundle's
`strategy_spec.toml`. The schema is
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
`python/src/eventcontracts/strategy/registry.py`. Rust first looks it up in
[`rust/crates/runner/src/registry.rs`](../rust/crates/runner/src/registry.rs),
then falls back to recognized spec-driven archetypes declared in `[tags]` or
`[parameters]`.

## Promotion Model

The default path is now spec-driven archetypes, not one-off Rust ports. Known
archetypes let a researcher add a strategy by editing TOML and parity fixtures;
no new Rust is needed when the strategy fits the archetype.

Supported archetypes:

- `threshold`: quote-mid threshold strategy using `buy_below`, `sell_above`,
  and `size`.
- `external_edge`: slow external probability signal versus quote mid using
  `signal_source`, `min_edge_bps`, and `size`.
- `model_edge`, `scalper`, `arb`: reserved names for the next config-only
  runtimes. Until a Rust archetype exists, these require a normal Rust
  implementation before live promotion.

PyO3 embedding remains a future option for non-latency Python strategies, but
it is not the current live path. Rust production runtimes stay deterministic,
reviewable, and parity-gated.

## Adding A New Strategy

### Step 1 - Scaffold The Config Set

For an archetyped strategy, start with:

```bash
ec new-strategy your-name --archetype external_edge
```

In a source checkout, the same command is available as:

```bash
PYTHONPATH=python/src python -m eventcontracts.cli new-strategy your-name --archetype external_edge
```

This creates the strategy spec, paper sleeve, and parity directory.

### Step 2 - Research In Python

Write the strategy under `python/src/eventcontracts/plugins/strategies/`
following the existing pattern: subclass `StrategyBase`, implement `on_event`,
and register with `@register("your_name")`. Validate it with backtests and the
existing `make quality` gate.

For known archetypes, this Python plugin is optional for Rust live promotion:
the TOML parameters are the cross-language contract. A Python plugin is still
useful for notebooks and richer research workflows.

### Step 3 - Author Or Tune The TOML Spec

Drop a file in `configs/strategies/your-name.toml` with the parameters your
strategy reads. Python validates it via `contracts/schemas/strategy_spec.schema.json`.
Declare the archetype when the strategy is config-only:

```toml
[tags]
archetype = "external_edge"
mode = "paper"
```

### Step 4 - Add Rust Only When No Archetype Fits

Add a Rust impl under `rust/crates/runner/src/` or a new crate. It needs two
trait impls:

```rust
use eventcontracts_runner::{
    DecisionPayload, FromSpec, RunnerError, SpecError, StrategyContext,
    StrategyEvent, StrategyRuntime, StrategySpecArtifact,
};

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
    fn on_event(&mut self, event: &StrategyEvent, ctx: &StrategyContext)
        -> Result<Vec<DecisionPayload>, RunnerError> { /* ... */ }
}
```

### Step 5 - Register Non-Archetype Rust Implementations

One line in [`rust/crates/runner/src/registry.rs`](../rust/crates/runner/src/registry.rs)
inside `default_registry()`:

```rust
r.register("your_name", |spec| Ok(Box::new(MyStrategy::from_spec(spec)?)));
```

Archetyped strategies do not need a registry line. `external_edge` is loaded
from TOML by the registry fallback.

### Step 6 - Verify Before Promotion

```bash
make verify-strategy your-name
make parity-check
```

Any non-paper sleeve or artifact bundle must reference a nonempty parity set
before it is promotable.

### Step 7 - Run It Live

```bash
cargo build -p eventcontracts-live-runner --release
./rust/target/release/eventcontracts-live-runner \
  --strategy-spec configs/strategies/your-name.toml \
  --pattern KXHIGH \
  --max-markets 5 \
  --duration-secs 60
```

Live Kalshi WS data flows through your strategy, risk gate, and `DryRunGateway`.
No orders are placed unless the deployment is explicitly configured for live
execution.

## What Is Already Plumbed

- [`ThresholdStrategy`](../rust/crates/runner/src/lib.rs): Rust impl for the
  `weather_threshold` and `example_threshold` spec names.
- `external_edge`: config-only Rust archetype for slow external probability
  strategies such as `flu_hospitalization_surge` and
  `crop_drought_yield_reversion`.
- [`StrategySpecArtifact`](../rust/crates/runner/src/spec.rs): TOML loader.
- [`default_registry()`](../rust/crates/runner/src/registry.rs): built-in
  factories plus archetype fallback.
- [`DryRunGateway`](../rust/crates/gateway/src/lib.rs): risk, OMS,
  idempotency, and scheduler with a recording venue for dry runs.
- [`KalshiWsClient`](../rust/crates/kalshi/src/ws.rs): authenticated WS
  subscriber.
- [`KalshiRest`](../rust/crates/kalshi/src/rest.rs): authenticated REST for
  market discovery.
- [`KalshiAuth`](../rust/crates/kalshi/src/auth.rs): RSA-PSS-SHA256 signing.

## Boundaries The Contract Preserves

- Strategies return values; they never touch the network.
- Risk runs in the runner and in the gateway, by design.
- The dry-run gateway is the only ack source until live credentials are
  approved.
- TOML is the cross-language wire format.

## Parity Expectations

Python research is not enough for promotion. When a strategy graduates from
paper to dry-run-live:

1. Generate parity cases by replaying a captured partition through Python and
   recording `(event_id, decision)` pairs.
2. Replay the same cases through Rust with `eventcontracts-parity`.
3. Diff the emitted decision payloads. Tolerances belong in the bundle
   manifest, not in strategy code.

No strategy goes to tiny-live until parity is green.

## Limits Today

- Only `external_edge` has the new generic archetype fallback in Rust.
- Rust decimal math in the runner uses fixed-point integers internally and
  emits decimal strings at the boundary; expand parity coverage before adding
  more price math.
- Strategy state is in memory only. Snapshots and recovery remain a runner
  roadmap item.
