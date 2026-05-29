use criterion::{black_box, criterion_group, criterion_main, Criterion};
use std::time::Instant;
use tokio::runtime::Runtime;
use tokio::sync::mpsc;
use tokio::time::{sleep, Duration};

const EVENTS: usize = 1_000;
const CHANNEL_CAPACITY: usize = 1_024;

fn ws_lag_under_load(c: &mut Criterion) {
    let runtime = Runtime::new().expect("tokio runtime");
    c.bench_function("ws_lag_under_load", |b| {
        b.iter(|| {
            let p99_lag_us = runtime.block_on(simulate_ws_lag());
            black_box(p99_lag_us);
        });
    });
}

async fn simulate_ws_lag() -> u64 {
    let (tx, mut rx) = mpsc::channel::<Instant>(CHANNEL_CAPACITY);
    let producer = tokio::spawn(async move {
        for _ in 0..EVENTS {
            if tx.send(Instant::now()).await.is_err() {
                break;
            }
            tokio::task::yield_now().await;
        }
    });

    let consumer = tokio::spawn(async move {
        let mut lags = Vec::with_capacity(EVENTS);
        while let Some(sent_at) = rx.recv().await {
            lags.push(sent_at.elapsed().as_micros() as u64);
            if lags.len() % 100 == 0 {
                sleep(Duration::from_millis(2)).await;
            }
            if lags.len() >= EVENTS {
                break;
            }
        }
        lags
    });

    let _ = producer.await;
    let mut lags = consumer.await.expect("consumer task");
    if lags.is_empty() {
        return 0;
    }
    lags.sort_unstable();
    let idx = ((lags.len() - 1) as f64 * 0.99).round() as usize;
    lags[idx]
}

criterion_group!(benches, ws_lag_under_load);
criterion_main!(benches);
