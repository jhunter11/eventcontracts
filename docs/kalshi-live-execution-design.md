# Kalshi Live Execution (VenueClient) Design

## Overview
To transition from the `DryRunGateway` to real-money execution, the Rust runner requires a concrete implementation of the `VenueClient` trait tailored for the Kalshi V2 Trade API. This document outlines the architecture, authentication, and error-handling requirements for `KalshiVenueClient`.

## Architecture
The `KalshiVenueClient` will sit behind the `eventcontracts_gateway::Gateway` and map `DecisionPayload` intents into Kalshi REST HTTP requests. 

### Trait Implementation
```rust
#[async_trait]
pub trait VenueClient {
    async fn submit_order(&self, intent: &IntentSnapshot) -> Result<VenueOrderId, GatewayError>;
    async fn cancel_order(&self, client_order_id: &str) -> Result<bool, GatewayError>;
    // Note: Kalshi V2 supports order replacement via `decrease_by` or cancel+replace.
    async fn replace_order(&self, intent: &IntentSnapshot) -> Result<VenueOrderId, GatewayError>;
}
```

## Authentication
Kalshi's algorithmic API endpoints require ECDSA signature authentication (RSA is deprecated/legacy for some endpoints).
1. **Credentials:** The client will require `KALSHI_KEY_ID` and the ECDSA private key file path.
2. **Request Signing:** Every request must include:
   - `KALSHI-ACCESS-KEY`: The Key ID.
   - `KALSHI-ACCESS-SIGNATURE`: A base64 encoded signature of the timestamp + method + path.
   - `KALSHI-ACCESS-TIMESTAMP`: Current Unix timestamp in milliseconds.

## Payload Mapping
The Rust `IntentSnapshot` uses `OutcomeSide` (Yes/No) and `Side` (Buy/Sell). Kalshi V2 expects `action` ("buy"/"sell") and `side` ("yes"/"no") or explicit `yes_price`/`no_price`.

Mapping Logic for `POST /trade-api/v2/portfolio/orders`:
- `ticker`: Maps to `intent.instrument_id` (strip the "kalshi:" prefix if present).
- `action`: "buy" if `intent.side == Buy`, else "sell".
- `type`: "limit" (market orders should be avoided for live execution).
- `client_order_id`: Maps directly to `intent.client_order_id` (enforces idempotency).
- `count`: Convert the `i64` fixed-point quantity to integer contracts.
- **Price Translation:** If `intent.outcome_side` is `Yes`, pass `yes_price = intent.price.ticks() / 10,000` (cents). If `No`, pass `no_price`.

## Idempotency & Rate Limiting
- **Idempotency:** Kalshi respects `client_order_id`. If a network timeout occurs and we retry with the same `client_order_id`, Kalshi will return the existing order rather than double-filling. The `KalshiVenueClient` MUST pass this field.
- **Rate Limiting:** Kalshi enforces strict rate limits (e.g., 10 requests/second for standard tiers). The client must wrap the `reqwest::Client` with a token bucket rate limiter (e.g., `governor` crate) to prevent `429 Too Many Requests` bans.

## Error Handling
Map Kalshi HTTP statuses to `GatewayError`:
- `400 Bad Request`: `GatewayError::Rejected` (e.g., invalid price increment).
- `403 Forbidden`: `GatewayError::AuthFailed`.
- `422 Unprocessable Entity`: Check for "Insufficient funds" -> `GatewayError::Rejected`.
- `429 Too Many Requests`: `GatewayError::RateLimited` (trigger backoff).
- `5xx Server Error`: `GatewayError::VenueOffline`.
