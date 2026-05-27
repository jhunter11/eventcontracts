//! Authenticated Kalshi REST client. Read-only by design — submit/cancel
//! endpoints intentionally don't exist here.

use crate::auth::KalshiAuth;
use reqwest::{Client, ClientBuilder};
use serde::Deserialize;
use thiserror::Error;
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

pub struct KalshiRest {
    client: Client,
    base_url: String,
    auth: KalshiAuth,
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
}
