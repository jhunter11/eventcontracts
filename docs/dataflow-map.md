# Dataflow Map

This document names the implementation path and the data type that
crosses each layer. After the Phase 1-3 vertical slice, every link
shown below has a real implementation that runs end-to-end.

## Raw Capture To Storage

Producer boundary:

- `eventcontracts.ingestion.CaptureSource.capture(job) -> Iterable[EventEnvelope]`
- `eventcontracts.ingestion.IterableCaptureSource` is the local/test
  producer; venue adapters in `eventcontracts.adapters.venues.*` are the
  production producers.

Persistence boundary:

- `eventcontracts.ingestion.IngestionPipeline.run(job) -> int`
- Input type: `IngestionJob`
- Stored type: `storage.EventEnvelope`
- Store ports:
  - `storage.InMemoryEventStore` (tests, local)
  - `storage.ParquetEventStore` (durable: partitions under `raw/venue=.../source=.../date=...`)

`EventEnvelope` is the raw persisted object. It carries
venue/source/channel, exchange time, receipt time, raw payload, schema
version, and metadata. Raw payloads are always stored before
normalization.

## Raw Storage To Normalized Events

Normalizer boundary:

- `normalization.EventNormalizer.normalize(raw) -> NormalizationResult`
- Input type: `storage.EventEnvelope`
- Output type: `domain.NormalizedEvent | None`

Parser functions:

- `normalization.normalize_trade(raw) -> TradeEvent`
- `normalization.normalize_quote(raw) -> QuoteEvent`
- `normalization.normalize_order_book(raw) -> OrderBookEvent`

Pipeline boundary:

- `normalization.NormalizationPipeline.run(source="*") -> tuple[NormalizationResult, ...]`
- Reads from: `storage.EventStore.read(source)`
- Writes to: `storage.NormalizedEventStore.append_normalized(event)`

## Replay To Strategy Runner

Raw replay:

- `replay.RawReplayEngine.replay() -> Iterator[EventEnvelope]`

Strategy replay:

- `replay.NormalizedReplaySource.stream() -> Iterator[NormalizedEvent]`
- Implements the runner `EventSource` port.
- Backed by either `InMemoryEventStore` or `ParquetEventStore` — same
  contract, deterministic ordering by `(exchange_ts, received_at, event_id)`.

Order book reconstruction (optional, for top-of-book venues):

- `replay.OrderBookReconstructor` maintains running ladders from quote
  and trade events.
- `replay.reconstruct_books(events)` is the generator that interleaves
  synthetic `OrderBookEvent` values into a stream.

Analytical read path:

- `storage.DuckDbEventStore` exposes the same Parquet partitions as SQL
  views (`raw_events`, `normalized_events`) for ad-hoc queries and
  parity case generation.

Runner handoff:

- `runner.StrategyRunner.run() -> RunSummary`
- Calls: `Strategy.on_event(event: NormalizedEvent, ctx: StrategyContext)`
- Strategy returns: `Sequence[StrategyDecision]`
- Runner wraps each decision as: `domain.IntentEnvelope`
- Strategy snapshots persist through `storage.FileStateStore` (binary
  blobs, one file per strategy id, atomic rename on write).

## Risk To Paper Execution

Risk boundary:

- `risk.SleeveRiskGate.evaluate(envelope, ctx) -> RiskDecision`
  - per-order notional, projected position notional, open-order count,
    sleeve gross exposure, daily realized loss, kill switch
- Stateful inputs: `risk.DailyLossLedger`, `risk.KillSwitch`

Execution translation:

- `execution.intent_to_order(envelope) -> OrderIntent | None`
- Input type: `domain.IntentEnvelope`
- Output type: `execution.OrderIntent`

Paper execution:

- `execution.MarketPaperSimulator.submit(intent, now) -> list[Fill]`
  - taker fills walk the opposite book
  - passive fills queue with a `QueuePositionEstimator`
  - cancel/replace, post-only, market lifecycle handling
- Fee model: venue-specific via `adapters.venues.<venue>.<Venue>FeeModel`
  (Kalshi, Polymarket).
- Latency: `execution.ConstantLatency` / `LognormalLatency` /
  `LookupLatency` — seeded for replay determinism.
- Queue position: `execution.DepthQueueEstimator`,
  `FractionalQueueEstimator`, `FrontOfQueueEstimator`.

Position keeping and PnL:

- `execution.PnLTracker` implements `FillSink`. Buys update weighted-
  average cost; sells realize PnL; quotes mark positions to mid.
- `execution.BacktestReport.from_run(summary, pnl)` aggregates the
  runner summary and PnL state into one serializable report
  (realized + unrealized PnL, drawdown, fill rate, fees,
  rejection-reason counts).

## Current Test Path

The Phase 1-3 vertical slice in `tests/test_end_to_end.py` runs:

```text
synthetic NormalizedEvent stream
  -> ParquetEventStore.append_normalized   (durable)
  -> DuckDbEventStore.normalized_count     (verifies partition layout)
  -> NormalizedReplaySource.stream         (replay, deterministic)
  -> StrategyRunner
       -> Strategy.on_event(event, ctx)
       -> IntentEnvelope
       -> SleeveRiskGate (limits + daily loss + kill switch)
       -> PaperIntentSink
            -> intent_to_order
            -> MarketPaperSimulator.submit
                 -> KalshiFeeModel
                 -> FractionalQueueEstimator
                 -> ConstantLatency
            -> Fill
            -> PnLTracker.on_fill
       -> RunSummary
  -> BacktestReport
```

`tests/test_determinism.py` runs the same slice twice and asserts the
serialized reports are byte-identical, which is the foundation for
the future Python/Rust parity job.
