//! Live paper run: subscribes to live Kalshi market data with prod keys,
//! feeds events through the SleeveRunner + DryRunGateway, places no orders.
//!
//! Usage:
//!   eventcontracts-live-runner [--pattern <PREFIX>] [--duration-secs N]
//!                              [--max-markets N] [--buy-below 0.40]
//!                              [--size 10]
//!
//! Defaults are conservative. `--buy-below 0` disables order intents so the
//! strategy only emits NoAction, useful for measuring pure event throughput.

use clap::Parser;
use eventcontracts_contracts::{
    canonical_sha256, AuditStamp, Contract, EventProvenance, IntentEnvelopeRecord, Metadata,
    NormalizedEventRecord,
};
use eventcontracts_feature_builder::TennisMatchSnapshot;
use eventcontracts_feature_builder::QUOTE_FEATURE_WIDTH;
use eventcontracts_gateway::{
    AsyncVenueClient, DecisionPayload, DryRunGateway, EnqueueOutcome, GatewayError,
    LiveVenueClient, MarketState, PortfolioGuard, PortfolioPolicy, RecordingVenueClient,
    ToxicityCircuitBreaker, VenueClient,
};
use eventcontracts_kalshi::{
    normalize_ws_payload, reset_sequence_tracking, KalshiAuth, KalshiEnv, KalshiEnvironment,
    KalshiFill, KalshiRest, KalshiVenueClient, KalshiWsClient, KalshiWsEnvelope, NormalizeError,
};
use eventcontracts_model_runtime::{OnnxScorer, OutputSelect, TennisOnnxArtifact};
use eventcontracts_risk::{
    epoch_seconds_from_rfc3339, invalidate_quote_bbo, record_book_bbo, record_quote_bbo,
    utc_day_from_epoch_secs, IntentSnapshot, RiskDecision, RiskGate, RiskLimits,
};
use eventcontracts_runner::{
    build_intent_envelope, default_registry, priority_from_spec, OnnxQuoteStrategy, SpecError,
    StrategyContext, StrategyEvent, StrategyRuntime, StrategySpecArtifact, ThresholdStrategy,
};
use eventcontracts_runtime_hot::{project_event, HotEvent};
use serde::Deserialize;

mod reconcile;
use std::error::Error;
use std::fs::File;
use std::io::{BufRead, BufReader, Write};
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};
use time::format_description::well_known::Rfc3339;
use time::OffsetDateTime;
use tokio::sync::mpsc;

/// Erased venue client: `RecordingVenueClient` for paper, `KalshiVenueClient`
/// for live. Both implement [`LiveVenueClient`] so the runner can call the
/// sync trait (used by startup reconciliation and shutdown bulk cancel)
/// AND the async trait (used by the hot-path submit/cancel inside the main
/// loop, where blocking would freeze ingest).
type BoxedVenue = Box<dyn LiveVenueClient>;

enum WsLoopResult {
    Deadline,
    Shutdown(&'static str),
    Tick,
    Envelope(KalshiWsEnvelope),
    /// The spawned WS reader task has exited — either gracefully on close
    /// or after exhausting its reconnect budget. Either way the runner can
    /// no longer receive ingest; treat this as a terminal shutdown.
    WsTerminated,
}

#[derive(Parser, Debug)]
struct Args {
    /// Market-ticker prefix filter, e.g. "KXHIGHNY" or "KXINX" or "" for any.
    #[arg(long, default_value = "KXHIGHNY")]
    pattern: String,
    /// Max markets to subscribe to.
    #[arg(long, default_value_t = 3)]
    max_markets: usize,
    /// How long to listen.
    #[arg(long, default_value_t = 30)]
    duration_secs: u64,
    /// Strategy triggers PlaceOrder when mid < buy_below. Set to 0 to disable
    /// intents entirely (pure throughput measurement). Ignored when
    /// --strategy-spec is provided.
    #[arg(long, default_value_t = 0.0)]
    buy_below: f64,
    /// Strategy triggers Sell PlaceOrder when mid > sell_above. Set to 1.0
    /// to disable. Ignored when --strategy-spec is provided.
    #[arg(long, default_value_t = 1.0)]
    sell_above: f64,
    /// Order size for any emitted intent.
    #[arg(long, default_value = "5")]
    size: String,
    /// Explicit ticker overrides (skip market discovery).
    #[arg(long, value_delimiter = ',')]
    tickers: Vec<String>,
    /// Path to .env file. If omitted, searches ./.env then ../.env then ../../.env.
    #[arg(long)]
    env_file: Option<PathBuf>,
    /// Path to a Python-authored strategy spec TOML (same schema as
    /// `configs/strategies/*.toml`). When provided, the strategy is
    /// instantiated via the Rust registry from the spec's `name` field.
    #[arg(long)]
    strategy_spec: Option<PathBuf>,
    /// Path to sleeve TOML. Live submit requires this so Rust risk limits
    /// match the promoted sleeve config instead of using dev defaults.
    #[arg(long)]
    sleeve_spec: Option<PathBuf>,
    /// Promoted tennis XGBoost ONNX artifact bundle. When provided with
    /// --tennis-snapshots-jsonl, the runner scores upcoming matches into
    /// external prediction events before consuming live market quotes.
    #[arg(long)]
    tennis_artifact: Option<PathBuf>,
    /// JSONL rows containing `market_id` plus the flattened
    /// TennisMatchSnapshot fields from eventcontracts-feature-builder.
    #[arg(long)]
    tennis_snapshots_jsonl: Option<PathBuf>,
    /// Submit real orders to Kalshi. Off by default; paper mode otherwise.
    /// Requires --max-live-orders, an explicit KALSHI_ENV, and (unless --yes)
    /// an interactive confirmation prompt.
    #[arg(long, default_value_t = false)]
    live_submit: bool,
    /// Hard cap on the number of live order submissions per process. The
    /// runner aborts the loop once this many submits have been attempted,
    /// regardless of how the strategy keeps firing. Required when
    /// --live-submit is set.
    #[arg(long)]
    max_live_orders: Option<u32>,
    /// Skip the interactive confirmation when --live-submit is set. Intended
    /// for automated/scheduled runs that have an external review gate.
    #[arg(long, default_value_t = false)]
    yes: bool,
    /// File-based operator kill switch. If the file appears during a run, the
    /// runner bulk-cancels live orders and engages the sleeve kill switch.
    #[arg(long, default_value = ".eventcontracts.KILL_SWITCH")]
    kill_switch_file: PathBuf,
    /// On startup, cancel any resting venue orders instead of trying to adopt
    /// them into the local OMS.
    #[arg(long, default_value_t = false)]
    cancel_orphans_on_start: bool,
    /// On startup, query the venue for resting orders and adopt every
    /// adoptable order into the local OMS. If an order cannot be adopted, the
    /// runner refuses to start and asks the operator to rerun with
    /// --cancel-orphans-on-start.
    #[arg(long, default_value_t = false)]
    reconcile_on_start: bool,
    /// Optional JSON metrics export written at process exit.
    #[arg(long)]
    metrics_json: Option<PathBuf>,
    /// Optional path for the startup reconciliation diff report (JSON). Written
    /// whether reconciliation is clean or halts, for the operator's record.
    #[arg(long)]
    reconcile_report: Option<PathBuf>,
    /// Optional path for a live metrics snapshot, rewritten ~once per second
    /// with current counters plus the operator-critical gauges (uptime,
    /// kill-switch state, daily realized loss, live order attempts). Tail it or
    /// point a Prometheus textfile collector at it for real-time monitoring.
    #[arg(long)]
    metrics_snapshot_file: Option<PathBuf>,
    /// Portfolio-level gross exposure cap. Enforced by the gateway against
    /// live positions plus open-order reservations.
    #[arg(long, default_value = "1000")]
    portfolio_max_gross: String,
    /// Portfolio group cap in `group=amount` form. Repeat for multiple
    /// correlated groups.
    #[arg(long = "portfolio-group-limit")]
    portfolio_group_limits: Vec<String>,
    /// Instrument prefix to portfolio group in `prefix=group` form. Repeat
    /// for correlated market families, e.g. `kalshi:KXCPI=macro`.
    #[arg(long = "portfolio-group-rule")]
    portfolio_group_rules: Vec<String>,
    /// Fill count that trips the toxicity circuit inside the rolling window.
    #[arg(long, default_value_t = 20)]
    toxicity_max_fills: usize,
    /// Rolling fill-velocity window in milliseconds.
    #[arg(long, default_value_t = 1000)]
    toxicity_window_ms: u64,
    /// Toxicity cooldown in milliseconds; live mode bulk-cancels immediately.
    #[arg(long, default_value_t = 30000)]
    toxicity_cooldown_ms: u64,
}

#[cfg(test)]
mod tests {
    use super::*;

    fn spec_with_params(extras: &str) -> StrategySpecArtifact {
        let toml = format!(
            r#"strategy_id = "my-onnx-v1"
name = "onnx_quote"
version = "0.1.0"
[parameters]
{extras}
"#
        );
        StrategySpecArtifact::from_toml_str(&toml).unwrap()
    }

    #[test]
    fn onnx_quote_factory_rejects_missing_model_path() {
        let spec = spec_with_params(
            r#"buy_yes_above = 0.65
buy_no_below = 0.35
"#,
        );
        match build_onnx_quote_from_spec(&spec) {
            Err(SpecError::InvalidParameter(field, _)) => assert_eq!(field, "model_path"),
            Err(other) => panic!("unexpected error: {other}"),
            Ok(_) => panic!("expected error, got Ok"),
        }
    }

    #[test]
    fn onnx_quote_factory_rejects_threshold_inversion() {
        // buy_no_below > buy_yes_above ⇒ every score would trigger both
        // sides; the factory catches this before reaching ONNX load.
        let spec = spec_with_params(
            r#"model_path = "/tmp/does_not_matter.onnx"
buy_yes_above = 0.3
buy_no_below = 0.7
"#,
        );
        match build_onnx_quote_from_spec(&spec) {
            Err(SpecError::InvalidParameter(field, _)) => {
                assert_eq!(field, "buy_yes_above/buy_no_below")
            }
            Err(other) => panic!("unexpected error: {other}"),
            Ok(_) => panic!("expected error, got Ok"),
        }
    }

    #[test]
    fn onnx_quote_registry_lookup_finds_the_factory() {
        // Confirm the registration site is reachable via the same registry
        // path that `--strategy-spec` uses at runtime. We don't instantiate
        // (would require a real model file); we just check the lookup.
        let mut registry = default_registry();
        registry.register("onnx_quote", build_onnx_quote_from_spec);
        assert!(registry.registered_names().contains(&"onnx_quote"));
    }

    #[test]
    fn tennis_snapshot_row_flattens_into_external_prediction_event() {
        let row: TennisSnapshotRow = serde_json::from_str(
            r#"{
                "market_id": "KXTENNIS-M1",
                "surface": "Hard",
                "tourney_level": "A",
                "best_of": 3,
                "p1_elo": 1600.0,
                "p2_elo": 1500.0,
                "p1_surface_elo": 1580.0,
                "p2_surface_elo": 1510.0
            }"#,
        )
        .unwrap();

        let event = tennis_prediction_event(&row, 0.612345, 7).unwrap();
        assert_eq!(event.event_kind, "external");
        assert_eq!(event.provenance.source, "tennis_xgboost_onnx");
        assert!(event.payload_json.contains("\"market_id\":\"KXTENNIS-M1\""));
        assert!(event
            .payload_json
            .contains("\"player_1_win_probability\":\"0.612345\""));
        event.validate().unwrap();
    }

    #[test]
    fn sleeve_spec_risk_limits_override_dev_defaults() {
        let path =
            std::env::temp_dir().join(format!("eventcontracts-sleeve-{}.toml", std::process::id()));
        std::fs::write(
            &path,
            r#"sleeve_id = "test"
capital_allocation = "1000"

[risk]
max_order_notional = "12"
max_position_notional = "34"
max_daily_loss = "5"
max_open_orders = 3
max_gross_exposure = "56"
max_market_data_age_secs = 7
"#,
        )
        .unwrap();
        let limits = load_risk_limits(Some(&path)).unwrap();
        let _ = std::fs::remove_file(path);
        assert_eq!(limits.max_order_notional, "12");
        assert_eq!(limits.max_position_notional, "34");
        assert_eq!(limits.max_daily_loss, "5");
        assert_eq!(limits.max_open_orders, 3);
        assert_eq!(limits.max_gross_exposure, "56");
        assert_eq!(limits.max_market_data_age_secs, 7);
    }

    #[test]
    fn sleeve_spec_gross_limit_defaults_to_position_limit() {
        let path = std::env::temp_dir().join(format!(
            "eventcontracts-sleeve-default-gross-{}.toml",
            std::process::id()
        ));
        std::fs::write(
            &path,
            r#"[risk]
max_order_notional = "12"
max_position_notional = "34"
"#,
        )
        .unwrap();
        let limits = load_risk_limits(Some(&path)).unwrap();
        let _ = std::fs::remove_file(path);
        assert_eq!(limits.max_gross_exposure, "34");
        assert_eq!(limits.max_open_orders, 10);
    }

    #[test]
    fn live_tennis_sleeve_config_parses_with_conservative_limits() {
        // The committed live sleeve is a go-live artifact: it MUST stay parseable
        // by the live-runner. It carries Python-only fields (currency,
        // max_market_data_age_ms, max_spread, ...) that Rust ignores; this guards
        // against a typo or a future strict field silently breaking the launch.
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../../configs/sleeves/sports-tennis-kalshi-live-a.toml");
        let limits = load_risk_limits(Some(&path)).expect("live tennis sleeve must parse");
        assert_eq!(limits.max_order_notional, "25");
        assert_eq!(limits.max_position_notional, "100");
        assert_eq!(limits.max_daily_loss, "150");
        assert_eq!(limits.max_open_orders, 5);
        assert_eq!(limits.max_gross_exposure, "250");
    }

    #[test]
    fn adoption_subscribes_runner_to_adopted_instruments() {
        let mut subscribed = vec!["KXALREADY".to_string()];
        let added = adopted_subscription_tickers(
            &mut subscribed,
            &[
                "kalshi:KXALREADY".to_string(),
                "kalshi:KXNEW".to_string(),
                "KXOTHER".to_string(),
                "kalshi:KXNEW".to_string(),
            ],
        );

        assert_eq!(added, vec!["KXNEW".to_string(), "KXOTHER".to_string()]);
        assert_eq!(
            subscribed,
            vec![
                "KXALREADY".to_string(),
                "KXNEW".to_string(),
                "KXOTHER".to_string()
            ]
        );
    }

    #[test]
    fn daily_loss_restored_across_restart() {
        let midnight = 1_779_600_000_i64;
        let fills = vec![
            KalshiFill {
                realized_pnl_dollars: Some(serde_json::json!("-100.00")),
                fee_dollars: Some(serde_json::json!("0.02")),
                ts: Some(serde_json::json!(midnight + 10)),
                ..KalshiFill::default()
            },
            KalshiFill {
                realized_pnl_dollars: Some(serde_json::json!("25.00")),
                fee_dollars: Some(serde_json::json!("0.01")),
                ts: Some(serde_json::json!(midnight + 20)),
                ..KalshiFill::default()
            },
            KalshiFill {
                realized_pnl_dollars: Some(serde_json::json!("-50.00")),
                fee_dollars: Some(serde_json::json!("0.02")),
                ts: Some(serde_json::json!(midnight - 1)),
                ..KalshiFill::default()
            },
        ];

        assert_eq!(sum_daily_loss_ticks(&fills, midnight), 1_000_300);
    }
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn Error>> {
    let args = Args::parse();
    load_env(args.env_file.as_deref());

    eprintln!("=== eventcontracts live-runner ===");
    let env = KalshiEnvironment::from_env();
    eprintln!("env:           {:?}", env.env);
    eprintln!("REST base:     {}", env.rest_base());
    eprintln!("WS url:        {}", env.ws_url());

    let auth = KalshiAuth::from_env()?;
    eprintln!(
        "api_key_id:    {}...{}",
        &auth.api_key_id.chars().take(4).collect::<String>(),
        &auth.api_key_id.chars().rev().take(4).collect::<String>()
    );

    // ---------- market discovery (or explicit override) ----------
    let mut tickers: Vec<String> = if !args.tickers.is_empty() {
        args.tickers.clone()
    } else {
        eprintln!("discovering markets matching prefix `{}`...", args.pattern);
        let rest = KalshiRest::new(env.rest_base(), auth.clone())?;
        let markets = rest
            .list_open_markets(args.max_markets, Some(&args.pattern))
            .await?;
        if markets.is_empty() {
            eprintln!("no open markets matching `{}`, aborting", args.pattern);
            return Ok(());
        }
        markets.into_iter().map(|m| m.ticker).collect()
    };
    eprintln!("subscribing to {} markets:", tickers.len());
    for t in &tickers {
        eprintln!("  - {}", t);
    }

    // ---------- venue client (paper vs live) ----------
    let max_live_orders = preflight_live_submit(&args, &env, &tickers)?;
    let venue: BoxedVenue = if args.live_submit {
        let rest = KalshiRest::new(env.rest_base(), auth.clone())?;
        let handle = tokio::runtime::Handle::current();
        Box::new(KalshiVenueClient::new(rest, handle))
    } else {
        Box::new(RecordingVenueClient::new())
    };

    // ---------- ws connect + subscribe ----------
    let connect_t0 = Instant::now();
    let mut ws = KalshiWsClient::new(env.ws_url(), auth.clone());
    ws.connect().await?;
    let channels = [
        "ticker",
        "trade",
        "orderbook_delta",
        "market_lifecycle_v2",
        "fill",
        "order",
    ];
    let initial_ticker_refs: Vec<&str> = tickers.iter().map(|s| s.as_str()).collect();
    ws.subscribe(&channels, &initial_ticker_refs).await?;
    eprintln!("connected + subscribed in {:?}", connect_t0.elapsed());

    // ---------- runner state ----------
    // If --strategy-spec is provided, instantiate via the Rust registry from
    // the same TOML file the Python framework consumes. Otherwise fall back
    // to constructing a ThresholdStrategy from the CLI flags.
    let mut strategy: Box<dyn StrategyRuntime> = if let Some(spec_path) = &args.strategy_spec {
        let spec = StrategySpecArtifact::load(spec_path)
            .map_err(|e| format!("load strategy spec: {e}"))?;
        eprintln!(
            "loaded strategy spec: {}@{} ({})",
            spec.name, spec.version, spec.strategy_id
        );
        let mut registry = default_registry();
        registry.register("onnx_quote", build_onnx_quote_from_spec);
        registry
            .instantiate(&spec)
            .map_err(|e| format!("instantiate strategy: {e}"))?
    } else {
        Box::new(ThresholdStrategy::new(
            "kalshi-live-threshold-v0",
            "kalshi-live-paper",
            args.buy_below,
            args.sell_above,
            &args.size,
        ))
    };
    eprintln!(
        "strategy active: strategy_id={} sleeve_id={}",
        strategy.strategy_id(),
        strategy.sleeve_id()
    );
    let risk_limits = load_risk_limits(args.sleeve_spec.as_deref())?;
    let risk = RiskGate::new(risk_limits.clone());
    // Single source of truth: the gateway owns the sleeve state (it also owns
    // the OMS, which advances positions on fills). The runner used to keep a
    // local mirror and clone in both directions per intent (F1) — gone.
    let mut gateway: DryRunGateway<BoxedVenue> =
        DryRunGateway::new(RiskGate::new(risk_limits), venue);
    gateway.last_look.require_executable_bbo = args.live_submit;
    gateway.last_look.require_l1_depth = args.live_submit;
    if args.live_submit {
        gateway.toxicity = ToxicityCircuitBreaker::enabled(
            args.toxicity_max_fills,
            args.toxicity_window_ms as i64,
            args.toxicity_cooldown_ms as i64,
        );
        // Strict market-state gating in live mode: a `PlaceOrder` for an
        // instrument with no recorded market state is rejected. The
        // lifecycle channel is subscribed below, so this gate forces the
        // runner to receive a lifecycle event before placing.
        gateway.require_market_state = true;
    }
    gateway.portfolio = build_portfolio_guard(&args)?;
    if args.live_submit && (args.cancel_orphans_on_start || args.reconcile_on_start) {
        // Production startup reconciliation. Make local state match venue truth
        // before placing a single order:
        //   1. Restore today's realized loss (idempotent: re-summed from the
        //      venue's fills since UTC midnight — the venue is authoritative).
        //   2. Seed local positions from the venue so risk sizing is correct
        //      after a crash/restart, not sized from an empty book.
        //   3. Record the account balance and resting-order disposition.
        //   4. Resting orders: cancel (--cancel-orphans-on-start) or adopt.
        //   5. HALT if the adopted baseline already breaches risk policy.
        let rest = KalshiRest::new(env.rest_base(), auth.clone())?;
        let mut report = reconcile::ReconcileReport::default();

        let today_start_epoch = current_utc_midnight_epoch_secs();
        let fills = rest.list_fills_since(today_start_epoch).await?;
        let restored_daily_loss =
            restore_daily_loss_from_fills(&mut gateway, &fills, today_start_epoch);
        report.daily_realized_loss_ticks = restored_daily_loss;
        eprintln!(
            "reconcile-on-start: restored daily_realized_loss={} from {} fill(s) since UTC midnight",
            restored_daily_loss,
            fills.len()
        );

        // Seed venue positions into the risk sleeve state (venue truth).
        let positions = rest.list_positions().await?;
        report.positions_seeded =
            reconcile::seed_positions_into_state(&mut gateway.sleeve_state, &positions);
        eprintln!(
            "reconcile-on-start: seeded {} venue position(s) into local risk state",
            report.positions_seeded
        );

        // Account balance: visibility + audit (Rust risk has no cash gate yet;
        // this surfaces the available capital for the operator and the report).
        match rest.get_balance().await {
            Ok(balance) => {
                report.balance_ticks = balance.available_ticks();
                let cents = balance.available_cents();
                eprintln!(
                    "reconcile-on-start: venue balance=${}.{:02}",
                    cents / 100,
                    (cents % 100).abs()
                );
            }
            Err(e) => eprintln!("reconcile-on-start: balance fetch failed (non-fatal): {e}"),
        }

        let resting = rest.list_open_orders().await?;
        eprintln!(
            "reconcile-on-start: venue reports {} resting order(s)",
            resting.len()
        );
        if !resting.is_empty() {
            if args.cancel_orphans_on_start {
                eprintln!("canceling resting venue orders (--cancel-orphans-on-start)");
                match gateway.venue.cancel_all(&rfc3339_now()) {
                    Ok(acks) => {
                        report.resting_orders_cancelled = acks.len();
                        eprintln!("startup bulk cancel acknowledged {} order(s)", acks.len())
                    }
                    Err(e) => return Err(format!("startup cancel-all failed: {e}").into()),
                }
            } else {
                let now = rfc3339_now();
                let mut adopted_instruments = Vec::new();
                for order in &resting {
                    let snapshot = order.to_resting_snapshot(&now).map_err(|e| {
                        format!(
                            "reconcile-on-start: cannot adopt venue order {}; {e}; \
                             rerun with --cancel-orphans-on-start to clear venue truth",
                            order.order_id
                        )
                    })?;
                    adopted_instruments.push(gateway.adopt_resting_order(snapshot)?);
                }
                report.resting_orders_adopted = resting.len();
                let newly_subscribed =
                    adopted_subscription_tickers(&mut tickers, &adopted_instruments);
                if !newly_subscribed.is_empty() {
                    let adopted_refs: Vec<&str> =
                        newly_subscribed.iter().map(|s| s.as_str()).collect();
                    ws.subscribe(&channels, &adopted_refs).await?;
                    eprintln!(
                        "reconcile-on-start: subscribed to {} adopted market(s): {}",
                        adopted_refs.len(),
                        newly_subscribed.join(", ")
                    );
                }
                eprintln!(
                    "reconcile-on-start: adopted {} resting order(s) into local OMS",
                    resting.len()
                );
            }
        }

        // HALT on a breached baseline: a venue position/exposure already outside
        // policy is an operator decision, not something to trade through.
        let clean = report.evaluate_state(&risk, &gateway.sleeve_state);
        write_reconcile_report(args.reconcile_report.as_deref(), &report);
        if !clean {
            return Err(format!(
                "startup reconciliation: adopted venue state breaches risk policy {:?}; \
                 clear or repair venue state before live trading",
                report.risk_breaches
            )
            .into());
        }
        eprintln!(
            "reconcile-on-start: clean baseline ({} position(s), {} resting order(s))",
            report.positions_seeded,
            resting.len()
        );
    }
    // Hard *attempt* counter — never decrements on cancel. Intentional:
    // `--max-live-orders` is a one-way budget on submission events to
    // bound the worst-case venue-credit consumption, not a "currently open"
    // count. (N8)
    if gateway.sleeve_state.kill_switch_engaged {
        return Err(
            "startup reconciliation adopted state that breaches risk; clear or repair venue state before live trading"
                .into(),
        );
    }
    let mut live_place_attempts: u32 = 0;

    // ---------- run loop ----------
    let start_time = Instant::now();
    let deadline = start_time + Duration::from_secs(args.duration_secs);
    let mut metrics = Metrics::default();
    let mut next_corr: u64 = 0;

    if args.tennis_artifact.is_some() != args.tennis_snapshots_jsonl.is_some() {
        return Err(
            "--tennis-artifact and --tennis-snapshots-jsonl must be provided together".into(),
        );
    }
    if let (Some(artifact_dir), Some(snapshot_path)) =
        (&args.tennis_artifact, &args.tennis_snapshots_jsonl)
    {
        // Prefill: warm strategy state with offline tennis predictions before
        // the live WS loop opens. Decisions emitted during prefill are
        // dropped — markets aren't subscribed yet, last_quote_epoch_secs is
        // empty, so risk would reject them anyway. Counting dropped warmup
        // decisions in metrics surfaces the case where a researcher
        // expected an order from prefill alone.
        let events = score_tennis_snapshot_file(artifact_dir, snapshot_path)?;
        eprintln!(
            "scored {} tennis snapshots from {}",
            events.len(),
            snapshot_path.display()
        );
        for normalized in events {
            let strategy_event = match StrategyEvent::from_record(&normalized) {
                Ok(e) => e,
                Err(e) => {
                    metrics.strategy_errors += 1;
                    eprintln!("prefill strategy parse err: {e}");
                    continue;
                }
            };
            let ctx = StrategyContext::from_sleeve_state(rfc3339_now(), &gateway.sleeve_state)
                .with_source_event(normalized.event_id.clone());
            let decisions = strategy.on_event(&strategy_event, &ctx).unwrap_or_default();
            metrics.normalized_events += 1;
            metrics.decisions += decisions.len() as u64;
            if !decisions.is_empty() {
                metrics.prefill_decisions_dropped += decisions.len() as u64;
            }
        }
    }

    // R1: spawn the WS reader on its own task and feed envelopes through a
    // bounded channel. The main loop awaits the channel and the gateway's
    // *async* `process_batch_async`, so a slow REST submit no longer blocks
    // ingest — the WS task keeps draining frames into the channel during
    // the await. Bounded capacity gives natural backpressure: when the
    // main loop falls behind, the WS task awaits on `send`, which in turn
    // applies TCP backpressure on tungstenite.
    let (env_tx, mut env_rx) = mpsc::channel::<KalshiWsEnvelope>(WS_INGEST_CHANNEL_CAPACITY);
    let (reconnect_tx, reconnect_rx) = mpsc::channel::<&'static str>(4);
    let ws_errors_counter = Arc::new(AtomicU64::new(0));
    let ws_task = {
        let channels_owned: Vec<String> = channels.iter().map(|&s| s.to_string()).collect();
        let tickers_owned: Vec<String> = tickers.clone();
        let ws_errors = ws_errors_counter.clone();
        tokio::spawn(async move {
            ws_reader_task(
                ws,
                channels_owned,
                tickers_owned,
                env_tx,
                reconnect_rx,
                ws_errors,
            )
            .await
        })
    };

    eprintln!("running for {}s...", args.duration_secs);
    while Instant::now() < deadline {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            break;
        }
        let recv_t = tokio::select! {
            biased;
            _ = tokio::signal::ctrl_c() => WsLoopResult::Shutdown("ctrl-c"),
            _ = tokio::time::sleep(Duration::from_secs(1)) => WsLoopResult::Tick,
            recv = tokio::time::timeout(remaining, env_rx.recv()) => match recv {
                Err(_) => WsLoopResult::Deadline,
                Ok(Some(env)) => WsLoopResult::Envelope(env),
                Ok(None) => WsLoopResult::WsTerminated,
            },
        };
        let recv_at = Instant::now();
        let env_msg = match recv_t {
            WsLoopResult::Deadline => break,
            WsLoopResult::Shutdown(reason) => {
                gateway.sleeve_state.kill_switch_engaged = true;
                if args.live_submit {
                    cancel_all_or_log_async(&mut gateway, reason).await;
                }
                break;
            }
            WsLoopResult::Tick => {
                if let Some(path) = &args.metrics_snapshot_file {
                    let live = LiveStatus {
                        elapsed_secs: start_time.elapsed().as_secs_f64(),
                        kill_switch_engaged: gateway.sleeve_state.kill_switch_engaged,
                        daily_realized_loss_ticks: gateway.sleeve_state.daily_realized_loss,
                        live_place_attempts,
                    };
                    write_metrics_snapshot(
                        path,
                        &build_metrics_value(&metrics, &tickers, &args, &live),
                    );
                }
                if args.kill_switch_file.exists() {
                    gateway.sleeve_state.kill_switch_engaged = true;
                    if args.live_submit {
                        cancel_all_or_log_async(&mut gateway, "kill-switch-file").await;
                    }
                    break;
                }
                continue;
            }
            WsLoopResult::Envelope(e) => e,
            WsLoopResult::WsTerminated => {
                eprintln!("ws reader task exited; halting main loop");
                gateway.sleeve_state.kill_switch_engaged = true;
                if args.live_submit {
                    cancel_all_or_log_async(&mut gateway, "ws-terminated").await;
                }
                break;
            }
        };

        metrics.raw_events += 1;
        metrics
            .by_channel
            .entry(env_msg.msg_type.clone())
            .and_modify(|c| *c += 1)
            .or_insert(1);

        // normalize
        let normalized = match normalize_ws_payload(&env_msg, OffsetDateTime::now_utc()) {
            Ok(n) => n,
            Err(NormalizeError::Ignored(_)) => {
                metrics.normalize_ignored += 1;
                continue;
            }
            Err(NormalizeError::UnsupportedChannel(c)) => {
                metrics.normalize_unsupported += 1;
                metrics
                    .by_channel
                    .entry(format!("unsupported:{c}"))
                    .and_modify(|x| *x += 1)
                    .or_insert(1);
                continue;
            }
            Err(NormalizeError::SequenceGap { .. }) => {
                metrics.sequence_gaps += 1;
                metrics.normalize_errors += 1;
                eprintln!("sequence gap detected; forcing ws resubscribe");
                if reconnect_tx.send("sequence-gap").await.is_err() {
                    eprintln!("ws task gone; cannot request resubscribe");
                    break;
                }
                continue;
            }
            Err(e) => {
                metrics.normalize_errors += 1;
                eprintln!("normalize err: {e}");
                continue;
            }
        };

        let normalize_done = Instant::now();
        metrics
            .normalize_latency_us
            .push((normalize_done - recv_at).as_micros() as u64);

        metrics.normalized_events += 1;

        if apply_private_venue_event(&normalized, &mut gateway, &mut metrics)? {
            gateway.sleeve_state.kill_switch_engaged = true;
            if args.live_submit {
                cancel_all_or_log_async(&mut gateway, "unknown-private-venue-event").await;
            }
            break;
        }
        if apply_market_lifecycle_event(&normalized, &mut gateway, &mut metrics, &rfc3339_now())
            .await?
        {
            // Lifecycle events drive the gateway's market-state machine
            // (and may emit cancels for resting orders); they do not feed
            // the strategy directly.
            continue;
        }
        if gateway.sleeve_state.kill_switch_engaged {
            if args.live_submit {
                cancel_all_or_log_async(&mut gateway, "gateway-kill-switch").await;
            }
            break;
        }

        // strategy
        let strategy_event = match StrategyEvent::from_record(&normalized) {
            Ok(event) => event,
            Err(e) => {
                metrics.strategy_errors += 1;
                eprintln!("strategy event parse err: {e}");
                continue;
            }
        };
        let now_str = rfc3339_now();
        let now_epoch = epoch_seconds_from_rfc3339(&now_str);
        if let StrategyEvent::Quote {
            instrument,
            bid,
            ask,
            ..
        } = &strategy_event
        {
            // Write quote freshness + mark only into the gateway's sleeve
            // state — that IS the source of truth (it owns the OMS and
            // updates positions on fills). F1.
            // N5: synthetic mid is fiction when the BBO spread is wider than
            // half-a-dollar (a Kalshi binary contract can't realistically be
            // mid-50¢ on a 5¢/95¢ book). Refuse to write a mark in that
            // case — leave the previous mark in place so gross-exposure
            // projections aren't manipulated by a one-sided posting.
            // Current behavior: invalid quotes remove executable BBO/mark
            // entries so the gateway's last-look cannot trade on an old mark.
            let bid_t = bid.ticks() / 100;
            let ask_t = ask.ticks() / 100;
            const HALF_DOLLAR_4DP_TICKS: i64 = 5_000;
            if bid_t > 0 && ask_t > 0 && ask_t.saturating_sub(bid_t) <= HALF_DOLLAR_4DP_TICKS {
                record_quote_bbo(
                    &mut gateway.sleeve_state,
                    instrument,
                    bid_t,
                    ask_t,
                    now_epoch,
                );
            } else {
                invalidate_quote_bbo(&mut gateway.sleeve_state, instrument, now_epoch);
            }
        }
        if let StrategyEvent::Book {
            instrument,
            bids,
            asks,
            ..
        } = &strategy_event
        {
            if let (Some((bid, bid_qty)), Some((ask, ask_qty))) = (bids.first(), asks.first()) {
                let bid_t = bid.ticks() / 100;
                let ask_t = ask.ticks() / 100;
                const HALF_DOLLAR_4DP_TICKS: i64 = 5_000;
                if bid_t > 0 && ask_t > 0 && ask_t.saturating_sub(bid_t) <= HALF_DOLLAR_4DP_TICKS {
                    record_book_bbo(
                        &mut gateway.sleeve_state,
                        instrument,
                        bid_t,
                        *bid_qty,
                        ask_t,
                        *ask_qty,
                        now_epoch,
                    );
                }
            }
        }
        // Read from the gateway by reference for the strategy context — no
        // clone of the sleeve_state per quote. Strategies only need a
        // read-only view; the explicit `&` enforces that.
        let ctx = StrategyContext::from_sleeve_state(now_str.as_str(), &gateway.sleeve_state)
            .with_source_event(normalized.event_id.clone());
        let decisions = match strategy.on_event(&strategy_event, &ctx) {
            Ok(d) => d,
            Err(e) => {
                metrics.strategy_errors += 1;
                eprintln!("strategy err: {e}");
                continue;
            }
        };
        let strat_done = Instant::now();
        metrics
            .strategy_latency_us
            .push((strat_done - normalize_done).as_micros() as u64);
        metrics.decisions += decisions.len() as u64;

        // wrap → risk → gateway
        for decision in decisions {
            let is_place_order = matches!(decision, DecisionPayload::PlaceOrder { .. });
            let feedback_client_order_id = match &decision {
                DecisionPayload::PlaceOrder {
                    client_order_id, ..
                } => Some(client_order_id.clone()),
                DecisionPayload::CancelOrder { .. } => None,
            };
            next_corr += 1;
            let envelope = match wrap_envelope(strategy.as_ref(), next_corr, &decision) {
                Ok(e) => e,
                Err(e) => {
                    metrics.decision_wrap_errors += 1;
                    eprintln!("wrap err: {e}");
                    if let Some(client_order_id) = &feedback_client_order_id {
                        strategy.on_intent_rejected(client_order_id, "decision_wrap_error");
                    }
                    continue;
                }
            };

            let snap = match &decision {
                DecisionPayload::PlaceOrder {
                    client_order_id,
                    instrument_id,
                    outcome_side,
                    side,
                    price,
                    quantity,
                    fair_price,
                    min_executable_edge_ticks,
                    fee_rate_bps,
                    ..
                } => Some(IntentSnapshot {
                    client_order_id: client_order_id.clone(),
                    instrument_id: instrument_id.clone(),
                    outcome_side: *outcome_side,
                    side: *side,
                    price: price.clone(),
                    quantity: quantity.clone(),
                    fair_price: fair_price.clone(),
                    min_executable_edge_ticks: *min_executable_edge_ticks,
                    fee_rate_bps: *fee_rate_bps,
                }),
                DecisionPayload::CancelOrder { .. } => {
                    metrics.cancels += 1;
                    None
                }
            };

            // Kill switch ALWAYS gates even cancels — N10. The full risk
            // suite only fires on place_order (cancels are inherently
            // size-reducing); the gateway's rate budget fires on both kinds.
            if gateway.sleeve_state.kill_switch_engaged {
                metrics.intents_rejected_by_risk += 1;
                metrics
                    .risk_reject_reasons
                    .entry("KillSwitchEngaged".into())
                    .and_modify(|c| *c += 1)
                    .or_insert(1);
                if let Some(client_order_id) = &feedback_client_order_id {
                    strategy.on_intent_rejected(client_order_id, "KillSwitchEngaged");
                }
                continue;
            }

            if let Some(snap) = snap {
                match risk.evaluate(&gateway.sleeve_state, &snap, now_epoch) {
                    RiskDecision::Approved => {
                        metrics.intents_approved += 1;
                    }
                    RiskDecision::Rejected(reason) => {
                        metrics.intents_rejected_by_risk += 1;
                        metrics
                            .risk_reject_reasons
                            .entry(format!("{reason:?}"))
                            .and_modify(|c| *c += 1)
                            .or_insert(1);
                        if let Some(client_order_id) = &feedback_client_order_id {
                            strategy.on_intent_rejected(client_order_id, &format!("{reason:?}"));
                        }
                        continue;
                    }
                }
            }

            if args.live_submit && is_place_order {
                if let Some(cap) = max_live_orders {
                    if live_place_attempts >= cap {
                        eprintln!("hit --max-live-orders cap ({cap}); halting strategy loop");
                        if let Some(client_order_id) = &feedback_client_order_id {
                            strategy.on_intent_rejected(client_order_id, "max_live_orders");
                        }
                        break;
                    }
                }
            }

            // No sync_sleeve_state/clone-back round-trip — gateway is the truth.
            match gateway.enqueue(envelope)? {
                EnqueueOutcome::Enqueued => {}
                EnqueueOutcome::DroppedOldestNonCancel {
                    client_order_id, ..
                } => {
                    metrics.intent_queue_shed_non_cancel += 1;
                    if let Some(client_order_id) = client_order_id {
                        strategy.on_intent_rejected(&client_order_id, "intent_queue_shed");
                    }
                }
                EnqueueOutcome::DroppedIncomingNonCancel {
                    client_order_id, ..
                } => {
                    metrics.intent_queue_shed_non_cancel += 1;
                    if let Some(client_order_id) =
                        client_order_id.or_else(|| feedback_client_order_id.clone())
                    {
                        strategy.on_intent_rejected(&client_order_id, "intent_queue_shed");
                    }
                    continue;
                }
            }
            // R1: `process_batch_async` awaits the venue submit instead of
            // calling `block_in_place` inside the WS task — the spawned ws
            // reader continues filling `env_rx` during the await so ingest
            // is no longer gated on REST round-trip time.
            let acks = gateway.process_batch_async(&rfc3339_now(), 16).await;
            for (_cid, res) in acks {
                match res {
                    Ok(ack) if ack.accepted => {
                        metrics.gateway_acks += 1;
                        if is_place_order {
                            live_place_attempts += 1;
                        }
                    }
                    Ok(ack) => {
                        metrics.gateway_errors += 1;
                        if let Some(client_order_id) = &feedback_client_order_id {
                            strategy.on_intent_rejected(client_order_id, &ack.reasons.join(","));
                        }
                    }
                    Err(GatewayError::Stale { .. }) => {
                        metrics.gateway_stale_drops += 1;
                        if let Some(client_order_id) = &feedback_client_order_id {
                            strategy.on_intent_rejected(client_order_id, "stale");
                        }
                    }
                    Err(err) => {
                        metrics.gateway_errors += 1;
                        if let Some(client_order_id) = &feedback_client_order_id {
                            strategy.on_intent_rejected(client_order_id, &err.to_string());
                        }
                    }
                }
            }
            let dispatch_done = Instant::now();
            metrics
                .end_to_end_us
                .push((dispatch_done - recv_at).as_micros() as u64);
        }
    }

    if args.live_submit {
        cancel_all_or_log_async(&mut gateway, "shutdown").await;
    }
    // Tear down the WS reader task. Dropping `reconnect_tx` ensures the
    // task's select! sees a closed channel; `abort` makes shutdown
    // immediate even if the task is blocked on a slow venue close.
    drop(reconnect_tx);
    ws_task.abort();
    let _ = ws_task.await;
    metrics.ws_errors = metrics
        .ws_errors
        .saturating_add(ws_errors_counter.load(Ordering::Relaxed));
    metrics.duration = start_time.elapsed();

    print_report(&metrics, &tickers, &args);
    if let Some(path) = &args.metrics_json {
        write_metrics_json(path, &metrics, &tickers, &args)?;
    }
    Ok(())
}

/// Validate the safety preconditions for `--live-submit` and (optionally)
/// prompt the operator for confirmation. Returns the live-order cap, or
/// `None` when running paper.
///
/// Refuses to proceed unless:
/// - `--max-live-orders` is set,
/// - the user explicitly set `KALSHI_ENV` (so prod is never the default),
/// - startup reconciliation/cancel policy is explicit,
/// - either `--yes` was passed or stdin confirms with `y`/`yes`.
fn preflight_live_submit(
    args: &Args,
    env: &KalshiEnvironment,
    tickers: &[String],
) -> Result<Option<u32>, Box<dyn Error>> {
    if !args.live_submit {
        return Ok(None);
    }
    let cap = args
        .max_live_orders
        .ok_or("--live-submit requires --max-live-orders")?;
    if cap == 0 {
        return Err("--max-live-orders must be > 0 when --live-submit is set".into());
    }
    if std::env::var("KALSHI_ENV").is_err() {
        return Err(
            "--live-submit refuses to run with an implicit KALSHI_ENV; set \
             KALSHI_ENV=demo or KALSHI_ENV=prod explicitly"
                .into(),
        );
    }
    if !args.reconcile_on_start && !args.cancel_orphans_on_start {
        return Err(
            "--live-submit requires --reconcile-on-start or --cancel-orphans-on-start \
             so venue truth is checked before trading"
                .into(),
        );
    }
    if args.sleeve_spec.is_none() {
        return Err(
            "--live-submit requires --sleeve-spec so Rust risk uses promoted sleeve limits".into(),
        );
    }
    let env_label = match env.env {
        KalshiEnv::Prod => "PROD",
        KalshiEnv::Demo => "demo",
    };
    eprintln!();
    eprintln!("!!!  LIVE SUBMIT  !!!");
    eprintln!("  environment:     {env_label}");
    eprintln!("  REST base:       {}", env.rest_base());
    eprintln!("  hard cap:        {cap} order(s) per process");
    eprintln!("  markets:         {}", tickers.join(", "));
    eprintln!();
    if args.yes {
        eprintln!("  --yes supplied; skipping confirmation prompt");
        return Ok(Some(cap));
    }
    eprint!("Type `yes` to proceed: ");
    std::io::stderr().flush().ok();
    let mut input = String::new();
    std::io::stdin().read_line(&mut input)?;
    let answer = input.trim().to_lowercase();
    if answer != "yes" && answer != "y" {
        return Err("live submit aborted by operator".into());
    }
    Ok(Some(cap))
}

fn build_portfolio_guard(args: &Args) -> Result<PortfolioGuard, Box<dyn Error>> {
    let mut policy = PortfolioPolicy::enabled(args.portfolio_max_gross.clone())?;
    for raw in &args.portfolio_group_limits {
        let (group, limit) = parse_key_value(raw, "portfolio-group-limit")?;
        policy = policy.with_group_limit(group, limit)?;
    }
    for raw in &args.portfolio_group_rules {
        let (prefix, group) = parse_key_value(raw, "portfolio-group-rule")?;
        policy = policy.with_prefix_group(prefix, group);
    }
    Ok(PortfolioGuard::new(policy))
}

#[derive(Debug, Deserialize)]
struct SleeveSpecToml {
    #[serde(default)]
    capital_allocation: Option<String>,
    risk: SleeveRiskToml,
}

#[derive(Debug, Deserialize)]
struct SleeveRiskToml {
    max_order_notional: String,
    max_position_notional: String,
    #[serde(default = "default_max_daily_loss")]
    max_daily_loss: String,
    #[serde(default = "default_max_open_orders")]
    max_open_orders: u64,
    #[serde(default)]
    max_gross_exposure: Option<String>,
    #[serde(default = "default_max_market_data_age_secs")]
    max_market_data_age_secs: u32,
}

fn default_max_daily_loss() -> String {
    "250".into()
}

fn default_max_open_orders() -> u64 {
    10
}

fn default_max_market_data_age_secs() -> u32 {
    30
}

fn load_risk_limits(path: Option<&std::path::Path>) -> Result<RiskLimits, Box<dyn Error>> {
    let Some(path) = path else {
        return Ok(default_limits());
    };
    let text = std::fs::read_to_string(path)?;
    let spec: SleeveSpecToml = toml::from_str(&text)?;
    Ok(RiskLimits {
        max_order_notional: spec.risk.max_order_notional,
        max_position_notional: spec.risk.max_position_notional.clone(),
        max_daily_loss: spec.risk.max_daily_loss,
        max_open_orders: spec.risk.max_open_orders,
        max_gross_exposure: spec
            .risk
            .max_gross_exposure
            .or(spec.capital_allocation)
            .unwrap_or(spec.risk.max_position_notional),
        currency: "USD".into(),
        max_market_data_age_secs: spec.risk.max_market_data_age_secs,
    })
}

fn parse_key_value(raw: &str, flag: &str) -> Result<(String, String), Box<dyn Error>> {
    let (key, value) = raw
        .split_once('=')
        .ok_or_else(|| format!("--{flag} expects key=value, got `{raw}`"))?;
    if key.trim().is_empty() || value.trim().is_empty() {
        return Err(format!("--{flag} expects non-empty key=value, got `{raw}`").into());
    }
    Ok((key.trim().to_string(), value.trim().to_string()))
}

fn adopted_subscription_tickers(
    subscribed_tickers: &mut Vec<String>,
    adopted_instruments: &[String],
) -> Vec<String> {
    let mut added = Vec::new();
    for instrument in adopted_instruments {
        let ticker = instrument
            .strip_prefix("kalshi:")
            .unwrap_or(instrument.as_str())
            .to_string();
        if subscribed_tickers
            .iter()
            .any(|existing| existing == &ticker)
            || added.iter().any(|existing| existing == &ticker)
        {
            continue;
        }
        subscribed_tickers.push(ticker.clone());
        added.push(ticker);
    }
    added
}

fn current_utc_midnight_epoch_secs() -> i64 {
    let now = OffsetDateTime::now_utc().unix_timestamp();
    now - now.rem_euclid(86_400)
}

fn write_reconcile_report(path: Option<&std::path::Path>, report: &reconcile::ReconcileReport) {
    let Some(path) = path else {
        return;
    };
    match serde_json::to_string_pretty(report) {
        Ok(json) => {
            if let Err(e) = std::fs::write(path, json) {
                eprintln!(
                    "reconcile-on-start: failed to write report {}: {e}",
                    path.display()
                );
            }
        }
        Err(e) => eprintln!("reconcile-on-start: failed to serialize reconcile report: {e}"),
    }
}

fn restore_daily_loss_from_fills(
    gateway: &mut DryRunGateway<BoxedVenue>,
    fills: &[KalshiFill],
    since_epoch_secs: i64,
) -> i64 {
    let restored = sum_daily_loss_ticks(fills, since_epoch_secs);
    gateway.sleeve_state.daily_loss_day_utc = utc_day_from_epoch_secs(since_epoch_secs);
    gateway.sleeve_state.daily_realized_loss = restored;
    restored
}

fn sum_daily_loss_ticks(fills: &[KalshiFill], since_epoch_secs: i64) -> i64 {
    fills.iter().fold(0_i64, |acc, fill| {
        let in_window = fill
            .epoch_secs()
            .map(|ts| ts >= since_epoch_secs)
            .unwrap_or(true);
        if in_window {
            acc.saturating_add(fill.realized_loss_ticks())
        } else {
            acc
        }
    })
}

fn load_env(explicit: Option<&std::path::Path>) {
    if let Some(p) = explicit {
        let _ = dotenvy::from_path(p);
        return;
    }
    for candidate in [".env", "../.env", "../../.env"] {
        if std::path::Path::new(candidate).exists() {
            let _ = dotenvy::from_path(candidate);
            return;
        }
    }
}

/// Bound on the ingest channel between the WS reader task and the main
/// loop. Sized so a ~30s burst at ~30 envelopes/sec fits before
/// backpressure kicks in; tuned with `cargo bench` (see Phase 1 R1).
const WS_INGEST_CHANNEL_CAPACITY: usize = 1024;

/// Stable-message run required before resetting reconnect backoff. A
/// flapping connection produced a tight reconnect storm in the
/// pre-R1 monolithic loop because the counter reset on the first message
/// after reconnect.
const STABLE_MSGS_TO_RESET_RECONNECT: u32 = 10;

/// Background task that owns the `KalshiWsClient`, drains envelopes into
/// `env_tx`, and handles its own reconnect budget. Lives on a separate
/// tokio task so a slow venue REST call on the main task cannot stall
/// ingest. The task exits on:
///   - env_tx closed (main loop ended),
///   - reconnect_rx closed (main loop ended), or
///   - reconnect budget exhausted.
async fn ws_reader_task(
    mut ws: KalshiWsClient,
    channels: Vec<String>,
    tickers: Vec<String>,
    env_tx: mpsc::Sender<KalshiWsEnvelope>,
    mut reconnect_rx: mpsc::Receiver<&'static str>,
    ws_errors: Arc<AtomicU64>,
) {
    let chan_refs: Vec<&str> = channels.iter().map(|s| s.as_str()).collect();
    let ticker_refs: Vec<&str> = tickers.iter().map(|s| s.as_str()).collect();
    let mut reconnect_attempts: u32 = 0;
    let mut stable_msg_streak: u32 = 0;

    loop {
        tokio::select! {
            biased;
            reason = reconnect_rx.recv() => {
                match reason {
                    Some(reason) => {
                        eprintln!("ws reconnect requested: {reason}");
                        stable_msg_streak = 0;
                        if let Err(e) = reconnect_ws(
                            &mut ws,
                            &chan_refs,
                            &ticker_refs,
                            &mut reconnect_attempts,
                        )
                        .await
                        {
                            eprintln!("ws reconnect (forced) failed: {e}");
                            break;
                        }
                    }
                    None => break,
                }
            }
            result = ws.next_envelope() => {
                match result {
                    Ok(Some(env)) => {
                        stable_msg_streak = stable_msg_streak.saturating_add(1);
                        if stable_msg_streak >= STABLE_MSGS_TO_RESET_RECONNECT {
                            reconnect_attempts = 0;
                        }
                        if env_tx.send(env).await.is_err() {
                            break;
                        }
                    }
                    Ok(None) => continue,
                    Err(e) => {
                        ws_errors.fetch_add(1, Ordering::Relaxed);
                        eprintln!("ws error: {e}");
                        stable_msg_streak = 0;
                        if let Err(reconnect_err) = reconnect_ws(
                            &mut ws,
                            &chan_refs,
                            &ticker_refs,
                            &mut reconnect_attempts,
                        )
                        .await
                        {
                            eprintln!("ws reconnect failed: {reconnect_err}");
                            break;
                        }
                    }
                }
            }
        }
    }
    let _ = ws.close().await;
}

async fn reconnect_ws(
    ws: &mut KalshiWsClient,
    channels: &[&str],
    ticker_refs: &[&str],
    attempts: &mut u32,
) -> Result<(), Box<dyn Error + Send + Sync>> {
    if *attempts >= 8 {
        return Err("ws reconnect attempt budget exhausted".into());
    }
    *attempts += 1;
    let exp = 1_u64 << (*attempts - 1).min(5);
    let jitter_ms = (OffsetDateTime::now_utc().unix_timestamp_nanos() as u64) % 250;
    let delay = Duration::from_secs(exp.min(30)) + Duration::from_millis(jitter_ms);
    tokio::time::sleep(delay).await;
    let _ = ws.close().await;
    ws.connect().await?;
    reset_sequence_tracking();
    ws.subscribe(channels, ticker_refs).await?;
    eprintln!("ws reconnected after attempt {}", *attempts);
    Ok(())
}

/// Async bulk-cancel for the hot path. Used by the main loop on every
/// shutdown trigger (ctrl-c, kill switch file, gateway kill switch, WS
/// termination, deadline). Uses the async venue trait so a slow venue
/// response doesn't block the runtime tear-down.
async fn cancel_all_or_log_async(gateway: &mut DryRunGateway<BoxedVenue>, reason: &str) {
    match gateway.venue.cancel_all_async(&rfc3339_now()).await {
        Ok(acks) => eprintln!("{reason}: bulk cancel acknowledged {} order(s)", acks.len()),
        Err(e) => eprintln!("{reason}: bulk cancel failed: {e}"),
    }
}

/// Factory registered under the strategy spec `name = "onnx_quote"`.
///
/// TOML schema:
/// ```toml
/// strategy_id = "my-onnx-quote-v1"
/// name = "onnx_quote"
/// version = "0.1.0"
///
/// [parameters]
/// model_path     = "/abs/path/to/model.onnx"   # required
/// buy_yes_above  = 0.65                         # required, model output ≥ → Buy YES
/// buy_no_below   = 0.35                         # required, model output ≤ → Buy NO at (1-bid)
/// size           = "5"                          # default "1"
/// input_name     = "features"                   # default "features"
/// output_name    = "probabilities"              # default "probabilities"
/// output_index   = 1                            # default 1 (P(class=1))
/// ```
///
/// Loads an `OnnxScorer` configured to return the single scalar at
/// `output_index`, then wraps it in an `OnnxQuoteStrategy` whose 4-feature
/// vector (bid/ask/spread/mid) the model must have been trained on.
fn build_onnx_quote_from_spec(
    spec: &StrategySpecArtifact,
) -> Result<Box<dyn StrategyRuntime>, SpecError> {
    let model_path = spec.param_str_or("model_path", "");
    if model_path.is_empty() {
        return Err(SpecError::InvalidParameter(
            "model_path".into(),
            "required for onnx_quote strategies".into(),
        ));
    }
    let buy_yes_above = spec.param_f64("buy_yes_above")? as f32;
    let buy_no_below = spec.param_f64("buy_no_below")? as f32;
    if buy_no_below.partial_cmp(&buy_yes_above) != Some(std::cmp::Ordering::Less) {
        return Err(SpecError::InvalidParameter(
            "buy_yes_above/buy_no_below".into(),
            format!("buy_no_below ({buy_no_below}) must be < buy_yes_above ({buy_yes_above})"),
        ));
    }
    let size = spec.param_str_or("size", "1");
    let input_name = spec.param_str_or("input_name", "features");
    let output_name = spec.param_str_or("output_name", "probabilities");
    let output_index = spec.param_f64_or("output_index", 1.0)? as usize;

    let scorer = OnnxScorer::load(
        &model_path,
        input_name,
        output_name,
        QUOTE_FEATURE_WIDTH,
        OutputSelect::ScalarAt(output_index),
    )
    .map_err(|e| SpecError::Factory(format!("load onnx model `{model_path}`: {e}")))?;

    let sleeve_id = format!("{}-sleeve", spec.strategy_id);
    let (tier, expires_after_ms) = priority_from_spec(spec);
    Ok(Box::new(
        OnnxQuoteStrategy::new(
            spec.strategy_id.clone(),
            sleeve_id,
            scorer,
            buy_yes_above,
            buy_no_below,
            size,
        )
        .with_priority(tier, expires_after_ms),
    ))
}

fn default_limits() -> RiskLimits {
    RiskLimits {
        max_order_notional: "100".into(),
        max_position_notional: "500".into(),
        max_daily_loss: "50".into(),
        max_open_orders: 10,
        max_gross_exposure: "1000".into(),
        currency: "USD".into(),
        max_market_data_age_secs: 60,
    }
}

fn rfc3339_now() -> String {
    OffsetDateTime::now_utc()
        .format(&Rfc3339)
        .unwrap_or_else(|_| "1970-01-01T00:00:00Z".into())
}

fn wrap_envelope(
    strategy: &dyn StrategyRuntime,
    n: u64,
    decision: &DecisionPayload,
) -> Result<IntentEnvelopeRecord, Box<dyn Error>> {
    // Single source of truth lives in `runner::build_intent_envelope`. The
    // producer tag distinguishes live-runner intents from SleeveRunner ones
    // in the audit log. Strategy carries its own priority_tier + TTL from
    // its TOML spec (P3 — was hardcoded "standard" before).
    Ok(build_intent_envelope(
        strategy.strategy_id(),
        strategy.sleeve_id(),
        n,
        decision,
        &rfc3339_now(),
        "live-runner",
        strategy.priority_tier(),
        strategy.expires_after_ms(),
    )?)
}

async fn apply_market_lifecycle_event(
    normalized: &NormalizedEventRecord,
    gateway: &mut DryRunGateway<BoxedVenue>,
    metrics: &mut Metrics,
    now: &str,
) -> Result<bool, Box<dyn Error>> {
    if normalized.event_kind != "lifecycle" {
        return Ok(false);
    }
    let hot_event = match project_event(normalized) {
        Ok(ev) => ev,
        Err(e) => {
            metrics.lifecycle_errors += 1;
            eprintln!("lifecycle projection err: {e}");
            return Ok(true);
        }
    };
    let HotEvent::MarketState(market_state) = hot_event else {
        metrics.lifecycle_errors += 1;
        eprintln!(
            "lifecycle event projected to unexpected variant: kind={:?}",
            hot_event.kind()
        );
        return Ok(true);
    };

    let instrument = market_state.instrument.as_str().to_string();
    let state: MarketState = market_state.state;
    metrics.lifecycle_events += 1;

    let outcome =
        gateway.apply_market_state(&instrument, state, market_state.reason.as_deref(), now)?;
    metrics.lifecycle_cancels_issued += outcome.canceled_client_order_ids.len() as u64;
    if outcome.bbo_invalidated_for_reopen {
        metrics.lifecycle_reopens += 1;
    }
    if !state.is_tradable() && state != MarketState::MetadataUpdated {
        metrics.lifecycle_suspensions += 1;
    }

    // Drain any cancels the state machine enqueued so they actually hit the
    // venue. Use a small batch limit beyond the issued count to absorb any
    // other queued cancels without starving them.
    if !outcome.canceled_client_order_ids.is_empty() {
        let limit = outcome.canceled_client_order_ids.len() + 4;
        let acks = gateway.process_batch_async(now, limit).await;
        for (_cid, res) in acks {
            match res {
                Ok(ack) if ack.accepted => metrics.gateway_acks += 1,
                Ok(_) => metrics.gateway_errors += 1,
                Err(GatewayError::Stale { .. }) => metrics.gateway_stale_drops += 1,
                Err(_) => metrics.gateway_errors += 1,
            }
        }
    }

    eprintln!(
        "lifecycle {state:?} on {instrument} (prev={:?}, canceled={}, bbo_invalidated={})",
        outcome.previous,
        outcome.canceled_client_order_ids.len(),
        outcome.bbo_invalidated_for_reopen,
    );
    Ok(true)
}

fn apply_private_venue_event(
    normalized: &NormalizedEventRecord,
    gateway: &mut DryRunGateway<BoxedVenue>,
    metrics: &mut Metrics,
) -> Result<bool, Box<dyn Error>> {
    // F1: the gateway is the single source of truth for sleeve state. No
    // local mirror to clone back into.
    match normalized.event_kind.as_str() {
        "own_fill" | "own_order_update" | "own_order_reject" => {}
        _ => return Ok(false),
    }
    let mut halt_for_reconciliation = false;
    match project_event(normalized) {
        Ok(HotEvent::OwnFill(fill)) => {
            let oms_fill = eventcontracts_oms::Fill {
                fill_id: fill.fill_id.to_string(),
                client_order_id: fill.client_order_id.to_string(),
                price: fill.price.to_decimal_string(),
                quantity: fill.quantity.raw().to_string(),
                fee: fill.fee.to_decimal_string(),
                trade_ts: normalized.audit.produced_at.clone(),
            };
            match gateway.apply_fill(oms_fill) {
                Ok(true) => metrics.own_fills += 1,
                Ok(false) => metrics.duplicate_own_fills += 1,
                Err(GatewayError::Oms(eventcontracts_oms::OmsError::UnknownOrder(id))) => {
                    metrics.private_event_errors += 1;
                    eprintln!("own fill for unknown order `{id}`; reconciliation required");
                    halt_for_reconciliation = true;
                }
                Err(e) => return Err(Box::new(e)),
            }
        }
        Ok(HotEvent::OwnOrderUpdate(update)) | Ok(HotEvent::OwnOrderReject(update)) => {
            match gateway.apply_order_update(
                update.client_order_id.as_str(),
                update.state.as_str(),
                &normalized.audit.produced_at,
                update.reason.as_deref(),
            ) {
                Ok(()) => metrics.own_order_updates += 1,
                Err(GatewayError::Oms(eventcontracts_oms::OmsError::UnknownOrder(id))) => {
                    metrics.private_event_errors += 1;
                    eprintln!("own order update for unknown order `{id}`; reconciliation required");
                    halt_for_reconciliation = true;
                }
                Err(e) => return Err(Box::new(e)),
            }
        }
        Ok(_) => {}
        Err(e) => {
            metrics.private_event_errors += 1;
            eprintln!("private event projection err: {e}");
        }
    }
    Ok(halt_for_reconciliation)
}

#[derive(Debug, Deserialize)]
struct TennisSnapshotRow {
    market_id: String,
    #[serde(default = "default_tennis_prediction_source")]
    source: String,
    #[serde(flatten)]
    snapshot: TennisMatchSnapshot,
}

fn default_tennis_prediction_source() -> String {
    "tennis_xgboost_onnx".into()
}

fn score_tennis_snapshot_file(
    artifact_dir: &std::path::Path,
    snapshot_path: &std::path::Path,
) -> Result<Vec<NormalizedEventRecord>, Box<dyn Error>> {
    let mut artifact = TennisOnnxArtifact::load_bundle(artifact_dir)?;
    let file = File::open(snapshot_path)?;
    let reader = BufReader::new(file);
    let mut events = Vec::new();
    for (index, line) in reader.lines().enumerate() {
        let line = line?;
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        let row: TennisSnapshotRow = serde_json::from_str(trimmed)?;
        let probability = artifact.predict_snapshot(&row.snapshot)?;
        events.push(tennis_prediction_event(
            &row,
            probability,
            index as u64 + 1,
        )?);
    }
    Ok(events)
}

fn tennis_prediction_event(
    row: &TennisSnapshotRow,
    probability: f32,
    seq: u64,
) -> Result<NormalizedEventRecord, Box<dyn Error>> {
    let now = rfc3339_now();
    let payload = serde_json::json!({
        "source": row.source,
        "market_id": row.market_id,
        "player_1_win_probability": format!("{probability:.6}"),
    });
    let event_id = format!("tennis-xgboost:{}:{seq}", row.market_id);
    let payload_json = payload.to_string();
    let provenance = EventProvenance {
        source: row.source.clone(),
        channel: "artifact-score".into(),
        schema_version: "normalized-event-v1".into(),
        venue: Some("kalshi".into()),
        source_sequence: Some(seq.to_string()),
        normalization_version: "tennis-xgboost-onnx-v1".into(),
        metadata: Metadata::new(),
    };
    let digest = canonical_sha256(&serde_json::json!({
        "event_id": event_id.clone(),
        "event_kind": "external",
        "payload_json": payload_json.clone(),
        "provenance": provenance.clone(),
    }))?;
    let event = NormalizedEventRecord {
        event_id: event_id.clone(),
        event_kind: "external".into(),
        payload_json,
        provenance,
        audit: AuditStamp {
            object_id: event_id,
            object_kind: "normalized_event".into(),
            schema_version: "normalized-event-v1".into(),
            produced_at: now,
            producer: "live-runner-tennis-onnx".into(),
            canonical_sha256: digest,
            parent_ids: vec![],
            trace_id: None,
            metadata: Metadata::new(),
        },
    };
    event.validate()?;
    Ok(event)
}

// ---------- metrics ----------

#[derive(Default)]
struct Metrics {
    duration: Duration,
    raw_events: u64,
    normalized_events: u64,
    normalize_ignored: u64,
    normalize_unsupported: u64,
    normalize_errors: u64,
    sequence_gaps: u64,
    strategy_errors: u64,
    decision_wrap_errors: u64,
    decisions: u64,
    prefill_decisions_dropped: u64,
    cancels: u64,
    intents_approved: u64,
    intents_rejected_by_risk: u64,
    risk_reject_reasons: std::collections::HashMap<String, u64>,
    gateway_acks: u64,
    gateway_stale_drops: u64,
    gateway_errors: u64,
    intent_queue_shed_non_cancel: u64,
    own_fills: u64,
    duplicate_own_fills: u64,
    own_order_updates: u64,
    private_event_errors: u64,
    lifecycle_events: u64,
    lifecycle_errors: u64,
    lifecycle_suspensions: u64,
    lifecycle_reopens: u64,
    lifecycle_cancels_issued: u64,
    ws_errors: u64,
    by_channel: std::collections::HashMap<String, u64>,
    normalize_latency_us: Vec<u64>,
    strategy_latency_us: Vec<u64>,
    end_to_end_us: Vec<u64>,
}

fn percentile(samples: &mut [u64], p: f64) -> u64 {
    if samples.is_empty() {
        return 0;
    }
    samples.sort_unstable();
    let idx = ((samples.len() as f64 - 1.0) * p).round() as usize;
    samples[idx]
}

fn print_report(metrics: &Metrics, tickers: &[String], args: &Args) {
    let secs = metrics.duration.as_secs_f64().max(1e-9);
    let rps = metrics.raw_events as f64 / secs;
    let nps = metrics.normalized_events as f64 / secs;
    let mut nl = metrics.normalize_latency_us.clone();
    let mut sl = metrics.strategy_latency_us.clone();
    let mut e2e = metrics.end_to_end_us.clone();

    println!();
    println!("=== live-runner report ===");
    println!("duration:                       {:.2}s", secs);
    println!(
        "tickers:                        {} ({})",
        tickers.len(),
        tickers.join(",")
    );
    println!("strategy.buy_below:             {}", args.buy_below);
    println!();
    println!("--- counts ---");
    println!(
        "raw ws events:                  {}  ({:.1}/s)",
        metrics.raw_events, rps
    );
    println!(
        "normalized events:              {}  ({:.1}/s)",
        metrics.normalized_events, nps
    );
    println!(
        "  normalize ignored ctrl msgs:  {}",
        metrics.normalize_ignored
    );
    println!(
        "  normalize unsupported:        {}",
        metrics.normalize_unsupported
    );
    println!(
        "  normalize errors:             {}",
        metrics.normalize_errors
    );
    println!("  sequence gaps:                {}", metrics.sequence_gaps);
    println!("strategy decisions:             {}", metrics.decisions);
    println!("cancels:                        {}", metrics.cancels);
    println!(
        "intents approved by risk:       {}",
        metrics.intents_approved
    );
    println!(
        "intents rejected by risk:       {}",
        metrics.intents_rejected_by_risk
    );
    if !metrics.risk_reject_reasons.is_empty() {
        let mut rows: Vec<_> = metrics.risk_reject_reasons.iter().collect();
        rows.sort_by(|a, b| b.1.cmp(a.1));
        for (reason, count) in rows {
            println!("    {reason} -> {count}");
        }
    }
    println!("gateway acks (paper):           {}", metrics.gateway_acks);
    println!(
        "gateway stale drops:            {}",
        metrics.gateway_stale_drops
    );
    println!(
        "intent queue shed non-cancel:   {}",
        metrics.intent_queue_shed_non_cancel
    );
    println!("gateway errors:                 {}", metrics.gateway_errors);
    println!("own fills applied:              {}", metrics.own_fills);
    println!(
        "duplicate own fills:            {}",
        metrics.duplicate_own_fills
    );
    println!(
        "own order updates:              {}",
        metrics.own_order_updates
    );
    println!(
        "private event errors:           {}",
        metrics.private_event_errors
    );
    println!("ws errors:                      {}", metrics.ws_errors);
    println!();
    println!("--- per-channel raw counts ---");
    let mut channels: Vec<_> = metrics.by_channel.iter().collect();
    channels.sort_by(|a, b| b.1.cmp(a.1));
    for (chan, count) in channels {
        println!("  {chan:<24} {count}");
    }
    println!();
    println!("--- latency (microseconds) ---");
    println!(
        "normalize:                       p50={} p95={} p99={} (n={})",
        percentile(&mut nl, 0.50),
        percentile(&mut nl, 0.95),
        percentile(&mut nl, 0.99),
        metrics.normalize_latency_us.len()
    );
    println!(
        "strategy.on_event:               p50={} p95={} p99={} (n={})",
        percentile(&mut sl, 0.50),
        percentile(&mut sl, 0.95),
        percentile(&mut sl, 0.99),
        metrics.strategy_latency_us.len()
    );
    println!(
        "end-to-end recv→gateway ack:      p50={} p95={} p99={} (n={})",
        percentile(&mut e2e, 0.50),
        percentile(&mut e2e, 0.95),
        percentile(&mut e2e, 0.99),
        metrics.end_to_end_us.len()
    );
}

/// Live runtime gauges for the metrics snapshot — the signals an operator must
/// watch in real time on a funded account.
struct LiveStatus {
    elapsed_secs: f64,
    kill_switch_engaged: bool,
    daily_realized_loss_ticks: i64,
    live_place_attempts: u32,
}

fn build_metrics_value(
    metrics: &Metrics,
    tickers: &[String],
    args: &Args,
    live: &LiveStatus,
) -> serde_json::Value {
    serde_json::json!({
        "uptime_secs": live.elapsed_secs,
        "kill_switch_engaged": live.kill_switch_engaged,
        "daily_realized_loss_ticks": live.daily_realized_loss_ticks,
        "daily_realized_loss_usd": live.daily_realized_loss_ticks as f64 / 10_000.0,
        "live_place_attempts": live.live_place_attempts,
        "max_live_orders": args.max_live_orders,
        "tickers": tickers,
        "live_submit": args.live_submit,
        "raw_events": metrics.raw_events,
        "normalized_events": metrics.normalized_events,
        "normalize_errors": metrics.normalize_errors,
        "sequence_gaps": metrics.sequence_gaps,
        "strategy_errors": metrics.strategy_errors,
        "decisions": metrics.decisions,
        "cancels": metrics.cancels,
        "intents_approved": metrics.intents_approved,
        "intents_rejected_by_risk": metrics.intents_rejected_by_risk,
        "gateway_acks": metrics.gateway_acks,
        "gateway_errors": metrics.gateway_errors,
        "gateway_stale_drops": metrics.gateway_stale_drops,
        "intent_queue_shed_non_cancel": metrics.intent_queue_shed_non_cancel,
        "own_fills": metrics.own_fills,
        "duplicate_own_fills": metrics.duplicate_own_fills,
        "own_order_updates": metrics.own_order_updates,
        "private_event_errors": metrics.private_event_errors,
        "ws_errors": metrics.ws_errors,
        "by_channel": metrics.by_channel,
        "risk_reject_reasons": metrics.risk_reject_reasons,
    })
}

/// Write the live metrics snapshot. Best-effort: a failed write logs and is
/// otherwise ignored so monitoring I/O never takes down the trading loop.
fn write_metrics_snapshot(path: &std::path::Path, value: &serde_json::Value) {
    let json = match serde_json::to_string_pretty(value) {
        Ok(j) => j,
        Err(_) => return,
    };
    let tmp = path.with_extension("json.tmp");
    if std::fs::write(&tmp, &json)
        .and_then(|_| std::fs::rename(&tmp, path))
        .is_err()
    {
        // Fall back to a direct write; if that also fails, log once and move on.
        if std::fs::write(path, &json).is_err() {
            eprintln!("metrics snapshot write failed: {}", path.display());
        }
    }
}

fn write_metrics_json(
    path: &std::path::Path,
    metrics: &Metrics,
    tickers: &[String],
    args: &Args,
) -> Result<(), Box<dyn Error>> {
    let value = serde_json::json!({
        "duration_secs": metrics.duration.as_secs_f64(),
        "tickers": tickers,
        "live_submit": args.live_submit,
        "raw_events": metrics.raw_events,
        "normalized_events": metrics.normalized_events,
        "normalize_errors": metrics.normalize_errors,
        "sequence_gaps": metrics.sequence_gaps,
        "strategy_errors": metrics.strategy_errors,
        "decisions": metrics.decisions,
        "cancels": metrics.cancels,
        "intents_approved": metrics.intents_approved,
        "intents_rejected_by_risk": metrics.intents_rejected_by_risk,
        "gateway_acks": metrics.gateway_acks,
        "gateway_errors": metrics.gateway_errors,
        "gateway_stale_drops": metrics.gateway_stale_drops,
        "intent_queue_shed_non_cancel": metrics.intent_queue_shed_non_cancel,
        "own_fills": metrics.own_fills,
        "duplicate_own_fills": metrics.duplicate_own_fills,
        "own_order_updates": metrics.own_order_updates,
        "private_event_errors": metrics.private_event_errors,
        "ws_errors": metrics.ws_errors,
        "by_channel": metrics.by_channel,
        "risk_reject_reasons": metrics.risk_reject_reasons,
    });
    std::fs::write(path, serde_json::to_string_pretty(&value)?)?;
    Ok(())
}
