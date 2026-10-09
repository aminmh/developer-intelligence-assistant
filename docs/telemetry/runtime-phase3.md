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

Runtime also lists up to 50 sampled server requests from the selected project's
immutable revision and aggregation window. Expanding a request loads its trace
through a revision-scoped query and returns only the request span and database
spans with safe operation, system, namespace, table, and symbol attributes.
SQL text and bindings are not returned. Requests without matching DB spans
show an empty database-call list; unavailable fields remain unavailable for
older spans or unsupported PHP runtimes.

Request spans may include the PHP worker's user-plus-system CPU time delta
(microseconds) and PHP allocator peak above request-start memory (bytes). These
are worker-level estimates, not container CPU or resident memory. Request
sampling may omit traffic, and callers need project access to read the details.
The Runtime view can hide the currently loaded request rows without changing
storage. Project administrators can permanently delete the stored spans for up
to 50 currently listed request traces; this is revision-scoped, includes child
database spans, and reduces the aggregates computed from those spans. The UI
requires confirmation before deletion.

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

The API, web, PHP library, and custom Runtime Collector expose bounded,
revision-scoped request details with database calls and PHP worker estimates.
The production PHP SDK sampler remains at its existing 10% default; changing
it to 100% was not included.
