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
use eventcontracts_feature_builder::TennisV2Snapshot;
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
use eventcontracts_model_runtime::{
    bundle_feature_schema_version, OnnxScorer, OutputSelect, TennisOnnxArtifact,
    TennisV2OnnxArtifact,
};
use eventcontracts_risk::{
    epoch_seconds_from_rfc3339, invalidate_quote_bbo, liquidation_unrealized_drawdown_ticks,
    record_book_bbo, record_quote_bbo, utc_day_from_epoch_secs, IntentSnapshot, RiskDecision,
    RiskGate, RiskLimits,
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
use std::io::{BufRead, BufReader, Seek, SeekFrom, Write};
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
    Input(LiveInput),
    /// The spawned WS reader task has exited — either gracefully on close
    /// or after exhausting its reconnect budget. Either way the runner can
    /// no longer receive ingest; treat this as a terminal shutdown.
    WsTerminated,
}

enum LiveInput {
    Ws(KalshiWsEnvelope),
    External(NormalizedEventRecord),
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
    /// JSONL rows containing live external probabilities. This bridges the
    /// Python weather signal generator into the Rust submit/reconcile path;
    /// rows with stale `as_of` timestamps are ignored.
    #[arg(long)]
    external_signals_jsonl: Option<PathBuf>,
    /// Source label attached to `--external-signals-jsonl` probability rows.
    #[arg(long, default_value = "open-meteo")]
    external_signal_source: String,
    /// Poll interval for tailing `--external-signals-jsonl`.
    #[arg(long, default_value_t = 250)]
    external_signals_poll_ms: u64,
    /// Maximum accepted age for an external signal row.
    #[arg(long, default_value_t = 180)]
    external_signal_max_age_secs: u64,
    /// Fail-closed schema guard (F9): when set, the live runner refuses to
    /// start unless the promoted bundle's `feature_schema_version` (from its
    /// manifest) equals this value. Stops a v1 bundle from being scored with
    /// the v2 feature vector — or vice versa — which would silently mis-shape
    /// the model input on the real-money path. Set to "2" for the v2 sleeve.
    #[arg(long)]
    expect_tennis_schema_version: Option<String>,
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
    /// Seed subscribed markets as tradable at startup. Use when the operator
    /// selected explicit open tickers and the lifecycle stream does not replay
    /// their current state on subscribe; later lifecycle events still override
    /// this seed and can suspend/cancel.
    #[arg(long, default_value_t = false)]
    seed_open_market_state_on_start: bool,
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

        let event =
            tennis_prediction_event(&row.market_id, &row.source, 0.612345, false, 7).unwrap();
        assert_eq!(event.event_kind, "external");
        assert_eq!(event.provenance.source, "tennis_xgboost_onnx");
        assert!(event.payload_json.contains("\"market_id\":\"KXTENNIS-M1\""));
        assert!(event
            .payload_json
            .contains("\"player_1_win_probability\":\"0.612345\""));
        assert!(event
            .payload_json
            .contains("\"model_confidence\":\"0.612345\""));
        assert!(event.payload_json.contains("\"odds_present\":false"));
        event.validate().unwrap();
    }

    #[test]
    fn weather_external_signal_row_flattens_into_probability_event() {
        let now = rfc3339_now();
        let line = format!(
            r#"{{"as_of":"{now}","instrument":"KXHIGHMIA-26MAY31-B94.5","implied_prob":0.612345}}"#
        );
        let event = external_signal_row_event(&line, "open-meteo", 3, 180)
            .unwrap()
            .expect("fresh event");

        assert_eq!(event.event_kind, "external");
        assert_eq!(event.provenance.source, "open-meteo");
        assert!(event
            .payload_json
            .contains("\"market_id\":\"KXHIGHMIA-26MAY31-B94.5\""));
        assert!(event.payload_json.contains("\"implied_prob\":\"0.612345\""));
        let parsed = StrategyEvent::from_record(&event).unwrap();
        assert!(matches!(
            parsed,
            StrategyEvent::ExternalProbability {
                source,
                market_id,
                probability,
                ..
            } if source == "open-meteo"
                && market_id == "KXHIGHMIA-26MAY31-B94.5"
                && probability.ticks() == 612_345
        ));
    }

    #[test]
    fn tennis_v2_snapshot_row_flattens_and_parses() {
        let row: TennisV2SnapshotRow = serde_json::from_str(
            r#"{
                "market_id": "KXTENNIS-V2",
                "surface": "Clay",
                "tourney_level": "G",
                "best_of": 5,
                "round": "QF",
                "p1_elo": 1850.0,
                "p2_elo": 1600.0,
                "p1_elo_blend": 1870.0,
                "p2_elo_blend": 1580.0,
                "p1_hand": "L",
                "p2_hand": "R"
            }"#,
        )
        .unwrap();
        assert_eq!(row.market_id, "KXTENNIS-V2");
        assert_eq!(row.source, "tennis_xgboost_onnx");
        assert_eq!(row.snapshot.round, "QF");
        // unspecified fields fall back to the v2 priors.
        assert!((row.snapshot.p1_serve_won - 0.63).abs() < 1e-9);
    }

    #[test]
    fn schema_version_gate_passes_on_match_and_when_unset() {
        // No operator expectation → guard is a no-op for any bundle version.
        assert!(check_tennis_schema_version("2", None).is_ok());
        assert!(check_tennis_schema_version("1", None).is_ok());
        assert!(check_tennis_schema_version("", None).is_ok());
        // Expectation met → ok.
        assert!(check_tennis_schema_version("2", Some("2")).is_ok());
    }

    #[test]
    fn schema_version_gate_fails_closed_on_mismatch_or_missing() {
        // v1 bundle promoted under a v2 expectation must hard-fail — exactly the
        // "feature width mismatch" the live-capital audit flagged (F9).
        let err = check_tennis_schema_version("1", Some("2")).unwrap_err();
        assert!(err.contains("expected `2`"), "{err}");
        assert!(err.contains('1'), "{err}");
        // A bundle whose manifest carries no version, under an expectation, also
        // fails closed rather than silently defaulting to the v1 builder.
        let err = check_tennis_schema_version("", Some("2")).unwrap_err();
        assert!(err.contains("missing manifest"), "{err}");
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

    fn f7_limits() -> RiskLimits {
        RiskLimits {
            max_order_notional: "100".into(),
            max_position_notional: "1000".into(),
            max_daily_loss: "150".into(),
            max_open_orders: 10,
            max_gross_exposure: "1000".into(),
            currency: "USD".into(),
            max_market_data_age_secs: 60,
        }
    }

    /// Build a validated `NormalizedEventRecord` for a private-channel event,
    /// modeled on the live WS normalization so `apply_private_venue_event` sees
    /// exactly the record shape it gets in production.
    fn private_event_record(
        event_kind: &str,
        seq: u64,
        payload: serde_json::Value,
    ) -> NormalizedEventRecord {
        let event_id = format!("{event_kind}:{seq}");
        let payload_json = payload.to_string();
        let provenance = EventProvenance {
            source: "kalshi".into(),
            channel: event_kind.into(),
            schema_version: "normalized-event-v1".into(),
            venue: Some("kalshi".into()),
            source_sequence: Some(seq.to_string()),
            normalization_version: "kalshi-ws-v1".into(),
            metadata: Metadata::new(),
        };
        let digest = canonical_sha256(&serde_json::json!({
            "event_id": event_id.clone(),
            "event_kind": event_kind,
            "payload_json": payload_json.clone(),
            "provenance": provenance.clone(),
        }))
        .unwrap();
        let event = NormalizedEventRecord {
            event_id: event_id.clone(),
            event_kind: event_kind.into(),
            payload_json,
            provenance,
            audit: AuditStamp {
                object_id: event_id,
                object_kind: "normalized_event".into(),
                schema_version: "normalized-event-v1".into(),
                produced_at: "2026-05-26T12:00:00Z".into(),
                producer: "test".into(),
                canonical_sha256: digest,
                parent_ids: vec![],
                trace_id: None,
                metadata: Metadata::new(),
            },
        };
        event.validate().unwrap();
        event
    }

    fn seed_resting_buy_yes(gateway: &mut DryRunGateway<BoxedVenue>, client_order_id: &str) {
        use eventcontracts_gateway::RestingOrderSnapshot;
        use eventcontracts_oms::{OrderState, OutcomeSide, Side, TimeInForce};
        gateway
            .adopt_resting_order(RestingOrderSnapshot {
                client_order_id: client_order_id.into(),
                venue_order_id: Some(format!("v-{client_order_id}")),
                instrument_id: "kalshi:M-1".into(),
                outcome_side: OutcomeSide::Yes,
                side: Side::Buy,
                price: "0.50".into(),
                quantity: "10".into(),
                filled_quantity: "0".into(),
                time_in_force: TimeInForce::Ioc,
                // A live BUY acked by the venue and resting on the book — the
                // state an own-fill arrives against.
                state: OrderState::Acked,
                updated_at: "2026-05-26T12:00:00Z".into(),
                observed_at: "2026-05-26T12:00:00Z".into(),
                reject_reason: None,
            })
            .expect("seed resting order");
    }

    /// F7 end-to-end seam: an own-fill arriving on the private WS channel must
    /// flow `apply_private_venue_event` -> `project_event` -> `gateway.apply_fill`
    /// and be counted in metrics. This is the exact path with real money on it.
    /// (The cash/position/daily-loss arithmetic of `apply_fill` itself is covered
    /// by the gateway crate's
    /// `live_path_async_submit_then_fills_track_cash_and_daily_loss`; here we
    /// prove the live-runner private-event wiring around it, plus WS-replay
    /// dedupe and the non-private passthrough.)
    #[tokio::test]
    async fn own_fill_event_flows_through_apply_private_venue_event_into_sleeve_state() {
        let venue: BoxedVenue = Box::new(RecordingVenueClient::new());
        let mut gateway = DryRunGateway::new(RiskGate::new(f7_limits()), venue);
        let mut metrics = Metrics::default();
        seed_resting_buy_yes(&mut gateway, "c-1");

        let own_fill = private_event_record(
            "own_fill",
            1,
            serde_json::json!({
                "client_order_id": "c-1",
                "instrument": "kalshi:M-1",
                "fill_id": "f-1",
                "price": "0.50",
                "quantity": "10",
                "fee": "0.01",
                "remaining_quantity": "0",
            }),
        );

        let halt = apply_private_venue_event(&own_fill, &mut gateway, &mut metrics).unwrap();
        assert!(
            !halt,
            "a known own-fill must not trigger a reconciliation halt"
        );
        assert_eq!(
            metrics.own_fills, 1,
            "the fill must be counted exactly once"
        );
        assert_eq!(metrics.duplicate_own_fills, 0);
        assert_eq!(metrics.private_event_errors, 0);

        // Idempotency: replaying the same fill_id is deduped, not double-counted.
        let halt = apply_private_venue_event(&own_fill, &mut gateway, &mut metrics).unwrap();
        assert!(!halt);
        assert_eq!(metrics.own_fills, 1);
        assert_eq!(metrics.duplicate_own_fills, 1);

        // A non-private (quote) event is a passthrough: not handled here.
        let quote = private_event_record(
            "quote",
            2,
            serde_json::json!({ "instrument": "kalshi:M-1", "bid": "0.49", "ask": "0.51" }),
        );
        assert!(!apply_private_venue_event(&quote, &mut gateway, &mut metrics).unwrap());
    }

    /// F7 safety path: an own-fill for an order the OMS has never seen must halt
    /// the live run for reconciliation rather than silently dropping a real fill.
    #[tokio::test]
    async fn own_fill_for_unknown_order_halts_for_reconciliation() {
        let venue: BoxedVenue = Box::new(RecordingVenueClient::new());
        let mut gateway = DryRunGateway::new(RiskGate::new(f7_limits()), venue);
        let mut metrics = Metrics::default();

        let own_fill = private_event_record(
            "own_fill",
            1,
            serde_json::json!({
                "client_order_id": "unknown-cid",
                "instrument": "kalshi:M-1",
                "fill_id": "f-9",
                "price": "0.50",
                "quantity": "10",
                "fee": "0.01",
                "remaining_quantity": "0",
            }),
        );

        let halt = apply_private_venue_event(&own_fill, &mut gateway, &mut metrics).unwrap();
        assert!(
            halt,
            "an own-fill for an unknown order must halt for reconciliation"
        );
        assert_eq!(metrics.own_fills, 0);
        assert_eq!(metrics.private_event_errors, 1);
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
        // $8 throwaway first-live envelope (see the sleeve TOML header): one
        // 5-contract order, whole-bankroll caps, single open order. Update in
        // lockstep if the committed sleeve is re-funded.
        assert_eq!(limits.max_order_notional, "5");
        assert_eq!(limits.max_position_notional, "8");
        assert_eq!(limits.max_daily_loss, "8");
        assert_eq!(limits.max_open_orders, 1);
        assert_eq!(limits.max_gross_exposure, "8");
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
                // F6: arm the Rust available-cash gate with venue truth. Until
                // this is set the gate is inert (None); the notional/gross caps
                // are the only bound. The gateway keeps it in sync on fills.
                gateway.sleeve_state.available_cash_ticks = Some(balance.available_ticks());
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
                        // Fail-closed, but self-diagnosing: dump the full venue
                        // payload (including any fields we do not model, captured
                        // in `extra`) so the operator can tell a stale/market
                        // order from a Kalshi field rename before canceling.
                        eprintln!(
                            "reconcile-on-start: un-adoptable venue order {} — raw venue payload follows:\n{order:#?}",
                            order.order_id
                        );
                        format!(
                            "reconcile-on-start: cannot adopt venue order {}; {e}; \
                             inspect the payload dump above, then rerun with \
                             --cancel-orphans-on-start to clear venue truth",
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
    if args.seed_open_market_state_on_start {
        let now = rfc3339_now();
        for ticker in &tickers {
            let instrument = format!("kalshi:{ticker}");
            gateway.apply_market_state(
                &instrument,
                MarketState::Opened,
                Some("startup-open-seed"),
                &now,
            )?;
        }
        eprintln!(
            "seeded {} subscribed market(s) as Opened at startup; lifecycle events still override",
            tickers.len()
        );
    }
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
        let (events, missing_odds) = score_tennis_snapshot_file(
            artifact_dir,
            snapshot_path,
            args.expect_tennis_schema_version.as_deref(),
        )?;
        eprintln!(
            "scored {} tennis snapshots from {}",
            events.len(),
            snapshot_path.display()
        );
        metrics.tennis_snapshots_scored += events.len() as u64;
        metrics.tennis_snapshots_missing_odds += missing_odds as u64;
        if missing_odds > 0 {
            // F8: a snapshot with no bookmaker odds is scored, but if the sleeve
            // sets require_odds_present the order is suppressed downstream. Make
            // that loud here and in the metrics snapshot so a zero-order run is
            // never silently attributed to "no edge".
            eprintln!(
                "WARNING: {missing_odds} of {} tennis snapshots carried no bookmaker odds; \
                 with require_odds_present=true these markets will NOT trade. \
                 Wire an odds feed (see tennis-build-snapshots / docs) before funding.",
                events.len(),
            );
        }
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
    let (env_tx, mut env_rx) = mpsc::channel::<LiveInput>(WS_INGEST_CHANNEL_CAPACITY);
    let (reconnect_tx, reconnect_rx) = mpsc::channel::<&'static str>(4);
    let ws_errors_counter = Arc::new(AtomicU64::new(0));
    let external_signal_errors_counter = Arc::new(AtomicU64::new(0));
    let ws_task = {
        let channels_owned: Vec<String> = channels.iter().map(|&s| s.to_string()).collect();
        let tickers_owned: Vec<String> = tickers.clone();
        let ws_errors = ws_errors_counter.clone();
        let ws_tx = env_tx.clone();
        tokio::spawn(async move {
            ws_reader_task(
                ws,
                channels_owned,
                tickers_owned,
                ws_tx,
                reconnect_rx,
                ws_errors,
            )
            .await
        })
    };
    let external_signal_task = args.external_signals_jsonl.as_ref().map(|path| {
        let tx = env_tx.clone();
        let source = args.external_signal_source.clone();
        let path = path.clone();
        let errors = external_signal_errors_counter.clone();
        let max_age_secs = args.external_signal_max_age_secs;
        let poll_ms = args.external_signals_poll_ms;
        tokio::spawn(async move {
            external_signal_reader_task(path, source, tx, max_age_secs, poll_ms, errors).await
        })
    });
    drop(env_tx);

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
                Ok(Some(input)) => WsLoopResult::Input(input),
                Ok(None) => WsLoopResult::WsTerminated,
            },
        };
        let recv_at = Instant::now();
        let input_msg = match recv_t {
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
                    // Keep the mark-to-market drawdown current during quiet
                    // periods so the snapshot reflects the true daily-loss
                    // headroom even when no event is flowing (F3).
                    gateway.sleeve_state.unrealized_drawdown_loss =
                        liquidation_unrealized_drawdown_ticks(&gateway.sleeve_state);
                    let live = LiveStatus {
                        elapsed_secs: start_time.elapsed().as_secs_f64(),
                        kill_switch_engaged: gateway.sleeve_state.kill_switch_engaged,
                        daily_realized_loss_ticks: gateway.sleeve_state.daily_realized_loss,
                        unrealized_drawdown_loss_ticks: gateway
                            .sleeve_state
                            .unrealized_drawdown_loss,
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
            WsLoopResult::Input(LiveInput::WsTerminated) => {
                eprintln!("ws reader task exited; halting main loop");
                gateway.sleeve_state.kill_switch_engaged = true;
                if args.live_submit {
                    cancel_all_or_log_async(&mut gateway, "ws-terminated").await;
                }
                break;
            }
            WsLoopResult::Input(input) => input,
            WsLoopResult::WsTerminated => {
                eprintln!("ws reader task exited; halting main loop");
                gateway.sleeve_state.kill_switch_engaged = true;
                if args.live_submit {
                    cancel_all_or_log_async(&mut gateway, "ws-terminated").await;
                }
                break;
            }
        };

        let (normalized, normalize_done) = match input_msg {
            LiveInput::Ws(env_msg) => {
                metrics.raw_events += 1;
                metrics
                    .by_channel
                    .entry(env_msg.msg_type.clone())
                    .and_modify(|c| *c += 1)
                    .or_insert(1);

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
                (normalized, normalize_done)
            }
            LiveInput::External(normalized) => {
                metrics.external_signal_events += 1;
                metrics
                    .by_channel
                    .entry("external-signals-jsonl".into())
                    .and_modify(|c| *c += 1)
                    .or_insert(1);
                (normalized, recv_at)
            }
            LiveInput::WsTerminated => unreachable!("handled above"),
        };

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
            if let (Some((bid, bid_qty)), Some((no_bid, ask_qty))) = (bids.first(), asks.first()) {
                let bid_t = bid.ticks() / 100;
                // runtime-hot carries Kalshi `no` book levels as the ask-side
                // vector. Convert the top NO bid into an executable YES ask
                // before recording side-specific last-look BBO.
                let ask_t = 10_000_i64.saturating_sub(no_bid.ticks() / 100);
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
        // F3: re-mark open positions to the freshest executable book before the
        // risk gate runs this event's decisions. This folds liquidation-mark
        // drawdown into `max_daily_loss` so a held position bleeding intraday
        // counts toward the stop before it is realized at settlement.
        gateway.sleeve_state.unrealized_drawdown_loss =
            liquidation_unrealized_drawdown_ticks(&gateway.sleeve_state);
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
    if let Some(task) = external_signal_task {
        task.abort();
        let _ = task.await;
    }
    metrics.ws_errors = metrics
        .ws_errors
        .saturating_add(ws_errors_counter.load(Ordering::Relaxed));
    metrics.external_signal_errors = metrics
        .external_signal_errors
        .saturating_add(external_signal_errors_counter.load(Ordering::Relaxed));
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
    env_tx: mpsc::Sender<LiveInput>,
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
                        if env_tx.send(LiveInput::Ws(env)).await.is_err() {
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
    let _ = env_tx.send(LiveInput::WsTerminated).await;
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

#[derive(Debug, Deserialize)]
struct ExternalSignalJsonlRow {
    as_of: String,
    #[serde(alias = "market_id")]
    instrument: String,
    #[serde(alias = "probability")]
    implied_prob: f64,
}

async fn external_signal_reader_task(
    path: PathBuf,
    source: String,
    tx: mpsc::Sender<LiveInput>,
    max_age_secs: u64,
    poll_ms: u64,
    errors: Arc<AtomicU64>,
) {
    let mut offset = 0_u64;
    let mut seq = 0_u64;
    let poll = Duration::from_millis(poll_ms.max(25));
    loop {
        match read_external_signal_lines(&path, &source, &mut offset, &mut seq, max_age_secs) {
            Ok(events) => {
                for event in events {
                    if tx.send(LiveInput::External(event)).await.is_err() {
                        return;
                    }
                }
            }
            Err(e) => {
                errors.fetch_add(1, Ordering::Relaxed);
                eprintln!("external signal tail error ({}): {e}", path.display());
            }
        }
        tokio::time::sleep(poll).await;
    }
}

fn read_external_signal_lines(
    path: &std::path::Path,
    source: &str,
    offset: &mut u64,
    seq: &mut u64,
    max_age_secs: u64,
) -> Result<Vec<NormalizedEventRecord>, Box<dyn Error + Send + Sync>> {
    let mut file = File::open(path)?;
    let len = file.metadata()?.len();
    if len < *offset {
        *offset = 0;
    }
    file.seek(SeekFrom::Start(*offset))?;
    let mut reader = BufReader::new(file);
    let mut line = String::new();
    let mut events = Vec::new();
    loop {
        line.clear();
        let bytes = reader.read_line(&mut line)?;
        if bytes == 0 {
            break;
        }
        *offset = (*offset).saturating_add(bytes as u64);
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        *seq = seq.saturating_add(1);
        match external_signal_row_event(trimmed, source, *seq, max_age_secs) {
            Ok(Some(event)) => events.push(event),
            Ok(None) => {}
            Err(e) => {
                eprintln!("external signal row skipped: {e}");
            }
        }
    }
    Ok(events)
}

fn external_signal_row_event(
    line: &str,
    source: &str,
    seq: u64,
    max_age_secs: u64,
) -> Result<Option<NormalizedEventRecord>, Box<dyn Error + Send + Sync>> {
    let row: ExternalSignalJsonlRow = serde_json::from_str(line)?;
    if !external_signal_fresh(&row.as_of, max_age_secs) {
        return Ok(None);
    }
    if !(0.0..=1.0).contains(&row.implied_prob) {
        return Ok(None);
    }
    let market_id = row
        .instrument
        .strip_prefix("kalshi:")
        .unwrap_or(row.instrument.as_str());
    let probability = format!("{:.6}", row.implied_prob);
    external_probability_event(market_id, source, &probability, &row.as_of, seq).map(Some)
}

fn external_signal_fresh(as_of: &str, max_age_secs: u64) -> bool {
    let signal_epoch = epoch_seconds_from_rfc3339(as_of);
    if signal_epoch == 0 {
        return false;
    }
    let now = OffsetDateTime::now_utc().unix_timestamp();
    let age = now.saturating_sub(signal_epoch);
    age >= -5 && age <= max_age_secs as i64
}

fn external_probability_event(
    market_id: &str,
    source: &str,
    probability: &str,
    as_of: &str,
    seq: u64,
) -> Result<NormalizedEventRecord, Box<dyn Error + Send + Sync>> {
    let now = rfc3339_now();
    let payload = serde_json::json!({
        "source": source,
        "market_id": market_id,
        "implied_prob": probability,
        "as_of": as_of,
    });
    let event_id = format!("external-jsonl:{source}:{market_id}:{seq}");
    let payload_json = payload.to_string();
    let provenance = EventProvenance {
        source: source.to_string(),
        channel: "external-signals-jsonl".into(),
        schema_version: "normalized-event-v1".into(),
        venue: Some("kalshi".into()),
        source_sequence: Some(seq.to_string()),
        normalization_version: "external-jsonl-v1".into(),
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
            producer: "live-runner-external-jsonl".into(),
            canonical_sha256: digest,
            parent_ids: vec![],
            trace_id: None,
            metadata: Metadata::new(),
        },
    };
    event.validate()?;
    Ok(event)
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

#[derive(Debug, Deserialize)]
struct TennisV2SnapshotRow {
    market_id: String,
    #[serde(default = "default_tennis_prediction_source")]
    source: String,
    #[serde(flatten)]
    snapshot: TennisV2Snapshot,
}

/// Score offline tennis snapshots into external prediction events. The
/// promoted bundle's `feature_schema.json` decides which feature contract is
/// in force: `schema_version = "2"` routes to the 34-feature v2 builder,
/// anything else to the v1 (20-feature) builder. This keeps a v1 bundle
/// working untouched while letting a v2 bundle deploy by swapping the artifact.
fn score_tennis_snapshot_file(
    artifact_dir: &std::path::Path,
    snapshot_path: &std::path::Path,
    expect_schema_version: Option<&str>,
) -> Result<(Vec<NormalizedEventRecord>, usize), Box<dyn Error>> {
    let version = bundle_feature_schema_version(artifact_dir).unwrap_or_default();
    // F9 fail-closed gate: refuse to trade if the operator's declared schema
    // version disagrees with the promoted bundle. A mismatch would mis-shape
    // the live feature vector (v2 = 34 features, v1 = 20).
    if let Err(reason) = check_tennis_schema_version(&version, expect_schema_version) {
        return Err(format!(
            "{reason} (promoted bundle at {}). Refusing to start the live run.",
            artifact_dir.display(),
        )
        .into());
    }
    if version == "2" {
        eprintln!("tennis scoring: feature_schema v2 (34 features)");
        score_tennis_v2_snapshot_file(artifact_dir, snapshot_path)
    } else {
        eprintln!("tennis scoring: feature_schema v1 (20 features)");
        score_tennis_v1_snapshot_file(artifact_dir, snapshot_path)
    }
}

/// F9 fail-closed schema guard, factored out as a pure function so it can be
/// unit-tested without a real ONNX bundle on disk. `found` is the bundle's
/// declared `feature_schema_version` (empty string if its manifest carried
/// none); `expected` is the operator's `--expect-tennis-schema-version`. When
/// the operator declares an expectation, a disagreement is a hard error — the
/// live runner must not score a v1 bundle with the v2 vector, or vice versa.
/// When the operator declares no expectation, the guard is a no-op (the
/// version still drives builder selection downstream).
fn check_tennis_schema_version(found: &str, expected: Option<&str>) -> Result<(), String> {
    let Some(expected) = expected else {
        return Ok(());
    };
    if found == expected {
        return Ok(());
    }
    let found_label = if found.is_empty() {
        "<missing manifest feature_schema_version>"
    } else {
        found
    };
    Err(format!(
        "tennis bundle feature_schema_version mismatch: operator expected `{expected}` \
         (--expect-tennis-schema-version) but the bundle declares `{found_label}`"
    ))
}

fn score_tennis_v1_snapshot_file(
    artifact_dir: &std::path::Path,
    snapshot_path: &std::path::Path,
) -> Result<(Vec<NormalizedEventRecord>, usize), Box<dyn Error>> {
    let mut artifact = TennisOnnxArtifact::load_bundle(artifact_dir)?;
    let file = File::open(snapshot_path)?;
    let reader = BufReader::new(file);
    let mut events = Vec::new();
    let mut missing_odds = 0usize;
    for (index, line) in reader.lines().enumerate() {
        let line = line?;
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        let row: TennisSnapshotRow = serde_json::from_str(trimmed)?;
        let probability = artifact.predict_snapshot(&row.snapshot)?;
        let odds_present = tennis_v1_odds_present(&row.snapshot);
        if !odds_present {
            missing_odds += 1;
        }
        events.push(tennis_prediction_event(
            &row.market_id,
            &row.source,
            probability,
            odds_present,
            index as u64 + 1,
        )?);
    }
    Ok((events, missing_odds))
}

fn score_tennis_v2_snapshot_file(
    artifact_dir: &std::path::Path,
    snapshot_path: &std::path::Path,
) -> Result<(Vec<NormalizedEventRecord>, usize), Box<dyn Error>> {
    let mut artifact = TennisV2OnnxArtifact::load_bundle(artifact_dir)?;
    let file = File::open(snapshot_path)?;
    let reader = BufReader::new(file);
    let mut events = Vec::new();
    let mut missing_odds = 0usize;
    for (index, line) in reader.lines().enumerate() {
        let line = line?;
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        let row: TennisV2SnapshotRow = serde_json::from_str(trimmed)?;
        let probability = artifact.predict_snapshot(&row.snapshot)?;
        let odds_present = tennis_v2_odds_present(&row.snapshot);
        if !odds_present {
            missing_odds += 1;
        }
        events.push(tennis_prediction_event(
            &row.market_id,
            &row.source,
            probability,
            odds_present,
            index as u64 + 1,
        )?);
    }
    Ok((events, missing_odds))
}

fn tennis_prediction_event(
    market_id: &str,
    source: &str,
    probability: f32,
    odds_present: bool,
    seq: u64,
) -> Result<NormalizedEventRecord, Box<dyn Error>> {
    let now = rfc3339_now();
    let confidence = probability.max(1.0 - probability);
    let payload = serde_json::json!({
        "source": source,
        "market_id": market_id,
        "player_1_win_probability": format!("{probability:.6}"),
        "model_confidence": format!("{confidence:.6}"),
        "odds_present": odds_present,
    });
    let event_id = format!("tennis-xgboost:{market_id}:{seq}");
    let payload_json = payload.to_string();
    let provenance = EventProvenance {
        source: source.to_string(),
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

fn tennis_v1_odds_present(snapshot: &TennisMatchSnapshot) -> bool {
    snapshot.p1_decimal_odds.is_some_and(|value| value > 1.0)
        && snapshot.p2_decimal_odds.is_some_and(|value| value > 1.0)
}

fn tennis_v2_odds_present(snapshot: &TennisV2Snapshot) -> bool {
    snapshot.p1_decimal_odds.is_some_and(|value| value > 1.0)
        && snapshot.p2_decimal_odds.is_some_and(|value| value > 1.0)
}

// ---------- metrics ----------

#[derive(Default)]
struct Metrics {
    duration: Duration,
    raw_events: u64,
    normalized_events: u64,
    tennis_snapshots_scored: u64,
    tennis_snapshots_missing_odds: u64,
    external_signal_events: u64,
    external_signal_errors: u64,
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
        "external signal events:         {}",
        metrics.external_signal_events
    );
    println!(
        "external signal errors:         {}",
        metrics.external_signal_errors
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
    unrealized_drawdown_loss_ticks: i64,
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
        "unrealized_drawdown_loss_ticks": live.unrealized_drawdown_loss_ticks,
        "unrealized_drawdown_loss_usd": live.unrealized_drawdown_loss_ticks as f64 / 10_000.0,
        "live_place_attempts": live.live_place_attempts,
        "max_live_orders": args.max_live_orders,
        "tickers": tickers,
        "live_submit": args.live_submit,
        "raw_events": metrics.raw_events,
        "normalized_events": metrics.normalized_events,
        "tennis_snapshots_scored": metrics.tennis_snapshots_scored,
        "tennis_snapshots_missing_odds": metrics.tennis_snapshots_missing_odds,
        "external_signal_events": metrics.external_signal_events,
        "external_signal_errors": metrics.external_signal_errors,
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
        "tennis_snapshots_scored": metrics.tennis_snapshots_scored,
        "tennis_snapshots_missing_odds": metrics.tennis_snapshots_missing_odds,
        "external_signal_events": metrics.external_signal_events,
        "external_signal_errors": metrics.external_signal_errors,
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
