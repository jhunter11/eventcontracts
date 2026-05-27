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
use eventcontracts_contracts::{AuditStamp, Contract, IntentEnvelopeRecord, Metadata};
use eventcontracts_gateway::{DecisionPayload, DryRunGateway, GatewayError, RecordingVenueClient};
use eventcontracts_kalshi::{
    normalize_ws_payload, KalshiAuth, KalshiEnvironment, KalshiRest, KalshiWsClient,
    NormalizeError,
};
use eventcontracts_risk::{IntentSnapshot, RiskDecision, RiskGate, RiskLimits, SleeveState};
use eventcontracts_runner::{
    default_registry, StrategyRuntime, StrategySpecArtifact, ThresholdStrategy,
};
use std::error::Error;
use std::path::PathBuf;
use std::time::{Duration, Instant};
use time::format_description::well_known::Rfc3339;
use time::OffsetDateTime;

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
    eprintln!("api_key_id:    {}...{}",
        &auth.api_key_id.chars().take(4).collect::<String>(),
        &auth.api_key_id.chars().rev().take(4).collect::<String>());

    // ---------- market discovery (or explicit override) ----------
    let tickers: Vec<String> = if !args.tickers.is_empty() {
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

    // ---------- ws connect + subscribe ----------
    let connect_t0 = Instant::now();
    let mut ws = KalshiWsClient::new(env.ws_url(), auth.clone());
    ws.connect().await?;
    let ticker_refs: Vec<&str> = tickers.iter().map(|s| s.as_str()).collect();
    ws.subscribe(&["ticker", "trade", "orderbook_delta"], &ticker_refs)
        .await?;
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
        let registry = default_registry();
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
    let risk = RiskGate::new(default_limits());
    let mut sleeve_state = SleeveState::default();
    let mut gateway = DryRunGateway::new(RiskGate::new(default_limits()), RecordingVenueClient::new());

    // ---------- run loop ----------
    let start_time = Instant::now();
    let deadline = start_time + Duration::from_secs(args.duration_secs);
    let mut metrics = Metrics::default();
    let mut next_corr: u64 = 0;

    eprintln!("running for {}s...", args.duration_secs);
    while Instant::now() < deadline {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            break;
        }
        let recv_t = tokio::time::timeout(remaining, ws.next_envelope()).await;
        let recv_at = Instant::now();
        let env_msg = match recv_t {
            Err(_) => break, // deadline
            Ok(Ok(Some(e))) => e,
            Ok(Ok(None)) => continue,
            Ok(Err(e)) => {
                metrics.ws_errors += 1;
                eprintln!("ws error: {e}");
                break;
            }
        };

        metrics.raw_events += 1;
        metrics.by_channel
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
                metrics.by_channel
                    .entry(format!("unsupported:{c}"))
                    .and_modify(|x| *x += 1)
                    .or_insert(1);
                continue;
            }
            Err(e) => {
                metrics.normalize_errors += 1;
                eprintln!("normalize err: {e}");
                continue;
            }
        };

        let normalize_done = Instant::now();
        metrics.normalize_latency_us
            .push((normalize_done - recv_at).as_micros() as u64);

        metrics.normalized_events += 1;

        // strategy
        let decisions = match strategy.on_event(&normalized) {
            Ok(d) => d,
            Err(e) => {
                metrics.strategy_errors += 1;
                eprintln!("strategy err: {e}");
                continue;
            }
        };
        let strat_done = Instant::now();
        metrics.strategy_latency_us
            .push((strat_done - normalize_done).as_micros() as u64);
        metrics.decisions += decisions.len() as u64;

        // wrap → risk → gateway
        for decision in decisions {
            next_corr += 1;
            let envelope = match wrap_envelope(strategy.as_ref(), next_corr, &decision) {
                Ok(e) => e,
                Err(e) => {
                    metrics.decision_wrap_errors += 1;
                    eprintln!("wrap err: {e}");
                    continue;
                }
            };

            let snap = match &decision {
                DecisionPayload::PlaceOrder {
                    client_order_id,
                    instrument_id,
                    side,
                    price,
                    quantity,
                    ..
                } => IntentSnapshot {
                    client_order_id: client_order_id.clone(),
                    instrument_id: instrument_id.clone(),
                    side: *side,
                    price: price.clone(),
                    quantity: quantity.clone(),
                },
                DecisionPayload::CancelOrder { .. } => {
                    metrics.cancels += 1;
                    continue;
                }
            };

            match risk.evaluate(&sleeve_state, &snap) {
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
                    continue;
                }
            }

            gateway.enqueue(envelope)?;
            let acks = gateway.process_batch(&rfc3339_now(), 16);
            for (_cid, res) in acks {
                match res {
                    Ok(ack) if ack.accepted => {
                        metrics.gateway_acks += 1;
                        sleeve_state.open_orders = gateway.sleeve_state.open_orders;
                    }
                    Ok(_) => metrics.gateway_errors += 1,
                    Err(GatewayError::Stale { .. }) => metrics.gateway_stale_drops += 1,
                    Err(_) => metrics.gateway_errors += 1,
                }
            }
            let dispatch_done = Instant::now();
            metrics.end_to_end_us
                .push((dispatch_done - recv_at).as_micros() as u64);
        }
    }

    ws.close().await.ok();
    metrics.duration = start_time.elapsed();

    print_report(&metrics, &tickers, &args);
    Ok(())
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
    let now = rfc3339_now();
    let decision_json = serde_json::to_string(decision)?;
    let decision_kind = match decision {
        DecisionPayload::PlaceOrder { .. } => "place_order",
        DecisionPayload::CancelOrder { .. } => "cancel_order",
    }
    .to_string();
    let envelope = IntentEnvelopeRecord {
        strategy_id: strategy.strategy_id().to_string(),
        sleeve_id: strategy.sleeve_id().to_string(),
        correlation_id: format!("{}-{n:08}", strategy.sleeve_id()),
        emitted_at: now.clone(),
        decision_kind,
        decision_json,
        priority_tier: "standard".into(),
        audit: AuditStamp {
            object_id: format!("intent-{n:08}"),
            object_kind: "intent_envelope".into(),
            schema_version: "intent-envelope-v1".into(),
            produced_at: now,
            producer: "live-runner".into(),
            canonical_sha256: "0".repeat(64),
            parent_ids: vec![],
            trace_id: None,
            metadata: Metadata::new(),
        },
    };
    envelope.validate()?;
    Ok(envelope)
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
    strategy_errors: u64,
    decision_wrap_errors: u64,
    decisions: u64,
    cancels: u64,
    intents_approved: u64,
    intents_rejected_by_risk: u64,
    risk_reject_reasons: std::collections::HashMap<String, u64>,
    gateway_acks: u64,
    gateway_stale_drops: u64,
    gateway_errors: u64,
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
    println!("tickers:                        {} ({})", tickers.len(), tickers.join(","));
    println!("strategy.buy_below:             {}", args.buy_below);
    println!();
    println!("--- counts ---");
    println!("raw ws events:                  {}  ({:.1}/s)", metrics.raw_events, rps);
    println!("normalized events:              {}  ({:.1}/s)", metrics.normalized_events, nps);
    println!("  normalize ignored ctrl msgs:  {}", metrics.normalize_ignored);
    println!("  normalize unsupported:        {}", metrics.normalize_unsupported);
    println!("  normalize errors:             {}", metrics.normalize_errors);
    println!("strategy decisions:             {}", metrics.decisions);
    println!("cancels:                        {}", metrics.cancels);
    println!("intents approved by risk:       {}", metrics.intents_approved);
    println!("intents rejected by risk:       {}", metrics.intents_rejected_by_risk);
    if !metrics.risk_reject_reasons.is_empty() {
        let mut rows: Vec<_> = metrics.risk_reject_reasons.iter().collect();
        rows.sort_by(|a, b| b.1.cmp(a.1));
        for (reason, count) in rows {
            println!("    {reason} -> {count}");
        }
    }
    println!("gateway acks (paper):           {}", metrics.gateway_acks);
    println!("gateway stale drops:            {}", metrics.gateway_stale_drops);
    println!("gateway errors:                 {}", metrics.gateway_errors);
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
