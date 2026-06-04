# Entertainment Awards — Parity Fixture (placeholder)

`entertainment-awards-v1` runs on the generic `external_edge` archetype
(Python: `plugins/strategies/external_edge.py`; Rust: the `external_edge`
registry fallback). It is **paper-mode**, so no parity set is required to run
`ec live-paper`.

This directory is a placeholder. Before the sleeve can graduate from paper to
dry-run-live, generate `(event_id, decision)` parity cases by replaying a
captured awards window through Python and recording the decisions, then diff the
same cases through `eventcontracts-parity` (see `docs/strategy-promotion.md` and
`docs/v7-live-test-ready-strategy-specs.md` §2).

Until the fixture exists, both loaders treat its absence as "no parity coverage
yet", not an error. The eventual Parquet schema is in
`../../schemas/parity_cases.parquet.schema.md`.
