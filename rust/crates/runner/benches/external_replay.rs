use criterion::{black_box, criterion_group, criterion_main, Criterion};
use eventcontracts_kalshi::{normalize_ws_payload, reset_sequence_tracking, KalshiWsEnvelope};
use eventcontracts_runner::{StrategyEvent, StrategyRuntime, ThresholdStrategy};
use time::macros::datetime;

const KALSHI_WS_FIXTURE: &str =
    include_str!("../../../../python/tests/fixtures/kalshi/ws_messages.jsonl");

fn parse_fixture() -> Vec<KalshiWsEnvelope> {
    KALSHI_WS_FIXTURE
        .lines()
        .filter(|line| !line.trim().is_empty())
        .map(|line| serde_json::from_str(line).expect("valid Kalshi WS fixture"))
        .collect()
}

fn bench_external_ws_replay(c: &mut Criterion) {
    let envelopes = parse_fixture();
    c.bench_function("external_kalshi_ws_normalize_strategy", |b| {
        b.iter(|| {
            reset_sequence_tracking();
            let mut strategy =
                ThresholdStrategy::new("bench-weather", "bench-sleeve", 0.40, 0.60, "10");
            let mut decisions = 0_usize;
            for envelope in &envelopes {
                let normalized =
                    match normalize_ws_payload(envelope, datetime!(2026-05-24 12:00 UTC)) {
                        Ok(event) => event,
                        Err(_) => continue,
                    };
                let event = match StrategyEvent::from_record(&normalized) {
                    Ok(event) => event,
                    Err(_) => continue,
                };
                decisions += strategy
                    .on_event(
                        &event,
                        &eventcontracts_runner::StrategyContext::from_sleeve_state(
                            "2026-05-24T12:00:00Z",
                            &eventcontracts_risk::SleeveState::default(),
                        ),
                    )
                    .expect("threshold strategy should not fail")
                    .len();
            }
            black_box(decisions);
        });
    });
}

criterion_group!(benches, bench_external_ws_replay);
criterion_main!(benches);
