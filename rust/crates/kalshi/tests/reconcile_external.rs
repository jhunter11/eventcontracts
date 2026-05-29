use eventcontracts_contracts::{AuditStamp, IntentEnvelopeRecord, Metadata};
use eventcontracts_gateway::{
    DecisionPayload, DryRunGateway, OutcomeSide, PortfolioGuard, PortfolioPolicy,
    RecordingVenueClient,
};
use eventcontracts_kalshi::KalshiOrder;
use eventcontracts_oms::{Side, TimeInForce};
use eventcontracts_risk::{RiskGate, RiskLimits};
use serde::Deserialize;

const RESTING_ORDERS_FIXTURE: &str =
    include_str!("../../../../contracts/replay/kalshi/resting_orders.json");

#[derive(Deserialize)]
struct RestingOrdersFixture {
    orders: Vec<KalshiOrder>,
}

#[test]
fn external_resting_order_adoption_feeds_portfolio_admission() {
    let fixture: RestingOrdersFixture =
        serde_json::from_str(RESTING_ORDERS_FIXTURE).expect("valid resting order fixture");
    let snapshot = fixture.orders[0]
        .to_resting_snapshot("2026-05-24T12:00:02Z")
        .expect("fixture should be adoptable");

    let mut gateway = DryRunGateway::new(RiskGate::new(limits()), RecordingVenueClient::new());
    gateway.portfolio =
        PortfolioGuard::new(PortfolioPolicy::enabled("4.00").expect("valid portfolio cap"));
    gateway.adopt_resting_order(snapshot).unwrap();
    gateway
        .sleeve_state
        .mark_price_ticks
        .insert("kalshi:KXHIGHNY-26MAY24-B75".into(), 3500);

    gateway
        .enqueue(place_intent(
            "corr-portfolio-reject",
            "client_fixture_002",
            "0.35",
            "2",
        ))
        .unwrap();
    let result = gateway.process_batch("2026-05-24T12:00:02Z", 1);
    assert!(matches!(
        result[0].1.as_ref().unwrap_err(),
        eventcontracts_gateway::GatewayError::PortfolioRejected(_)
    ));
    assert_eq!(gateway.venue.submitted.len(), 0);
}

fn place_intent(
    correlation_id: &str,
    client_order_id: &str,
    price: &str,
    quantity: &str,
) -> IntentEnvelopeRecord {
    let payload = DecisionPayload::PlaceOrder {
        client_order_id: client_order_id.into(),
        instrument_id: "kalshi:KXHIGHNY-26MAY24-B75".into(),
        outcome_side: OutcomeSide::Yes,
        side: Side::Buy,
        price: price.into(),
        quantity: quantity.into(),
        fair_price: None,
        min_executable_edge_ticks: None,
        fee_rate_bps: None,
        time_in_force: TimeInForce::Ioc,
    };
    IntentEnvelopeRecord {
        strategy_id: "fixture-strategy".into(),
        sleeve_id: "fixture-sleeve".into(),
        correlation_id: correlation_id.into(),
        emitted_at: "2026-05-24T12:00:02Z".into(),
        decision_kind: "place_order".into(),
        decision_json: serde_json::to_string(&payload).unwrap(),
        priority_tier: "standard".into(),
        audit: AuditStamp {
            object_id: correlation_id.into(),
            object_kind: "intent".into(),
            schema_version: "intent-envelope-v1".into(),
            produced_at: "2026-05-24T12:00:02Z".into(),
            producer: "external-fixture".into(),
            canonical_sha256: "c".repeat(64),
            parent_ids: vec![],
            trace_id: None,
            metadata: Metadata::new(),
        },
    }
}

fn limits() -> RiskLimits {
    RiskLimits {
        max_order_notional: "500".into(),
        max_position_notional: "2500".into(),
        max_daily_loss: "250".into(),
        max_open_orders: 10,
        max_gross_exposure: "5000".into(),
        currency: "USD".into(),
        max_market_data_age_secs: 30,
    }
}
