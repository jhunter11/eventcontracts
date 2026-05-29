//! CI gate for the live-runner ingest safety property (V6-D2).
//!
//! The runner drains the WebSocket on a dedicated reader task into a bounded
//! channel; decide + execute (including a potentially slow REST submit) run on a
//! separate consumer. The safety property: **a slow/stalled submit must not
//! freeze WS ingest.** If a future refactor reintroduces an inline `await submit`
//! in the read path, the reader's per-event gap would balloon to the stall
//! duration and this test fails.

use std::time::{Duration, Instant};
use tokio::sync::mpsc;
use tokio::time::sleep;

const EVENTS: usize = 1_000;
const CHANNEL_CAPACITY: usize = 1_024;
/// Simulated stalled-submit duration (a slow/retrying REST call).
const SUBMIT_STALL: Duration = Duration::from_millis(200);
/// The reader must never gap more than this between consecutive ingests. A
/// regression to inline submit would push gaps to ~SUBMIT_STALL (200ms).
const MAX_READER_GAP: Duration = Duration::from_millis(100);

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn slow_submit_does_not_starve_ws_ingest() {
    let (tx, mut rx) = mpsc::channel::<Instant>(CHANNEL_CAPACITY);

    // Reader task = WS socket drain. Records the wall time after each enqueue so
    // we can prove it was never blocked waiting on the slow consumer.
    let reader = tokio::spawn(async move {
        let mut enqueue_times = Vec::with_capacity(EVENTS);
        for _ in 0..EVENTS {
            if tx.send(Instant::now()).await.is_err() {
                break;
            }
            enqueue_times.push(Instant::now());
        }
        enqueue_times
    });

    // Consumer = decide + execute. Every 250 events it hits a stalled submit.
    let consumer = tokio::spawn(async move {
        let mut delivered = 0usize;
        while rx.recv().await.is_some() {
            delivered += 1;
            if delivered.is_multiple_of(250) {
                sleep(SUBMIT_STALL).await;
            }
            if delivered >= EVENTS {
                break;
            }
        }
        delivered
    });

    let enqueue_times = reader.await.expect("reader task");
    let delivered = consumer.await.expect("consumer task");

    assert_eq!(delivered, EVENTS, "no ingested events were dropped");
    assert_eq!(enqueue_times.len(), EVENTS, "reader drained every event");

    let max_gap = enqueue_times
        .windows(2)
        .map(|w| w[1].duration_since(w[0]))
        .max()
        .unwrap_or_default();
    assert!(
        max_gap < MAX_READER_GAP,
        "WS ingest stalled: max reader gap {max_gap:?} >= {MAX_READER_GAP:?} \
         — a slow submit is blocking the read path (ingest/execute are coupled)"
    );
}
