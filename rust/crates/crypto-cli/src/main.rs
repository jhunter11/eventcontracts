//! Walk-forward backtest CLI for the crypto signal ensemble.
//!
//! Mirrors `scripts/run_real_backtest.py` in Rust. Pulls Kalshi
//! bracket candlesticks + Deribit OHLC / DVOL via public REST and
//! prints a JSON summary of decisions, fills, fees, and per-source
//! attribution.

use std::collections::BTreeMap;

use clap::Parser;
use eventcontracts_crypto_backtest::{
    fill_decision, CohortBacktestResult, SizingPolicy, WalkForwardReport,
};
use eventcontracts_crypto_domain::{
    AlertSeverity, ClientOrderId, InstrumentId, OrderSide, OrderType, PlaceOrder, Signal,
    StrategyDecision, TimeInForce, Venue,
};
use eventcontracts_crypto_historical::{
    candles_to_quotes, HistoricalLoader, KalshiMarket, KalshiMarketKind, QuoteSample,
};
use eventcontracts_crypto_signals::{
    bracket_vol_signals, combine_signals, parity_signals, skew_signals, vol_surface_signals,
    BracketVolState, ParityState, SkewState, VolSurfaceState,
};
use rust_decimal::Decimal;
use serde_json::json;

#[derive(Parser, Debug)]
#[command(
    name = "crypto-backtest",
    about = "Walk-forward settlement-PnL backtest of the crypto signal ensemble."
)]
struct Cli {
    /// One or more Kalshi expiry tokens (e.g. 26MAY2508).
    #[arg(long, required = true, num_args = 1..)]
    expiries: Vec<String>,

    #[arg(long, default_value = "KXBTC")]
    series_ticker: String,

    /// Restrict Kalshi markets to the cluster within this many $ of opening spot.
    #[arg(long, default_value_t = 1000i64)]
    atm_radius: i64,

    #[arg(long, default_value = "parity,bracket_vol,vol_surface,skew")]
    enabled_sources: String,

    #[arg(long, default_value = "1")]
    min_confluence: usize,

    #[arg(long, default_value = "100")]
    min_edge_bps: String,

    #[arg(long, default_value = "5000")]
    max_spread_bps: String,

    /// Sizing rule: flat-contracts | fixed-premium | fixed-payout.
    #[arg(long, default_value = "fixed-premium")]
    sizing: String,

    #[arg(long, default_value = "1")]
    sizing_dollars: String,

    /// Default contract count for flat sizing.
    #[arg(long, default_value = "5")]
    size: String,

    /// Cap each cohort at one fill per market — avoids signal-stacking inflation.
    #[arg(long)]
    one_fill_per_market: bool,
}

fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::from_default_env())
        .with_writer(std::io::stderr)
        .init();
    let cli = Cli::parse();
    let min_edge_bps: Decimal = cli.min_edge_bps.parse()?;
    let max_spread_bps: Decimal = cli.max_spread_bps.parse()?;
    let sizing = parse_sizing(&cli.sizing, &cli.sizing_dollars, &cli.size)?;

    let mut loader = HistoricalLoader::new()?;
    let mut report = WalkForwardReport::default();
    let mut per_source_pnl: BTreeMap<String, Decimal> = BTreeMap::new();
    let mut decisions_counter: BTreeMap<String, u64> = BTreeMap::new();
    let mut per_market_orders: BTreeMap<String, u64> = BTreeMap::new();

    for expiry in &cli.expiries {
        tracing::info!(%expiry, "loading cohort");
        let markets =
            loader.list_markets(&cli.series_ticker, "settled", Some(expiry), 20)?;
        if markets.is_empty() {
            tracing::warn!(%expiry, "no settled markets");
            continue;
        }
        let settlement =
            loader.fetch_cohort_settlement(&cli.series_ticker, expiry, 20)?;
        let cohort = run_cohort(
            &mut loader,
            &cli.series_ticker,
            markets,
            settlement,
            &cli.enabled_sources,
            cli.min_confluence,
            min_edge_bps,
            max_spread_bps,
            &sizing,
            cli.one_fill_per_market,
            Decimal::from(cli.atm_radius),
        )?;
        tracing::info!(
            %expiry,
            fills = cohort.fills.len(),
            pnl = %cohort.total_pnl(),
            fees = %cohort.total_fees(),
            win_rate = format!("{:.2}", cohort.win_rate()),
            "cohort done"
        );
        for fill in &cohort.fills {
            if fill.settled {
                *per_market_orders
                    .entry(fill.market_id.clone())
                    .or_insert(0) += 1;
                if fill.sources.is_empty() {
                    *per_source_pnl
                        .entry("unattributed".into())
                        .or_insert(Decimal::ZERO) += fill.pnl_total;
                } else {
                    let share = fill.pnl_total
                        / Decimal::from(fill.sources.len() as i64);
                    for src in &fill.sources {
                        *per_source_pnl
                            .entry(src.clone())
                            .or_insert(Decimal::ZERO) += share;
                    }
                }
            }
        }
        report.cohorts.push(cohort);
    }

    // Decisions counter is populated inside run_cohort via the
    // verdict pipeline; for the JSON output we recompute from fills.
    decisions_counter.insert(
        "fills_total".into(),
        report.total_fills() as u64,
    );

    let summary = json!({
        "sizing": cli.sizing,
        "sizing_dollars": cli.sizing_dollars,
        "cohorts_run": report.cohorts.len(),
        "total_fills": report.total_fills(),
        "total_settled_fills": report.total_settled_fills(),
        "total_pnl": report.total_pnl().to_string(),
        "total_fees": report.total_fees().to_string(),
        "net_pnl": (report.total_pnl() - report.total_fees()).to_string(),
        "win_rate": report.win_rate(),
        "per_source_pnl": per_source_pnl
            .iter()
            .map(|(k, v)| (k.clone(), v.to_string()))
            .collect::<BTreeMap<_, _>>(),
        "decisions": decisions_counter,
        "top_markets_by_fill_count": per_market_orders
            .iter()
            .take(10)
            .map(|(k, v)| (k.clone(), *v))
            .collect::<BTreeMap<_, _>>(),
    });
    println!("{}", serde_json::to_string_pretty(&summary)?);
    Ok(())
}

fn parse_sizing(mode: &str, dollars: &str, size: &str) -> anyhow::Result<SizingPolicy> {
    Ok(match mode {
        "flat-contracts" => SizingPolicy::FlatContracts { contracts: size.parse()? },
        "fixed-premium" => SizingPolicy::FixedPremium { dollars: dollars.parse()? },
        "fixed-payout" => SizingPolicy::FixedPayout { dollars: dollars.parse()? },
        other => anyhow::bail!("unknown sizing mode {other}"),
    })
}

#[allow(clippy::too_many_arguments)]
fn run_cohort(
    loader: &mut HistoricalLoader,
    series_ticker: &str,
    mut markets: Vec<KalshiMarket>,
    settlement: eventcontracts_crypto_historical::CohortSettlement,
    enabled: &str,
    min_confluence: usize,
    min_edge_bps: Decimal,
    max_spread_bps: Decimal,
    sizing: &SizingPolicy,
    one_fill_per_market: bool,
    atm_radius: Decimal,
) -> anyhow::Result<CohortBacktestResult> {
    // Open / close window from the market metadata.
    let expiry_at = markets.iter().map(|m| m.close_time).max().unwrap();
    let open_at = markets.iter().map(|m| m.open_time).min().unwrap();
    let start_ts = open_at.timestamp();
    let end_ts = expiry_at.timestamp();
    let start_ms = start_ts * 1000;
    let end_ms = end_ts * 1000;

    // Deribit data first so we know opening spot for the ATM filter.
    let spot_series = loader.fetch_deribit_ohlc("BTC-PERPETUAL", start_ms, end_ms, "1")?;
    let dvol_series = loader.fetch_deribit_dvol("BTC", start_ms, end_ms, 60)?;

    if let Some(first) = spot_series.first() {
        let opening_spot = first.close;
        let radius = atm_radius;
        let between: Vec<_> = markets
            .iter()
            .filter(|m| matches!(m.kind, KalshiMarketKind::Between))
            .cloned()
            .collect();
        let above: Vec<_> = markets
            .iter()
            .filter(|m| matches!(m.kind, KalshiMarketKind::Above))
            .cloned()
            .collect();
        let below: Vec<_> = markets
            .iter()
            .filter(|m| matches!(m.kind, KalshiMarketKind::Below))
            .cloned()
            .collect();
        let within = |m: &KalshiMarket| {
            if let Some(l) = m.lower {
                if (l - opening_spot).abs() <= radius * Decimal::TWO {
                    return true;
                }
            }
            if let Some(u) = m.upper {
                if (u - opening_spot).abs() <= radius * Decimal::TWO {
                    return true;
                }
            }
            false
        };
        if !between.is_empty() {
            let kept_between: Vec<_> = between.into_iter().filter(|m| within(m)).collect();
            markets = below
                .into_iter()
                .chain(kept_between)
                .chain(above)
                .collect();
        } else {
            markets = below
                .into_iter()
                .chain(above.into_iter().filter(within))
                .collect();
        }
    }

    // Fetch Kalshi candlesticks for every retained market and bucket
    // by minute timestamp.
    let mut quote_timeline: Vec<QuoteSample> = Vec::new();
    for m in &markets {
        let candles = loader.fetch_candlesticks(
            series_ticker,
            &m.ticker,
            start_ts,
            end_ts,
            1,
        )?;
        quote_timeline.extend(candles_to_quotes(&m.ticker, &candles));
    }
    quote_timeline.sort_by_key(|q| q.timestamp);

    // Build state for each enabled signal source.
    let sources: Vec<&str> = enabled.split(',').map(str::trim).collect();
    let mut parity_state = build_parity_state(&markets);
    let mut bracket_vol_state = build_bracket_vol_state(&markets);
    let mut vol_state = build_vol_surface_state(&markets);
    let mut skew_state = build_skew_state(&markets);
    vol_state.expiry_at = Some(expiry_at);
    bracket_vol_state.expiry_at = Some(expiry_at);

    let venue = Venue::Kalshi;
    let mut latest_quote: BTreeMap<String, QuoteSample> = BTreeMap::new();
    let mut result = CohortBacktestResult {
        expiry_at: Some(expiry_at),
        yes_market_ticker: settlement.yes_market_ticker.clone(),
        settlement_price: settlement.settlement_price,
        ..Default::default()
    };
    let mut already_filled: std::collections::HashSet<String> = Default::default();

    // Combined event timeline: sort spot, dvol, and quotes by ts.
    enum Event {
        Spot(chrono::DateTime<chrono::Utc>, Decimal),
        Dvol(chrono::DateTime<chrono::Utc>, Decimal),
        Quote(QuoteSample),
    }
    let mut events: Vec<Event> = Vec::new();
    for s in &spot_series {
        events.push(Event::Spot(s.timestamp, s.close));
    }
    for v in &dvol_series {
        events.push(Event::Dvol(v.timestamp, v.dvol_pct));
    }
    for q in &quote_timeline {
        events.push(Event::Quote(q.clone()));
    }
    events.sort_by_key(|e| match e {
        Event::Spot(t, _) => *t,
        Event::Dvol(t, _) => *t,
        Event::Quote(q) => q.timestamp,
    });

    let weights: BTreeMap<String, Decimal> = BTreeMap::new();
    for event in events {
        match event {
            Event::Spot(_, price) => {
                vol_state.spot = Some(price);
                bracket_vol_state.spot = Some(price);
            }
            Event::Dvol(_, dvol) => {
                vol_state.sigma_annual = Some(dvol);
                bracket_vol_state.sigma_annual = Some(dvol);
            }
            Event::Quote(q) => {
                latest_quote.insert(q.market_id.clone(), q.clone());
                let mid = (q.bid + q.ask) / Decimal::TWO;
                let spread = q.ask - q.bid;
                let spread_bps = if mid > Decimal::ZERO {
                    spread / mid * Decimal::from(10_000)
                } else {
                    Decimal::ZERO
                };
                parity_state.mid_by_market.insert(q.market_id.clone(), mid);
                parity_state
                    .spread_bps_by_market
                    .insert(q.market_id.clone(), spread_bps);
                bracket_vol_state.mid_by_market.insert(q.market_id.clone(), mid);
                vol_state.mid_by_market.insert(q.market_id.clone(), mid);
                if skew_state.strikes.iter().any(|(m, _)| m == &q.market_id) {
                    skew_state.mid_by_market.insert(q.market_id.clone(), mid);
                    skew_state.spread_bps_by_market.insert(q.market_id.clone(), spread_bps);
                }

                // Run enabled sources and collect signals on every quote tick.
                let mut signals: Vec<Signal> = Vec::new();
                let now = q.timestamp;
                if sources.contains(&"parity") {
                    signals.extend(parity_signals(
                        &parity_state,
                        venue,
                        Decimal::new(15, 3),
                        max_spread_bps,
                        None,
                    ));
                }
                if sources.contains(&"bracket_vol") {
                    signals.extend(bracket_vol_signals(
                        &bracket_vol_state,
                        venue,
                        now,
                        Decimal::from(60),
                        min_edge_bps,
                        None,
                    ));
                }
                if sources.contains(&"vol_surface") {
                    signals.extend(vol_surface_signals(
                        &vol_state,
                        venue,
                        now,
                        Decimal::from(60),
                        min_edge_bps,
                        None,
                    ));
                }
                if sources.contains(&"skew") {
                    signals.extend(skew_signals(
                        &skew_state,
                        venue,
                        Decimal::new(1, 2),
                        max_spread_bps,
                        None,
                    ));
                }
                let verdicts =
                    combine_signals(&signals, &weights, min_edge_bps, min_confluence);
                for verdict in verdicts {
                    let Some(side) = verdict.side else { continue };
                    let market_id = verdict.instrument_id.market_id.clone();
                    if one_fill_per_market && already_filled.contains(&market_id) {
                        continue;
                    }
                    let decision = PlaceOrder {
                        client_order_id: ClientOrderId::new(format!(
                            "ec-{}-{}",
                            market_id, now.timestamp()
                        )),
                        instrument_id: InstrumentId::new(venue, market_id.clone()),
                        outcome_side: side,
                        order_side: OrderSide::Buy,
                        order_type: OrderType::Limit,
                        time_in_force: TimeInForce::Gtc,
                        quantity: Decimal::ONE,
                        price: None,
                        reason: format!("ensemble:{}", verdict.contributing_sources.join("|")),
                        expected_edge_bps: Some(verdict.net_edge_bps.abs()),
                    };
                    if let Some(fill) = fill_decision(
                        &decision,
                        now,
                        &latest_quote,
                        &settlement,
                        sizing,
                        &verdict.contributing_sources,
                    ) {
                        result.fills.push(fill);
                        if one_fill_per_market {
                            already_filled.insert(market_id);
                        }
                    } else {
                        result.skipped_no_quote += 1;
                    }
                }
            }
        }
    }

    // Silence unused warnings on these types when no decisions ever fire.
    let _ = StrategyDecision::NoAction(eventcontracts_crypto_domain::NoAction { reason: String::new() });
    let _ = AlertSeverity::Info;

    Ok(result)
}

fn build_parity_state(markets: &[KalshiMarket]) -> ParityState {
    let bracket_ids: Vec<String> = markets
        .iter()
        .filter(|m| matches!(m.kind, KalshiMarketKind::Between)
            || matches!(m.kind, KalshiMarketKind::Above)
            || matches!(m.kind, KalshiMarketKind::Below))
        .map(|m| m.ticker.clone())
        .collect();
    ParityState::new(bracket_ids)
}

fn build_bracket_vol_state(markets: &[KalshiMarket]) -> BracketVolState {
    let mut intervals: BTreeMap<String, (Decimal, Option<Decimal>)> = BTreeMap::new();
    for m in markets {
        let (lower, upper) = match m.kind {
            KalshiMarketKind::Between => (
                m.lower.unwrap_or(Decimal::ZERO),
                m.upper,
            ),
            KalshiMarketKind::Above => (
                m.lower.unwrap_or(Decimal::ZERO),
                None,
            ),
            KalshiMarketKind::Below => (
                Decimal::ZERO,
                m.upper,
            ),
        };
        intervals.insert(m.ticker.clone(), (lower, upper));
    }
    BracketVolState::new(intervals)
}

fn build_vol_surface_state(markets: &[KalshiMarket]) -> VolSurfaceState {
    let mut strike_map: BTreeMap<String, Decimal> = BTreeMap::new();
    for m in markets {
        if matches!(m.kind, KalshiMarketKind::Above) {
            if let Some(strike) = m.lower {
                strike_map.insert(m.ticker.clone(), strike);
            }
        }
    }
    VolSurfaceState::new(strike_map)
}

fn build_skew_state(markets: &[KalshiMarket]) -> SkewState {
    let strikes: Vec<(String, Decimal)> = markets
        .iter()
        .filter_map(|m| {
            if matches!(m.kind, KalshiMarketKind::Above) {
                Some((m.ticker.clone(), m.lower?))
            } else {
                None
            }
        })
        .collect();
    SkewState::new(strikes)
}
