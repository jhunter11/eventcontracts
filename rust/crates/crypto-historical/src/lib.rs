//! Public REST loaders for Kalshi BTC bracket candles and Deribit
//! 1-minute spot + DVOL.
//!
//! Mirrors `python/src/eventcontracts/crypto/historical.py`. No API
//! keys: every endpoint is on the public free tier.
//!
//! The HTTP client is blocking [`reqwest`] so the loader works in
//! command-line contexts without an async runtime. The HTTP client is
//! cached per loader instance to amortize TLS setup across the many
//! per-bracket candle requests one cohort needs.

use std::collections::BTreeMap;
use std::thread::sleep;
use std::time::{Duration, Instant};

use chrono::{DateTime, NaiveDate, TimeZone, Utc};
use regex::Regex;
use rust_decimal::Decimal;
use serde::Deserialize;
use thiserror::Error;

pub const KALSHI_BASE: &str = "https://api.elections.kalshi.com/trade-api/v2";
pub const DERIBIT_BASE: &str = "https://www.deribit.com/api/v2";

#[derive(Debug, Error)]
pub enum LoaderError {
    #[error("http error: {0}")]
    Http(#[from] reqwest::Error),
    #[error("json error: {0}")]
    Json(#[from] serde_json::Error),
    #[error("api returned error: {0}")]
    Api(String),
    #[error("validation: {0}")]
    Validation(String),
}

pub type Result<T> = std::result::Result<T, LoaderError>;

// ----------------------------- Kalshi -----------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum KalshiMarketKind {
    Between,
    Above,
    Below,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct KalshiMarket {
    pub ticker: String,
    pub subtitle: String,
    pub open_time: DateTime<Utc>,
    pub close_time: DateTime<Utc>,
    pub kind: KalshiMarketKind,
    /// Lower bound (or strike for "above" markets).
    pub lower: Option<Decimal>,
    pub upper: Option<Decimal>,
    /// `"yes"` / `"no"` if settled.
    pub result: Option<String>,
}

/// Parse a Kalshi BTC-style ticker into the strike layout.
///
/// `KXBTC-26MAY2508-B77250` → between [77200, 77300]
/// `KXBTCD-26MAY2317-T80000` → above $80000 (or below — disambiguated by subtitle)
pub fn parse_ticker(ticker: &str, bracket_step: Decimal) -> Option<(KalshiMarketKind, Option<Decimal>, Option<Decimal>)> {
    let re = Regex::new(r"^[A-Z]+-[0-9A-Z]+-(?P<type>[TB])(?P<strike>[0-9.]+)$").ok()?;
    let caps = re.captures(ticker)?;
    let type_char = caps.name("type")?.as_str();
    let strike: Decimal = caps.name("strike")?.as_str().parse().ok()?;
    if type_char == "B" {
        Some((KalshiMarketKind::Between, Some(strike), Some(strike + bracket_step)))
    } else {
        Some((KalshiMarketKind::Above, Some(strike), None))
    }
}

#[derive(Deserialize, Debug)]
struct KalshiMarketsResponse {
    markets: Vec<KalshiMarketRaw>,
    cursor: Option<String>,
}

#[derive(Deserialize, Debug)]
struct KalshiMarketRaw {
    ticker: String,
    #[serde(default)]
    subtitle: Option<String>,
    open_time: String,
    close_time: String,
    #[serde(default)]
    result: Option<String>,
}

#[derive(Deserialize, Debug)]
struct KalshiCandlesticksResponse {
    candlesticks: Vec<KalshiCandle>,
}

#[derive(Deserialize, Debug)]
pub struct KalshiCandle {
    pub end_period_ts: i64,
    pub yes_bid: KalshiPriceBlock,
    pub yes_ask: KalshiPriceBlock,
}

#[derive(Deserialize, Debug)]
pub struct KalshiPriceBlock {
    #[serde(default)]
    pub close_dollars: Option<String>,
}

/// Blocking REST client with simple per-host throttling.
pub struct HistoricalLoader {
    client: reqwest::blocking::Client,
    last_kalshi: Option<Instant>,
    min_interval: Duration,
}

impl HistoricalLoader {
    pub fn new() -> Result<Self> {
        let client = reqwest::blocking::Client::builder()
            .user_agent("eventcontracts/0.1")
            .timeout(Duration::from_secs(15))
            .danger_accept_invalid_certs(insecure_tls())
            .build()?;
        Ok(Self {
            client,
            last_kalshi: None,
            min_interval: Duration::from_millis(250),
        })
    }

    fn throttle_kalshi(&mut self) {
        if let Some(last) = self.last_kalshi {
            let elapsed = last.elapsed();
            if elapsed < self.min_interval {
                sleep(self.min_interval - elapsed);
            }
        }
        self.last_kalshi = Some(Instant::now());
    }

    fn get_json<T: serde::de::DeserializeOwned>(
        &self,
        url: &str,
    ) -> Result<T> {
        let resp = self.client.get(url).send()?;
        let status = resp.status();
        let body = resp.text()?;
        if !status.is_success() {
            return Err(LoaderError::Api(format!(
                "HTTP {status} for {url}: {body}"
            )));
        }
        Ok(serde_json::from_str(&body)?)
    }

    pub fn list_markets(
        &mut self,
        series_ticker: &str,
        status: &str,
        expiry_hour_token: Option<&str>,
        max_pages: usize,
    ) -> Result<Vec<KalshiMarket>> {
        let mut out = Vec::new();
        let mut cursor: Option<String> = None;
        let step = Decimal::from(100);
        for _ in 0..max_pages {
            let mut url = format!(
                "{KALSHI_BASE}/markets?series_ticker={series_ticker}&status={status}&limit=1000"
            );
            if let Some(c) = &cursor {
                url.push_str(&format!("&cursor={c}"));
            }
            self.throttle_kalshi();
            let response: KalshiMarketsResponse = self.get_json(&url)?;
            let mut cohort_seen = false;
            for raw in &response.markets {
                if let Some(tok) = expiry_hour_token {
                    if !raw.ticker.contains(tok) {
                        continue;
                    }
                    cohort_seen = true;
                }
                let Some((mut kind, mut lower, mut upper)) = parse_ticker(&raw.ticker, step) else {
                    continue;
                };
                let subtitle = raw.subtitle.clone().unwrap_or_default();
                if matches!(kind, KalshiMarketKind::Above)
                    && subtitle.to_lowercase().contains("below")
                {
                    kind = KalshiMarketKind::Below;
                    upper = lower;
                    lower = None;
                }
                let open_time = parse_kalshi_datetime(&raw.open_time)?;
                let close_time = parse_kalshi_datetime(&raw.close_time)?;
                out.push(KalshiMarket {
                    ticker: raw.ticker.clone(),
                    subtitle,
                    open_time,
                    close_time,
                    kind,
                    lower,
                    upper,
                    result: raw.result.clone(),
                });
            }
            cursor = response.cursor;
            if cursor.is_none() {
                break;
            }
            if expiry_hour_token.is_some() && !out.is_empty() && !cohort_seen {
                break;
            }
        }
        Ok(out)
    }

    pub fn fetch_candlesticks(
        &mut self,
        series_ticker: &str,
        market_ticker: &str,
        start_ts: i64,
        end_ts: i64,
        period_interval: u32,
    ) -> Result<Vec<KalshiCandle>> {
        let url = format!(
            "{KALSHI_BASE}/series/{series_ticker}/markets/{market_ticker}/candlesticks?start_ts={start_ts}&end_ts={end_ts}&period_interval={period_interval}"
        );
        self.throttle_kalshi();
        let response: KalshiCandlesticksResponse = self.get_json(&url)?;
        Ok(response.candlesticks)
    }

    pub fn fetch_deribit_ohlc(
        &mut self,
        instrument: &str,
        start_ms: i64,
        end_ms: i64,
        resolution: &str,
    ) -> Result<Vec<DeribitOhlc>> {
        let url = format!(
            "{DERIBIT_BASE}/public/get_tradingview_chart_data?instrument_name={instrument}&start_timestamp={start_ms}&end_timestamp={end_ms}&resolution={resolution}"
        );
        let raw: DeribitChartResponse = self.get_json(&url)?;
        let mut out = Vec::new();
        for (ts_ms, close) in raw
            .result
            .ticks
            .iter()
            .zip(raw.result.close.iter())
        {
            let ts = Utc.timestamp_millis_opt(*ts_ms).single().ok_or_else(|| {
                LoaderError::Validation(format!("bad deribit ts {ts_ms}"))
            })?;
            out.push(DeribitOhlc {
                timestamp: ts,
                close: Decimal::try_from(*close).unwrap_or(Decimal::ZERO),
            });
        }
        Ok(out)
    }

    pub fn fetch_deribit_dvol(
        &mut self,
        currency: &str,
        start_ms: i64,
        end_ms: i64,
        resolution_seconds: i64,
    ) -> Result<Vec<DeribitDvol>> {
        let url = format!(
            "{DERIBIT_BASE}/public/get_volatility_index_data?currency={currency}&start_timestamp={start_ms}&end_timestamp={end_ms}&resolution={resolution_seconds}"
        );
        let raw: DeribitVolResponse = self.get_json(&url)?;
        let mut out = Vec::new();
        for row in raw.result.data {
            // row: [ts_ms, open, high, low, close]
            if row.len() < 5 {
                continue;
            }
            let ts_ms = row[0] as i64;
            let close = row[4];
            let ts = Utc
                .timestamp_millis_opt(ts_ms)
                .single()
                .ok_or_else(|| LoaderError::Validation("bad dvol ts".into()))?;
            out.push(DeribitDvol {
                timestamp: ts,
                // DVOL is in percent; convert to fraction.
                dvol_pct: Decimal::try_from(close).unwrap_or(Decimal::ZERO)
                    / Decimal::ONE_HUNDRED,
            });
        }
        Ok(out)
    }
}

fn insecure_tls() -> bool {
    std::env::var("EVENTCONTRACTS_INSECURE_TLS")
        .map(|v| v == "1")
        .unwrap_or(false)
}

fn parse_kalshi_datetime(value: &str) -> Result<DateTime<Utc>> {
    DateTime::parse_from_rfc3339(value)
        .map(|d| d.with_timezone(&Utc))
        .map_err(|e| LoaderError::Validation(format!("bad datetime {value}: {e}")))
}

// ----------------------------- Deribit shapes -----------------------------

#[derive(Debug, Clone)]
pub struct DeribitOhlc {
    pub timestamp: DateTime<Utc>,
    pub close: Decimal,
}

#[derive(Debug, Clone)]
pub struct DeribitDvol {
    pub timestamp: DateTime<Utc>,
    /// Annualized DVOL as a fraction (0.34 = 34%).
    pub dvol_pct: Decimal,
}

#[derive(Deserialize, Debug)]
struct DeribitChartResponse {
    result: DeribitChartResult,
}

#[derive(Deserialize, Debug)]
struct DeribitChartResult {
    ticks: Vec<i64>,
    close: Vec<f64>,
}

#[derive(Deserialize, Debug)]
struct DeribitVolResponse {
    result: DeribitVolResult,
}

#[derive(Deserialize, Debug)]
struct DeribitVolResult {
    data: Vec<Vec<f64>>,
}

// ----------------------------- Settlement -----------------------------

#[derive(Debug, Clone, Default)]
pub struct CohortSettlement {
    pub yes_market_ticker: Option<String>,
    pub settlement_price: Option<Decimal>,
    pub bracket_results: BTreeMap<String, String>,
}

impl HistoricalLoader {
    pub fn fetch_cohort_settlement(
        &mut self,
        series_ticker: &str,
        expiry_hour_token: &str,
        max_pages: usize,
    ) -> Result<CohortSettlement> {
        let markets = self.list_markets(
            series_ticker,
            "settled",
            Some(expiry_hour_token),
            max_pages,
        )?;
        let mut out = CohortSettlement::default();
        let step = Decimal::from(100);
        for m in &markets {
            if let Some(result) = &m.result {
                out.bracket_results.insert(m.ticker.clone(), result.clone());
                if result == "yes" && out.yes_market_ticker.is_none() {
                    if let Some((kind, lower, upper)) = parse_ticker(&m.ticker, step) {
                        if matches!(kind, KalshiMarketKind::Between) {
                            out.yes_market_ticker = Some(m.ticker.clone());
                            if let (Some(l), Some(u)) = (lower, upper) {
                                out.settlement_price = Some((l + u) / Decimal::TWO);
                            }
                        }
                    }
                }
            }
        }
        Ok(out)
    }
}

// ----------------------------- Quote events -----------------------------

#[derive(Debug, Clone)]
pub struct QuoteSample {
    pub market_id: String,
    pub timestamp: DateTime<Utc>,
    pub bid: Decimal,
    pub ask: Decimal,
}

pub fn candles_to_quotes(market_id: &str, candles: &[KalshiCandle]) -> Vec<QuoteSample> {
    let mut out = Vec::with_capacity(candles.len());
    for c in candles {
        let Some(bid_raw) = c.yes_bid.close_dollars.as_deref() else {
            continue;
        };
        let Some(ask_raw) = c.yes_ask.close_dollars.as_deref() else {
            continue;
        };
        let bid: Decimal = match bid_raw.parse() {
            Ok(v) => v,
            Err(_) => continue,
        };
        let ask: Decimal = match ask_raw.parse() {
            Ok(v) => v,
            Err(_) => continue,
        };
        if ask <= Decimal::ZERO || bid >= ask {
            continue;
        }
        let ts = Utc
            .timestamp_opt(c.end_period_ts, 0)
            .single()
            .unwrap_or_else(Utc::now);
        out.push(QuoteSample {
            market_id: market_id.to_string(),
            timestamp: ts,
            bid,
            ask,
        });
    }
    out
}

// ----------------------------- Date helpers -----------------------------

pub fn naive_date_utc_midnight(date: NaiveDate) -> DateTime<Utc> {
    Utc.from_utc_datetime(&date.and_hms_opt(0, 0, 0).expect("valid time"))
}

// ----------------------------- Tests -----------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use std::str::FromStr;

    #[test]
    fn parses_between_bracket_ticker() {
        let step = Decimal::from(100);
        let r = parse_ticker("KXBTC-26MAY2508-B77250", step).unwrap();
        assert_eq!(r.0, KalshiMarketKind::Between);
        assert_eq!(r.1, Some(Decimal::from(77250)));
        assert_eq!(r.2, Some(Decimal::from(77350)));
    }

    #[test]
    fn parses_above_tail_ticker() {
        let step = Decimal::from(100);
        let r = parse_ticker("KXBTC-26MAY2508-T85799.99", step).unwrap();
        assert_eq!(r.0, KalshiMarketKind::Above);
        assert_eq!(r.1, Some(Decimal::from_str("85799.99").unwrap()));
        assert!(r.2.is_none());
    }

    #[test]
    fn parses_kxbtcd_above_ticker() {
        // KXBTCD has KXBTCD-... prefix; the loosened regex must accept it.
        let step = Decimal::from(100);
        let r = parse_ticker("KXBTCD-26MAY2317-T76999.99", step).unwrap();
        assert_eq!(r.0, KalshiMarketKind::Above);
        assert_eq!(r.1, Some(Decimal::from_str("76999.99").unwrap()));
    }

    #[test]
    fn candles_to_quotes_filters_one_sided() {
        let candle = KalshiCandle {
            end_period_ts: 1_700_000_000,
            yes_bid: KalshiPriceBlock {
                close_dollars: Some("0.00".into()),
            },
            yes_ask: KalshiPriceBlock {
                close_dollars: Some("0.00".into()),
            },
        };
        let q = candles_to_quotes("M", &[candle]);
        assert!(q.is_empty());
    }
}
