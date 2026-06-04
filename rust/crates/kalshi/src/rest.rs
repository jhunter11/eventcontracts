//! Authenticated Kalshi REST client.
//!
//! Read endpoints (`get_markets_page`, `list_open_markets`) are unconditional.
//! Write endpoints (`submit_order`, `cancel_order`) are present but isolated:
//! they can only be invoked when a caller constructs a `KalshiVenueClient`
//! (see `venue_client.rs`), which in turn requires explicit opt-in plumbing
//! from `live-runner --live-submit`.

use crate::auth::KalshiAuth;
use eventcontracts_gateway::{OutcomeSide, RestingOrderSnapshot};
use eventcontracts_oms::{OrderState, Side, TimeInForce};
use reqwest::{header::RETRY_AFTER, Client, ClientBuilder};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::sync::atomic::{AtomicU32, Ordering};
use std::time::{Duration, Instant};
use thiserror::Error;
use time::format_description::well_known::Rfc3339;
use time::OffsetDateTime;
use tokio::sync::Mutex;
use url::Url;

#[derive(Debug, Error)]
pub enum RestError {
    #[error("url parse failed: {0}")]
    Url(#[from] url::ParseError),
    #[error("reqwest error: {0}")]
    Reqwest(#[from] reqwest::Error),
    #[error("kalshi returned status {status}: {body}")]
    Status { status: u16, body: String },
    #[error("auth signing failed: {0}")]
    Sign(#[from] crate::auth::SignError),
    #[error("json parse failed: {0}")]
    Json(#[from] serde_json::Error),
    #[error("rest circuit breaker open after repeated auth failures")]
    CircuitOpen,
}

#[derive(Debug, Clone, Deserialize)]
pub struct MarketsResponse {
    #[serde(default)]
    pub markets: Vec<MarketRow>,
    #[serde(default)]
    pub cursor: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct MarketRow {
    pub ticker: String,
    #[serde(default)]
    pub status: Option<String>,
    #[serde(default)]
    pub title: Option<String>,
    #[serde(default)]
    pub event_ticker: Option<String>,
    #[serde(default)]
    pub series_ticker: Option<String>,
}

/// Body of `POST /portfolio/orders`. Field names match Kalshi's API exactly.
#[derive(Debug, Clone, Serialize)]
pub struct PlaceOrderRequest {
    pub ticker: String,
    pub client_order_id: String,
    /// `"yes"` or `"no"` — which side of the binary outcome.
    pub side: String,
    /// `"buy"` or `"sell"`.
    pub action: String,
    /// `"limit"`. Market orders are intentionally not supported.
    #[serde(rename = "type")]
    pub order_type: String,
    pub count: u32,
    /// Integer cents (1..=99). Mutually exclusive with `no_price`.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub yes_price: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub no_price: Option<i64>,
    /// `"GTC"`, `"IOC"`, or `"FOK"`.
    pub time_in_force: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct PlaceOrderResponse {
    pub order: KalshiOrder,
}

#[derive(Debug, Clone, Deserialize)]
pub struct OrdersResponse {
    #[serde(default)]
    pub orders: Vec<KalshiOrder>,
    #[serde(default)]
    pub cursor: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct FillsResponse {
    #[serde(default)]
    pub fills: Vec<KalshiFill>,
    #[serde(default)]
    pub cursor: Option<String>,
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct KalshiFill {
    #[serde(default)]
    pub fill_id: Option<String>,
    #[serde(default)]
    pub trade_id: Option<String>,
    #[serde(default)]
    pub order_id: Option<String>,
    #[serde(default)]
    pub client_order_id: Option<String>,
    #[serde(default)]
    pub ticker: Option<String>,
    #[serde(default)]
    pub market_ticker: Option<String>,
    #[serde(default)]
    pub side: Option<String>,
    #[serde(default)]
    pub action: Option<String>,
    #[serde(default)]
    pub count: Option<Value>,
    #[serde(default)]
    pub quantity: Option<Value>,
    #[serde(default)]
    pub price: Option<Value>,
    #[serde(default)]
    pub yes_price: Option<Value>,
    #[serde(default)]
    pub no_price: Option<Value>,
    #[serde(default)]
    pub fee: Option<Value>,
    #[serde(default)]
    pub fee_dollars: Option<Value>,
    #[serde(default)]
    pub realized_pnl: Option<Value>,
    #[serde(default)]
    pub realized_pnl_dollars: Option<Value>,
    #[serde(default)]
    pub pnl: Option<Value>,
    #[serde(default)]
    pub profit_loss: Option<Value>,
    #[serde(default)]
    pub ts: Option<Value>,
    #[serde(default)]
    pub ts_ms: Option<Value>,
    #[serde(default)]
    pub created_time: Option<String>,
    #[serde(default)]
    pub trade_time: Option<String>,
}

impl KalshiFill {
    pub fn epoch_secs(&self) -> Option<i64> {
        self.ts
            .as_ref()
            .and_then(value_to_epoch_secs)
            .or_else(|| {
                self.ts_ms
                    .as_ref()
                    .and_then(value_to_epoch_millis)
                    .map(|v| v / 1000)
            })
            .or_else(|| self.trade_time.as_deref().and_then(rfc3339_epoch_secs))
            .or_else(|| self.created_time.as_deref().and_then(rfc3339_epoch_secs))
    }

    pub fn realized_loss_ticks(&self) -> i64 {
        let realized = self
            .realized_pnl_dollars
            .as_ref()
            .or(self.realized_pnl.as_ref())
            .or(self.pnl.as_ref())
            .or(self.profit_loss.as_ref())
            .and_then(decimal_value_to_ticks)
            .unwrap_or(0);
        let realized_loss = realized.min(0).saturating_neg();
        realized_loss.saturating_add(self.fee_ticks())
    }

    pub fn fee_ticks(&self) -> i64 {
        self.fee_dollars
            .as_ref()
            .or(self.fee.as_ref())
            .and_then(decimal_value_to_ticks)
            .unwrap_or(0)
            .max(0)
    }
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct PositionsResponse {
    #[serde(default)]
    pub market_positions: Vec<KalshiPosition>,
    #[serde(default)]
    pub cursor: Option<String>,
}

/// One row of `GET /portfolio/positions`. Tolerant of field naming so a venue
/// schema tweak degrades gracefully instead of failing reconciliation.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct KalshiPosition {
    #[serde(default)]
    pub ticker: Option<String>,
    #[serde(default)]
    pub market_ticker: Option<String>,
    /// Signed net contracts. Positive = long YES, negative = long NO.
    #[serde(default)]
    pub position: Option<Value>,
    /// Capital tied up by the position, in integer cents.
    #[serde(default)]
    pub market_exposure: Option<Value>,
    /// Cost basis, in integer cents, when the venue reports it.
    #[serde(default)]
    pub position_cost: Option<Value>,
    #[serde(default)]
    pub realized_pnl: Option<Value>,
}

impl KalshiPosition {
    pub fn ticker(&self) -> Option<&str> {
        self.ticker.as_deref().or(self.market_ticker.as_deref())
    }

    /// Signed net contracts (positive = YES, negative = NO), or `None` if the
    /// venue omitted the field.
    pub fn signed_contracts(&self) -> Option<i64> {
        self.position.as_ref().and_then(value_to_i64)
    }

    /// Best-effort per-contract price in ticks (dollars * 10_000), derived from
    /// `position_cost` when present, otherwise `market_exposure`. Used to seed a
    /// conservative startup mark for the risk gate before the first live quote
    /// arrives. Returns 0 when no cost basis is available.
    pub fn avg_price_ticks(&self) -> i64 {
        let contracts = self.signed_contracts().unwrap_or(0).abs();
        if contracts == 0 {
            return 0;
        }
        let cents = self
            .position_cost
            .as_ref()
            .or(self.market_exposure.as_ref())
            .and_then(value_to_i64)
            .unwrap_or(0)
            .abs();
        // cents -> ticks is *100; then per-contract.
        cents.saturating_mul(100) / contracts
    }
}

/// `GET /portfolio/balance` response. `balance` is integer cents.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct KalshiBalance {
    #[serde(default)]
    pub balance: Option<Value>,
}

impl KalshiBalance {
    pub fn available_cents(&self) -> i64 {
        self.balance.as_ref().and_then(value_to_i64).unwrap_or(0)
    }

    /// Available cash in ticks (dollars * 10_000).
    pub fn available_ticks(&self) -> i64 {
        self.available_cents().saturating_mul(100)
    }
}

#[derive(Debug, Clone, Deserialize)]
pub struct KalshiOrder {
    pub order_id: String,
    #[serde(default)]
    pub client_order_id: Option<String>,
    #[serde(default)]
    pub ticker: Option<String>,
    #[serde(default)]
    pub market_ticker: Option<String>,
    #[serde(default)]
    pub side: Option<String>,
    #[serde(default)]
    pub action: Option<String>,
    #[serde(default, rename = "type")]
    pub order_type: Option<String>,
    #[serde(default)]
    pub count: Option<Value>,
    #[serde(default)]
    pub remaining_count: Option<Value>,
    #[serde(default)]
    pub filled_count: Option<Value>,
    #[serde(default)]
    pub yes_price: Option<Value>,
    #[serde(default)]
    pub no_price: Option<Value>,
    #[serde(default)]
    pub time_in_force: Option<String>,
    #[serde(default)]
    pub status: Option<String>,
    #[serde(default)]
    pub created_time: Option<String>,
    #[serde(default)]
    pub last_update_time: Option<String>,
    /// Any venue fields we do not explicitly model above. Captured rather than
    /// silently dropped so that a Kalshi field rename — e.g. the limit price
    /// moving to a new key — surfaces in the reconcile-on-start failure dump
    /// instead of masquerading as a priceless order.
    #[serde(flatten)]
    pub extra: std::collections::BTreeMap<String, Value>,
}

impl KalshiOrder {
    pub fn to_resting_snapshot(&self, now: &str) -> Result<RestingOrderSnapshot, String> {
        let client_order_id = self
            .client_order_id
            .clone()
            .ok_or_else(|| format!("venue order {} has no client_order_id", self.order_id))?;
        let ticker = self
            .ticker
            .as_deref()
            .or(self.market_ticker.as_deref())
            .ok_or_else(|| format!("venue order {} has no ticker", self.order_id))?;
        let action = self
            .action
            .as_deref()
            .ok_or_else(|| format!("venue order {} has no action", self.order_id))?;
        let side = match action.to_ascii_lowercase().as_str() {
            "buy" => Side::Buy,
            "sell" => Side::Sell,
            other => {
                return Err(format!(
                    "venue order {} has unsupported action `{other}`",
                    self.order_id
                ))
            }
        };
        let outcome_side = match self
            .side
            .as_deref()
            .unwrap_or("yes")
            .to_ascii_lowercase()
            .as_str()
        {
            "yes" => OutcomeSide::Yes,
            "no" => OutcomeSide::No,
            other => {
                return Err(format!(
                    "venue order {} has unsupported outcome side `{other}`",
                    self.order_id
                ))
            }
        };
        let price_value = match outcome_side {
            OutcomeSide::Yes => self.yes_price.as_ref().or(self.no_price.as_ref()),
            OutcomeSide::No => self.no_price.as_ref().or(self.yes_price.as_ref()),
        }
        .ok_or_else(|| format!("venue order {} has no price", self.order_id))?;
        let price = cents_value_to_decimal(price_value)
            .ok_or_else(|| format!("venue order {} has invalid price", self.order_id))?;
        let quantity = count_value_to_decimal(self.count.as_ref())
            .or_else(|| count_value_to_decimal(self.remaining_count.as_ref()))
            .ok_or_else(|| format!("venue order {} has no count", self.order_id))?;
        let filled_quantity =
            count_value_to_decimal(self.filled_count.as_ref()).unwrap_or_else(|| "0".into());
        let tif = match self
            .time_in_force
            .as_deref()
            .unwrap_or("GTC")
            .to_ascii_uppercase()
            .as_str()
        {
            "GTC" => TimeInForce::Gtc,
            "IOC" => TimeInForce::Ioc,
            "FOK" => TimeInForce::Fok,
            other => {
                return Err(format!(
                    "venue order {} has unsupported time_in_force `{other}`",
                    self.order_id
                ))
            }
        };
        let state = match self
            .status
            .as_deref()
            .unwrap_or("resting")
            .to_ascii_lowercase()
            .as_str()
        {
            "resting" | "open" | "live" | "accepted" => OrderState::Acked,
            "partially_filled" | "partially-filled" | "partial" => OrderState::PartiallyFilled,
            other => {
                return Err(format!(
                    "venue order {} is not adoptable from status `{other}`",
                    self.order_id
                ))
            }
        };
        Ok(RestingOrderSnapshot {
            client_order_id,
            venue_order_id: Some(self.order_id.clone()),
            instrument_id: format!("kalshi:{ticker}"),
            outcome_side,
            side,
            price,
            quantity,
            filled_quantity,
            time_in_force: tif,
            state,
            updated_at: self
                .last_update_time
                .clone()
                .or_else(|| self.created_time.clone())
                .unwrap_or_else(|| now.to_string()),
            observed_at: now.to_string(),
            reject_reason: None,
        })
    }
}

pub struct KalshiRest {
    client: Client,
    base_url: String,
    auth: KalshiAuth,
    min_write_interval: Duration,
    last_write_at: Mutex<Option<Instant>>,
    auth_failures: AtomicU32,
}

impl KalshiRest {
    pub fn new(base_url: impl Into<String>, auth: KalshiAuth) -> Result<Self, RestError> {
        let client = ClientBuilder::new()
            .user_agent("eventcontracts-rs/0.1")
            .timeout(std::time::Duration::from_secs(15))
            .build()?;
        Ok(Self {
            client,
            base_url: base_url.into().trim_end_matches('/').to_string(),
            auth,
            min_write_interval: Duration::from_millis(200),
            last_write_at: Mutex::new(None),
            auth_failures: AtomicU32::new(0),
        })
    }

    /// `GET /markets` with optional filters. Returns at most `limit` rows per
    /// page; this method does not paginate — callers can call repeatedly with
    /// `cursor`.
    pub async fn get_markets_page(
        &self,
        limit: u32,
        cursor: Option<&str>,
        status: Option<&str>,
        series_ticker: Option<&str>,
    ) -> Result<MarketsResponse, RestError> {
        let mut url = Url::parse(&format!("{}/markets", self.base_url))?;
        {
            let mut q = url.query_pairs_mut();
            q.append_pair("limit", &limit.to_string());
            if let Some(c) = cursor {
                q.append_pair("cursor", c);
            }
            if let Some(s) = status {
                q.append_pair("status", s);
            }
            if let Some(t) = series_ticker {
                q.append_pair("series_ticker", t);
            }
        }
        let signed = self.auth.sign("GET", url.path())?;
        let mut req = self.client.get(url.as_str());
        for (k, v) in signed.as_pairs() {
            req = req.header(k, v);
        }
        let resp = req.send().await?;
        let status = resp.status();
        let bytes = resp.bytes().await?;
        if !status.is_success() {
            return Err(RestError::Status {
                status: status.as_u16(),
                body: String::from_utf8_lossy(&bytes).into_owned(),
            });
        }
        Ok(serde_json::from_slice(&bytes)?)
    }

    /// `POST /portfolio/orders` — submit a live order. Idempotent on
    /// `client_order_id` (Kalshi rejects a duplicate with 4xx and the
    /// original venue_order_id, which the caller treats as success-on-retry).
    pub async fn submit_order(
        &self,
        req: &PlaceOrderRequest,
    ) -> Result<PlaceOrderResponse, RestError> {
        let url = Url::parse(&format!("{}/portfolio/orders", self.base_url))?;
        self.ensure_circuit_closed()?;
        for attempt in 0..3 {
            self.throttle_write().await;
            let signed = self.auth.sign("POST", url.path())?;
            let mut request = self.client.post(url.as_str()).json(req);
            for (k, v) in signed.as_pairs() {
                request = request.header(k, v);
            }
            let resp = request.send().await?;
            let status = resp.status();
            let retry_after = retry_after_delay(&resp);
            let bytes = resp.bytes().await?;
            if status.is_success() {
                self.auth_failures.store(0, Ordering::Relaxed);
                return Ok(serde_json::from_slice(&bytes)?);
            }
            self.note_status(status.as_u16());
            if should_retry(status.as_u16()) && attempt < 2 {
                tokio::time::sleep(retry_after.unwrap_or_else(|| backoff(attempt))).await;
                continue;
            }
            return Err(RestError::Status {
                status: status.as_u16(),
                body: String::from_utf8_lossy(&bytes).into_owned(),
            });
        }
        unreachable!("retry loop returns")
    }

    /// `DELETE /portfolio/orders/{order_id}` — cancel a resting order by
    /// venue-assigned id. Returns the latest order state.
    pub async fn cancel_order(&self, venue_order_id: &str) -> Result<KalshiOrder, RestError> {
        let url = Url::parse(&format!(
            "{}/portfolio/orders/{venue_order_id}",
            self.base_url
        ))?;
        self.ensure_circuit_closed()?;
        for attempt in 0..3 {
            self.throttle_write().await;
            let signed = self.auth.sign("DELETE", url.path())?;
            let mut request = self.client.delete(url.as_str());
            for (k, v) in signed.as_pairs() {
                request = request.header(k, v);
            }
            let resp = request.send().await?;
            let status = resp.status();
            let retry_after = retry_after_delay(&resp);
            let bytes = resp.bytes().await?;
            if status.is_success() {
                self.auth_failures.store(0, Ordering::Relaxed);
                let parsed: PlaceOrderResponse = serde_json::from_slice(&bytes)?;
                return Ok(parsed.order);
            }
            self.note_status(status.as_u16());
            if should_retry(status.as_u16()) && attempt < 2 {
                tokio::time::sleep(retry_after.unwrap_or_else(|| backoff(attempt))).await;
                continue;
            }
            return Err(RestError::Status {
                status: status.as_u16(),
                body: String::from_utf8_lossy(&bytes).into_owned(),
            });
        }
        unreachable!("retry loop returns")
    }

    /// `DELETE /portfolio/orders` â€” venue-side bulk cancel. Used by shutdown
    /// handlers and kill switches so local client-order caches are not a
    /// prerequisite for making the account safe.
    pub async fn cancel_all_open_orders(&self) -> Result<Vec<KalshiOrder>, RestError> {
        let url = Url::parse(&format!("{}/portfolio/orders", self.base_url))?;
        self.ensure_circuit_closed()?;
        for attempt in 0..3 {
            self.throttle_write().await;
            let signed = self.auth.sign("DELETE", url.path())?;
            let mut request = self.client.delete(url.as_str());
            for (k, v) in signed.as_pairs() {
                request = request.header(k, v);
            }
            let resp = request.send().await?;
            let status = resp.status();
            let retry_after = retry_after_delay(&resp);
            let bytes = resp.bytes().await?;
            if status.is_success() {
                self.auth_failures.store(0, Ordering::Relaxed);
                if bytes.is_empty() {
                    return Ok(vec![]);
                }
                return serde_json::from_slice::<OrdersResponse>(&bytes)
                    .map(|r| r.orders)
                    .or_else(|_| {
                        serde_json::from_slice::<PlaceOrderResponse>(&bytes).map(|r| vec![r.order])
                    })
                    .map_err(Into::into);
            }
            self.note_status(status.as_u16());
            if should_retry(status.as_u16()) && attempt < 2 {
                tokio::time::sleep(retry_after.unwrap_or_else(|| backoff(attempt))).await;
                continue;
            }
            return Err(RestError::Status {
                status: status.as_u16(),
                body: String::from_utf8_lossy(&bytes).into_owned(),
            });
        }
        unreachable!("retry loop returns")
    }

    /// `GET /portfolio/orders?status=resting` — return all open orders for
    /// the authenticated account. Used by startup reconciliation to detect
    /// resting orders left from a previous process lifetime.
    pub async fn list_open_orders(&self) -> Result<Vec<KalshiOrder>, RestError> {
        let mut out = Vec::new();
        let mut cursor: Option<String> = None;
        loop {
            let mut url = Url::parse(&format!("{}/portfolio/orders", self.base_url))?;
            {
                let mut q = url.query_pairs_mut();
                q.append_pair("status", "resting");
                q.append_pair("limit", "200");
                if let Some(c) = cursor.as_deref() {
                    q.append_pair("cursor", c);
                }
            }
            let signed = self.auth.sign("GET", url.path())?;
            let mut req = self.client.get(url.as_str());
            for (k, v) in signed.as_pairs() {
                req = req.header(k, v);
            }
            let resp = req.send().await?;
            let status = resp.status();
            let bytes = resp.bytes().await?;
            if !status.is_success() {
                self.note_status(status.as_u16());
                return Err(RestError::Status {
                    status: status.as_u16(),
                    body: String::from_utf8_lossy(&bytes).into_owned(),
                });
            }
            self.auth_failures.store(0, Ordering::Relaxed);
            let parsed: OrdersResponse = serde_json::from_slice(&bytes)?;
            out.extend(parsed.orders);
            match parsed.cursor {
                Some(next) if !next.is_empty() => cursor = Some(next),
                _ => break,
            }
        }
        Ok(out)
    }

    /// `GET /portfolio/fills` since an epoch-second checkpoint. Used by
    /// startup reconciliation to restore today's realized loss after a
    /// process restart.
    pub async fn list_fills_since(&self, epoch_secs: i64) -> Result<Vec<KalshiFill>, RestError> {
        let mut out = Vec::new();
        let mut cursor: Option<String> = None;
        loop {
            let mut url = Url::parse(&format!("{}/portfolio/fills", self.base_url))?;
            {
                let mut q = url.query_pairs_mut();
                q.append_pair("min_ts", &epoch_secs.to_string());
                q.append_pair("limit", "200");
                if let Some(c) = cursor.as_deref() {
                    q.append_pair("cursor", c);
                }
            }
            let signed = self.auth.sign("GET", url.path())?;
            let mut req = self.client.get(url.as_str());
            for (k, v) in signed.as_pairs() {
                req = req.header(k, v);
            }
            let resp = req.send().await?;
            let status = resp.status();
            let bytes = resp.bytes().await?;
            if !status.is_success() {
                self.note_status(status.as_u16());
                return Err(RestError::Status {
                    status: status.as_u16(),
                    body: String::from_utf8_lossy(&bytes).into_owned(),
                });
            }
            self.auth_failures.store(0, Ordering::Relaxed);
            let parsed: FillsResponse = serde_json::from_slice(&bytes)?;
            out.extend(
                parsed
                    .fills
                    .into_iter()
                    .filter(|fill| fill.epoch_secs().map(|ts| ts >= epoch_secs).unwrap_or(true)),
            );
            match parsed.cursor {
                Some(next) if !next.is_empty() => cursor = Some(next),
                _ => break,
            }
        }
        Ok(out)
    }

    /// `GET /portfolio/positions` — net positions for the authenticated
    /// account. Used by startup reconciliation to seed local position truth so
    /// risk sizing matches the venue after a restart.
    pub async fn list_positions(&self) -> Result<Vec<KalshiPosition>, RestError> {
        let mut out = Vec::new();
        let mut cursor: Option<String> = None;
        loop {
            let mut url = Url::parse(&format!("{}/portfolio/positions", self.base_url))?;
            {
                let mut q = url.query_pairs_mut();
                q.append_pair("limit", "200");
                if let Some(c) = cursor.as_deref() {
                    q.append_pair("cursor", c);
                }
            }
            let signed = self.auth.sign("GET", url.path())?;
            let mut req = self.client.get(url.as_str());
            for (k, v) in signed.as_pairs() {
                req = req.header(k, v);
            }
            let resp = req.send().await?;
            let status = resp.status();
            let bytes = resp.bytes().await?;
            if !status.is_success() {
                self.note_status(status.as_u16());
                return Err(RestError::Status {
                    status: status.as_u16(),
                    body: String::from_utf8_lossy(&bytes).into_owned(),
                });
            }
            self.auth_failures.store(0, Ordering::Relaxed);
            let parsed: PositionsResponse = serde_json::from_slice(&bytes)?;
            out.extend(parsed.market_positions);
            match parsed.cursor {
                Some(next) if !next.is_empty() => cursor = Some(next),
                _ => break,
            }
        }
        Ok(out)
    }

    /// `GET /portfolio/balance` — available cash for the authenticated account.
    pub async fn get_balance(&self) -> Result<KalshiBalance, RestError> {
        let url = Url::parse(&format!("{}/portfolio/balance", self.base_url))?;
        let signed = self.auth.sign("GET", url.path())?;
        let mut req = self.client.get(url.as_str());
        for (k, v) in signed.as_pairs() {
            req = req.header(k, v);
        }
        let resp = req.send().await?;
        let status = resp.status();
        let bytes = resp.bytes().await?;
        if !status.is_success() {
            self.note_status(status.as_u16());
            return Err(RestError::Status {
                status: status.as_u16(),
                body: String::from_utf8_lossy(&bytes).into_owned(),
            });
        }
        self.auth_failures.store(0, Ordering::Relaxed);
        Ok(serde_json::from_slice(&bytes)?)
    }

    pub async fn get_order_by_client_order_id(
        &self,
        client_order_id: &str,
    ) -> Result<Option<KalshiOrder>, RestError> {
        let mut url = Url::parse(&format!("{}/portfolio/orders", self.base_url))?;
        url.query_pairs_mut()
            .append_pair("client_order_id", client_order_id);
        let signed = self.auth.sign("GET", url.path())?;
        let mut req = self.client.get(url.as_str());
        for (k, v) in signed.as_pairs() {
            req = req.header(k, v);
        }
        let resp = req.send().await?;
        let status = resp.status();
        let bytes = resp.bytes().await?;
        if !status.is_success() {
            self.note_status(status.as_u16());
            return Err(RestError::Status {
                status: status.as_u16(),
                body: String::from_utf8_lossy(&bytes).into_owned(),
            });
        }
        self.auth_failures.store(0, Ordering::Relaxed);
        let parsed: OrdersResponse = serde_json::from_slice(&bytes)?;
        Ok(parsed.orders.into_iter().next())
    }

    /// Paginate `/markets` up to `max_rows` total, optionally filtering by a
    /// ticker-prefix matcher applied client-side. Bounded so we don't drain
    /// the entire prod catalog accidentally.
    pub async fn list_open_markets(
        &self,
        max_rows: usize,
        ticker_prefix: Option<&str>,
    ) -> Result<Vec<MarketRow>, RestError> {
        let mut out = Vec::new();
        let mut cursor: Option<String> = None;
        for _ in 0..50 {
            // page cap
            let page = self
                .get_markets_page(200, cursor.as_deref(), Some("open"), None)
                .await?;
            for row in page.markets {
                if ticker_prefix
                    .map(|p| row.ticker.starts_with(p))
                    .unwrap_or(true)
                {
                    out.push(row);
                    if out.len() >= max_rows {
                        return Ok(out);
                    }
                }
            }
            match page.cursor.as_deref() {
                Some(c) if !c.is_empty() => cursor = Some(c.to_string()),
                _ => return Ok(out),
            }
        }
        Ok(out)
    }

    fn ensure_circuit_closed(&self) -> Result<(), RestError> {
        if self.auth_failures.load(Ordering::Relaxed) >= 5 {
            return Err(RestError::CircuitOpen);
        }
        Ok(())
    }

    fn note_status(&self, status: u16) {
        if matches!(status, 401 | 403) {
            self.auth_failures.fetch_add(1, Ordering::Relaxed);
        }
    }

    async fn throttle_write(&self) {
        let mut last = self.last_write_at.lock().await;
        if let Some(previous) = *last {
            let elapsed = previous.elapsed();
            if elapsed < self.min_write_interval {
                tokio::time::sleep(self.min_write_interval - elapsed).await;
            }
        }
        *last = Some(Instant::now());
    }
}

fn should_retry(status: u16) -> bool {
    matches!(status, 429 | 500 | 502 | 503 | 504)
}

fn backoff(attempt: usize) -> Duration {
    Duration::from_millis(250 * (1_u64 << attempt.min(4)))
}

fn retry_after_delay(resp: &reqwest::Response) -> Option<Duration> {
    resp.headers()
        .get(RETRY_AFTER)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.parse::<u64>().ok())
        .map(Duration::from_secs)
}

fn cents_value_to_decimal(value: &Value) -> Option<String> {
    let cents = match value {
        Value::Number(n) => n
            .as_i64()
            .or_else(|| n.as_u64().and_then(|u| i64::try_from(u).ok())),
        Value::String(s) => s.parse::<i64>().ok(),
        _ => None,
    }?;
    if !(0..=100).contains(&cents) {
        return None;
    }
    Some(format!("{}.{:02}", cents / 100, cents % 100))
}

fn count_value_to_decimal(value: Option<&Value>) -> Option<String> {
    let value = value?;
    match value {
        Value::Number(n) => n
            .as_i64()
            .map(|v| v.max(0).to_string())
            .or_else(|| n.as_u64().map(|v| v.to_string())),
        Value::String(s) => {
            if s.trim().is_empty() {
                None
            } else {
                Some(s.trim().to_string())
            }
        }
        _ => None,
    }
}

fn value_to_i64(value: &Value) -> Option<i64> {
    match value {
        Value::Number(n) => n
            .as_i64()
            .or_else(|| n.as_u64().and_then(|u| i64::try_from(u).ok()))
            .or_else(|| n.as_f64().map(|f| f as i64)),
        Value::String(s) => s.trim().parse::<i64>().ok(),
        _ => None,
    }
}

fn value_to_epoch_secs(value: &Value) -> Option<i64> {
    match value {
        Value::Number(n) => n
            .as_i64()
            .or_else(|| n.as_u64().and_then(|v| i64::try_from(v).ok())),
        Value::String(s) => s
            .trim()
            .parse::<i64>()
            .ok()
            .or_else(|| rfc3339_epoch_secs(s.trim())),
        _ => None,
    }
}

fn value_to_epoch_millis(value: &Value) -> Option<i64> {
    match value {
        Value::Number(n) => n
            .as_i64()
            .or_else(|| n.as_u64().and_then(|v| i64::try_from(v).ok())),
        Value::String(s) => s.trim().parse::<i64>().ok(),
        _ => None,
    }
}

fn rfc3339_epoch_secs(value: &str) -> Option<i64> {
    OffsetDateTime::parse(value, &Rfc3339)
        .ok()
        .map(|dt| dt.unix_timestamp())
}

fn decimal_value_to_ticks(value: &Value) -> Option<i64> {
    match value {
        Value::Number(n) => decimal_str_to_ticks(&n.to_string()),
        Value::String(s) => decimal_str_to_ticks(s.trim()),
        _ => None,
    }
}

fn decimal_str_to_ticks(raw: &str) -> Option<i64> {
    if raw.is_empty() {
        return None;
    }
    let (negative, body) = raw
        .strip_prefix('-')
        .map(|rest| (true, rest))
        .unwrap_or((false, raw));
    let body = body.strip_prefix('+').unwrap_or(body);
    let mut parts = body.split('.');
    let whole = parts.next().unwrap_or("0");
    let frac = parts.next().unwrap_or("");
    if parts.next().is_some() || whole.is_empty() {
        return None;
    }
    let whole_ticks: i128 = whole.parse::<i128>().ok()?.saturating_mul(10_000);
    let mut frac_buf = String::with_capacity(4);
    for c in frac.chars().take(4) {
        if !c.is_ascii_digit() {
            return None;
        }
        frac_buf.push(c);
    }
    while frac_buf.len() < 4 {
        frac_buf.push('0');
    }
    let frac_ticks: i128 = frac_buf.parse::<i128>().ok()?;
    let mut ticks = whole_ticks.saturating_add(frac_ticks);
    if negative {
        ticks = -ticks;
    }
    i64::try_from(ticks).ok()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn fill_loss_sums_negative_realized_pnl_and_fee() {
        let fill = KalshiFill {
            realized_pnl_dollars: Some(json!("-100.00")),
            fee_dollars: Some(json!("0.02")),
            ts: Some(json!(1_779_631_202_i64)),
            ..KalshiFill::default()
        };

        assert_eq!(fill.epoch_secs(), Some(1_779_631_202));
        assert_eq!(fill.realized_loss_ticks(), 1_000_200);
    }

    #[test]
    fn fill_loss_ignores_positive_realized_pnl_but_keeps_fee() {
        let fill = KalshiFill {
            realized_pnl_dollars: Some(json!("10.00")),
            fee_dollars: Some(json!("0.01")),
            ..KalshiFill::default()
        };

        assert_eq!(fill.realized_loss_ticks(), 100);
    }

    #[test]
    fn balance_parses_cents_into_ticks() {
        let balance: KalshiBalance = serde_json::from_value(json!({"balance": 12_345})).unwrap();
        assert_eq!(balance.available_cents(), 12_345);
        // $123.45 -> 1_234_500 ticks.
        assert_eq!(balance.available_ticks(), 1_234_500);
    }

    #[test]
    fn position_yes_long_reports_signed_contracts_and_seed_price() {
        let positions: PositionsResponse = serde_json::from_value(json!({
            "market_positions": [
                {"ticker": "KXTEST", "position": 40, "market_exposure": 2000}
            ]
        }))
        .unwrap();
        let p = &positions.market_positions[0];
        assert_eq!(p.ticker(), Some("KXTEST"));
        assert_eq!(p.signed_contracts(), Some(40));
        // $20.00 exposure / 40 contracts = $0.50 = 5_000 ticks/contract.
        assert_eq!(p.avg_price_ticks(), 5_000);
    }

    #[test]
    fn position_no_long_is_negative_contracts() {
        let p: KalshiPosition =
            serde_json::from_value(json!({"ticker": "KXTEST", "position": -15})).unwrap();
        assert_eq!(p.signed_contracts(), Some(-15));
    }
}
