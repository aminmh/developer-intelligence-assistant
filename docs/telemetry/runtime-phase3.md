# Runtime telemetry pipeline and aggregation

Phase 3 joins Collector processing, project and immutable-revision scoped rollups, the
`PhpSymbol` graph, exception search, and live UI invalidation.

## Data path

1. The PHP SDK sends OTLP spans to the runtime Collector. Its resource identity
   must include `indexer.project_id`, the app-generated `indexer.revision_id`,
   `indexer.code_dirty=false`, and
   `indexer.schema.version=indexer-telemetry-v2`.
2. The Collector removes unapproved resource and span attributes. It writes the
   spans it received before tail sampling to `otel_spans_full`; `spanmetrics`
   derives count and duration metrics from that same input.
3. A second trace pipeline applies the project's rolling p95 threshold and
   tail-sampling policy, then writes retained traces to `otel_traces`.
4. The API periodically reads the pre-tail table over the configured rolling
   window. It updates runtime properties and `RUNTIME_CALLS`, `HANDLED_BY`, and
   `QUERIES` facts on the indexed `PhpSymbol` snapshot identified by its
   revision, and stores normalized exception signatures in the
   `runtime_exceptions` Qdrant collection.
5. The API publishes a project-free `runtime.updated` SSE invalidation after
   deployment reports and aggregation. Runtime, Definitions, and Schema views
   reload their data when they receive this event; they do not use a UI polling
   timer.

The Collector table is called `otel_spans_full` for compatibility. “Full” means
before Collector tail sampling. PHP SDK head sampling happens before the
Collector and may already have reduced the input. Therefore the pre-tail
aggregates are counts of spans received, not guaranteed counts of every
request. Error and slow-trace retention only applies to traces that reach the
Collector.

## Sampling controls and retention

- The rolling p95 window defaults to 24 hours, bounded by
  `TELEMETRY_AGGREGATION_RETENTION_HOURS` (default 168 hours).
- The Collector policy refresh defaults to 5 minutes. The Runtime telemetry
  settings card can change the p95 window and refresh interval.
- `TELEMETRY_RECORDING_PERCENT` controls the Collector's baseline tail sample
  after upstream SDK sampling. It defaults to 10%.
- `TELEMETRY_RETENTION_HOURS` controls retained tail-sampled traces in
  `otel_traces`. `TELEMETRY_AGGREGATION_RETENTION_HOURS` controls the pre-tail
  table used for rollups and p95 policy generation.

## Implementation status

The API, web, PHP library, and custom runtime Collector changes are implemented
on their Phase 3 feature branches. Automated tests, lint, Collector build,
production build, and repository diff checks are intentionally pending the
user's confirmation to begin verification. The production PHP SDK sampler
remains at its existing 10% default; changing it to 100% was not included.
