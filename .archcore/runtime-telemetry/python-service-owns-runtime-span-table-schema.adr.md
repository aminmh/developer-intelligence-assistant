---
title: "Python Service Owns the Runtime Span Table Schema"
status: accepted
tags:
  - "api"
  - "clickhouse"
  - "runtime-telemetry"
---

## Context

The Collector's ClickHouse exporter created `otel_spans_full` itself (`create_schema: true`). Its DDL sorts by `(ServiceName, SpanName, Timestamp)`, but the privacy transform renames every span to `runtime.span`, so the key carries no information. Every Runtime query filtered project and revision through `ResourceAttributes[...]` Map lookups on every row of the window. The exporter's TTL was 7 days instead of the configured 72 h, because the table pre-dated the `ttl` setting and the exporter never alters an existing table. It also maintained a trace-ID lookup table and materialized view that nothing reads.

## Decision

The Python service owns the full-span table DDL (`api/Python-Service/app/telemetry/span_schema.py`). The Collector's `clickhouse/full` exporter runs with `create_schema: false` and only inserts.

- Columns: the exporter's columns, unchanged, so its inserts keep working. Added `MATERIALIZED` columns: `ProjectId`, `RevisionId`, `Eligible` (not dirty and schema `indexer-telemetry-v2`), `SymbolId`, `HttpRoute`.
- `ORDER BY (ProjectId, RevisionId, SpanKind, Timestamp)`, `PARTITION BY toDate(Timestamp)`, a TraceId bloom filter, and a TTL of `aggregation_retention_hours`, with `ttl_only_drop_parts`.
- Version is recorded in the table comment (`devassistant-runtime-spans:v2`).
- The telemetry loop ensures the schema on the active storage target (Local or Remote) every 30 s. The check is cached per target and retention, and policy refresh and aggregation wait until it succeeds.
- An older table is migrated in place:
  1. Drop the exporter's trace-ID view and table.
  2. Copy the rows inside the retention window into a staging table.
  3. `EXCHANGE TABLES`.
  4. Copy the spans that arrived during the copy.
  5. Drop the old table.
- A retention change runs `ALTER TABLE … MODIFY TTL` without rewriting parts.
- Queries scope with `ProjectId = … AND RevisionId = … AND Eligible`.

## Alternatives Considered

- Keep the exporter's DDL and add data-skipping indexes: the sort key would still be useless and the TTL drift would remain.
- Pre-create the table and keep `create_schema: true` as a fallback: on a fresh volume the Collector could create the old shape first. It would also keep re-creating the unused trace-ID view.
- Migrate with an external script: Remote targets would need the same step by hand. The Python loop already reaches whichever target is active.

## Consequences

- If the Collector starts before the Python service on a fresh volume, its inserts fail and retry until the table exists (up to the exporter's 30 s retry window). Spans older than that are dropped.
- Schema changes are now versioned code: bump `SCHEMA_VERSION` and extend the migration.
- `otel_traces` and span-metric tables (Remote mode only) remain exporter-owned.
- Measured on Pizza Builder, about 27k spans across two revisions:
  - Summary bytes read: 117 MB → 33 MB.
  - Aggregation cycle: 130 MB → 38 MB.
  - Request detail: 1.85 MB → 0.22 MB.
  - Policy refresh: 3.0 MB → 0.23 MB.
  - Rows-read savings grow with the number of projects and revisions in the table.
