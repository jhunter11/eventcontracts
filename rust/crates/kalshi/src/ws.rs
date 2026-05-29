//! Authenticated Kalshi WebSocket subscriber. Read-only — this client never
//! sends a non-`subscribe`/`unsubscribe` frame.

use crate::auth::KalshiAuth;
use futures_util::{SinkExt, StreamExt};
use serde::{Deserialize, Serialize};
use serde_json::value::RawValue;
use std::sync::atomic::{AtomicI64, Ordering};
use std::time::{Duration, Instant};
use thiserror::Error;
use tokio::net::TcpStream;
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::http::HeaderValue;
use tokio_tungstenite::tungstenite::protocol::Message;
use tokio_tungstenite::{connect_async_tls_with_config, MaybeTlsStream, WebSocketStream};

const WS_SIGNING_PATH: &str = "/trade-api/ws/v2";

#[derive(Debug, Error)]
pub enum WsError {
    #[error("auth signing failed: {0}")]
    Sign(#[from] crate::auth::SignError),
    #[error("tungstenite error: {0}")]
    Tungstenite(#[from] tokio_tungstenite::tungstenite::Error),
    #[error("json error: {0}")]
    Json(#[from] serde_json::Error),
    #[error("ws closed by peer")]
    Closed,
    #[error("invalid header value: {0}")]
    InvalidHeader(#[from] tokio_tungstenite::tungstenite::http::header::InvalidHeaderValue),
}

#[derive(Debug, Clone, Serialize)]
pub struct SubscribeCommand {
    pub id: i64,
    pub cmd: &'static str,
    pub params: SubscribeParams,
}

#[derive(Debug, Clone, Serialize)]
pub struct SubscribeParams {
    pub channels: Vec<String>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub market_tickers: Vec<String>,
}

/// Raw envelope as parsed off the wire — kept verbatim so the normalizer can
/// inspect `type`, `sid`, `seq`, and the channel-specific `msg`.
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct KalshiWsEnvelope {
    #[serde(rename = "type")]
    pub msg_type: String,
    #[serde(default)]
    pub sid: Option<i64>,
    #[serde(default)]
    pub seq: Option<i64>,
    #[serde(default)]
    pub msg: Option<Box<RawValue>>,
    #[serde(default)]
    pub id: Option<i64>,
}

type WsStream = WebSocketStream<MaybeTlsStream<TcpStream>>;

pub struct KalshiWsClient {
    pub ws_url: String,
    pub auth: KalshiAuth,
    next_id: AtomicI64,
    stream: Option<WsStream>,
    last_message_at: Option<Instant>,
    idle_timeout: Duration,
}

impl KalshiWsClient {
    pub fn new(ws_url: impl Into<String>, auth: KalshiAuth) -> Self {
        Self {
            ws_url: ws_url.into(),
            auth,
            next_id: AtomicI64::new(1),
            stream: None,
            last_message_at: None,
            idle_timeout: Duration::from_secs(20),
        }
    }

    pub async fn connect(&mut self) -> Result<(), WsError> {
        let signed = self.auth.sign("GET", WS_SIGNING_PATH)?;
        let mut req = self.ws_url.as_str().into_client_request()?;
        for (k, v) in signed.as_pairs() {
            req.headers_mut().insert(k, HeaderValue::from_str(&v)?);
        }
        let (stream, _resp) = connect_async_tls_with_config(req, None, false, None).await?;
        self.stream = Some(stream);
        self.last_message_at = Some(Instant::now());
        Ok(())
    }

    pub async fn subscribe(
        &mut self,
        channels: &[&str],
        market_tickers: &[&str],
    ) -> Result<(), WsError> {
        let cmd = SubscribeCommand {
            id: self.next_id.fetch_add(1, Ordering::Relaxed),
            cmd: "subscribe",
            params: SubscribeParams {
                channels: channels.iter().map(|s| (*s).to_string()).collect(),
                market_tickers: market_tickers.iter().map(|s| (*s).to_string()).collect(),
            },
        };
        let stream = self.stream.as_mut().ok_or(WsError::Closed)?;
        let text = serde_json::to_string(&cmd)?;
        stream.send(Message::Text(text)).await?;
        Ok(())
    }

    /// Returns the next decoded envelope, or `Ok(None)` on a non-text frame
    /// (ping/pong/binary/close).
    pub async fn next_envelope(&mut self) -> Result<Option<KalshiWsEnvelope>, WsError> {
        let stream = self.stream.as_mut().ok_or(WsError::Closed)?;
        loop {
            let item = match tokio::time::timeout(self.idle_timeout, stream.next()).await {
                Ok(item) => item,
                Err(_) => {
                    stream.send(Message::Ping(Vec::new())).await?;
                    match tokio::time::timeout(self.idle_timeout, stream.next()).await {
                        Ok(item) => item,
                        Err(_) => return Err(WsError::Closed),
                    }
                }
            };
            match item {
                Some(Ok(Message::Text(text))) => {
                    self.last_message_at = Some(Instant::now());
                    let env: KalshiWsEnvelope = serde_json::from_str(&text)?;
                    return Ok(Some(env));
                }
                Some(Ok(Message::Binary(_))) => {
                    self.last_message_at = Some(Instant::now());
                    continue;
                }
                Some(Ok(Message::Ping(p))) => {
                    self.last_message_at = Some(Instant::now());
                    stream.send(Message::Pong(p)).await?;
                    continue;
                }
                Some(Ok(Message::Pong(_))) => {
                    self.last_message_at = Some(Instant::now());
                    continue;
                }
                Some(Ok(Message::Frame(_))) => continue,
                Some(Ok(Message::Close(_))) => return Err(WsError::Closed),
                Some(Err(e)) => return Err(e.into()),
                None => return Err(WsError::Closed),
            }
        }
    }

    pub async fn close(&mut self) -> Result<(), WsError> {
        if let Some(mut stream) = self.stream.take() {
            let _ = stream.close(None).await;
        }
        Ok(())
    }
}
