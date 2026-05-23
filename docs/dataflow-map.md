# Dataflow Map

This document names the current partial implementation path and the data type
that crosses each layer. It is intentionally narrow: these functions exist to
make the first end-to-end loop testable before real venue clients, data lake
writers, and market-realistic execution are implemented.

## Raw Capture To Storage

Producer boundary:

- `eventcontracts.ingestion.CaptureSource.capture(job) -> Iterable[EventEnvelope]`
- `eventcontracts.ingestion.IterableCaptureSource` is the local/test producer.

Persistence boundary:

- `eventcontracts.ingestion.IngestionPipeline.run(job) -> int`
- Input type: `IngestionJob`
- Stored type: `storage.EventEnvelope`
- Store port: `storage.EventStore.append(event)`

`EventEnvelope` is the raw persisted object. It carries venue/source/channel,
exchange time, receipt time, raw payload, schema version, and metadata. Raw
payloads should be stored before normalization.

## Raw Storage To Normalized Events

Normalizer boundary:

- `normalization.EventNormalizer.normalize(raw) -> NormalizationResult`
- Input type: `storage.EventEnvelope`
- Output type: `domain.NormalizedEvent | None`

Current parser functions:

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

Runner handoff:

- `runner.StrategyRunner.run()`
- Calls: `Strategy.on_event(event: NormalizedEvent, ctx: StrategyContext)`
- Strategy returns: `Sequence[StrategyDecision]`
- Runner wraps each decision as: `domain.IntentEnvelope`

## Risk To Paper Execution

Risk boundary:

- `risk.SleeveRiskGate.evaluate(envelope, ctx) -> RiskDecision`
- Input type: `domain.IntentEnvelope`
- Context type: `strategy.StrategyContext`

Execution translation:

- `execution.intent_to_order(envelope) -> OrderIntent | None`
- Input type: `domain.IntentEnvelope`
- Output type: `execution.OrderIntent`

Paper execution:

- `execution.PaperIntentSink.emit(envelope) -> None`
- `execution.PaperBroker.submit(order) -> list[SimulatedFill]`
- `execution.ImmediateFillSimulator.submit(order) -> list[SimulatedFill]`

`ImmediateFillSimulator` is a deterministic placeholder. It fills at the order
price with zero fees so the data path is executable while fee, queue, slippage,
latency, and lifecycle models are still pending.

## Current Test Path

`tests/test_vertical_dataflow.py` runs:

```text
EventEnvelope
  -> IngestionPipeline
  -> InMemoryEventStore.raw_events
  -> EventNormalizer / NormalizationPipeline
  -> InMemoryEventStore.normalized_events
  -> NormalizedReplaySource
  -> StrategyRunner
  -> IntentEnvelope
  -> SleeveRiskGate
  -> PaperIntentSink
  -> OrderIntent
  -> PaperBroker
  -> SimulatedFill
```
