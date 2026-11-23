---
title: "Runtime Summary Reads Hourly Rollups"
status: accepted
tags:
  - "api"
  - "clickhouse"
  - "runtime-telemetry"
---

## Context

`ClickHouseRuntime.summary()` serves both the Runtime UI and the 5-minute aggregation cycle. It ran seven queries over the whole aggregation window on every call, two of them self-joins of `otel_spans_full` (parent→child symbol edges and symbol→route). Its cost grew with the window and with traffic.

## Decision

Closed hours are rolled up once per revision into `runtime_span_rollups` (`api/Python-Service/app/telemetry/rollups.py`). The table is created by the same schema check that owns `otel_spans_full`.

- Engine: ReplacingMergeTree keyed by (project_id, revision_id, hour, kind, k1, k2, k3).
- Each row holds calls, failures, last_seen and a `quantile(0.95)` state.
- Row kinds:
  - `all`, keyed by sampling ratio
  - `symbol`
  - `route`
  - `table`
  - `symbol_table`
  - `parent`
  - `symbol_route`
  - `hour`: a marker written last
- An hour is rolled up only once it has been closed for 5 minutes, which covers the batch window and exporter retries.
- Joins count spans in the child's hour and look for the parent or server span from one hour earlier onward.
- `summary()` is one query: rolled-up hours (FINAL) UNION ALL the same kind SELECTs over raw spans of hours with no marker.
- The window is rounded down to whole hours.
- Deleting requests deletes the rollups of the affected hours, which are rolled up again on the next cycle. Revision removal deletes the table's rows like the other runtime tables.

## Alternatives Considered

- **Materialized views on insert:** the parent and route joins need spans from other insert batches, so a view cannot compute them.
- **AggregatingMergeTree with additive inserts:** a retried hour would double-count, and invalidating an hour after a delete would be awkward.

## Consequences

- Measured on Pizza Builder: one summary went from 9 queries / 197k rows / 33 MB to 3 queries / 573 rows / 0.05 MB. Output was identical to the previous implementation on both live revisions.
- Rollups make the summary eventually consistent for late spans: a span that arrives more than 5 minutes after its hour closed is not counted until that hour is invalidated.
- A new summary dimension needs a new kind, plus re-rolling existing hours (truncate the table; it rebuilds within one window).
