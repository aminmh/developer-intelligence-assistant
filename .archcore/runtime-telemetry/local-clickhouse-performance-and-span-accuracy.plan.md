---
title: "Improve Local ClickHouse Performance and Traced-Span Accuracy"
status: accepted
tags:
  - "api"
  - "runtime-telemetry"
---

## Goal

Reduce ClickHouse, Collector and instrumented-app overhead in **Local** Runtime mode, and make traced spans describe the request that actually ran: correct timing boundaries, correct parent/child structure, and correct symbol attribution. Scope covers the Collector config, the ClickHouse schema and queries in the Python service, and the `indexr-otel-library` packages (core, Laravel, Symfony).

## Findings (evidence)

### Local ClickHouse performance

- F1. Two write-only pipelines. The Runtime UI, aggregation and p95 policy all read `otel_spans_full` (`self.table`, @api/Python-Service/app/telemetry/runtime.py:213). `otel_traces` (sampled pipeline) is only referenced by revision deletion; span-metrics tables have no reader. Tail sampling (50k-trace buffer), p95 processor and two ClickHouse exporters run for data nobody queries.
- F2. Duplicate processing. `traces/sampled` and `traces/full` each run `memory_limiter`, `filter/runtime_privacy` and `transform/runtime_privacy` on every span (@api/telemetry-dev/collector-runtime.yaml:130-137).
- F3. Small synchronous inserts. `batch` is 128 spans / 1 s and `async_insert: false` across three exporters, which creates many small parts and constant merge work on a 4-thread background pool (@api/telemetry-dev/clickhouse-runtime.xml:3).
- F4. Sort key does not match queries. Tables are exporter-created (`create_schema: true`). Every query filters on `ResourceAttributes['indexer.project_id']`, `['indexer.revision_id']`, `['indexer.code_dirty']`, `['indexer.schema.version']` (runtime.py:18-21), which are Map lookups with no index. The transform also sets every span name to `runtime.span`, so a `SpanName`-based sort key prefix carries no information. [assumption: default exporter DDL in v0.136.0 orders by `ServiceName, SpanName, Timestamp`; confirm with `SHOW CREATE TABLE`.]
- F5. Full-window recompute every 5 minutes. `aggregate_runtime` (main.py:159-168) runs `summary()` per ready revision: about 7 queries over the whole 24 h window, including two self-joins of `SpanAttributes` maps (runtime.py:624-641), even when no new spans arrived.
- F6. Heavy deletes. Request deletion uses `ALTER TABLE … DELETE` with `mutations_sync=1` (runtime.py:553), which rewrites whole parts.
- F7. Cumulative span metrics with `symbol_id × caller_symbol_id` dimensions re-emit every series every 60 s into a table that has no reader.

### Traced-span accuracy (indexr-otel-library)

- A1. PHP 8.4 breaks caller attribution. PHP 8.4 names closure frames `{closure:App\A::f():1}` (verified locally on 8.4.26). `CallerAttributor` compares against `'{closure}'` (CallerAttributor.php:31). An app-class closure is then normalized as a method, the normalizer throws, and the outer catch returns null for **both** symbol and caller.
- A2. Vendor closures win attribution. `closureSymbol` accepts any file under the project root, including `vendor/`. Eloquent paths such as `Query\Builder::get → onceWithColumns(closure)` put a vendor closure ahead of the app frame (PHP < 8.4), so DB spans get attributed to framework code.
- A3. Wrong root in the span processor. `SpanSymbolIdProcessor` uses `getcwd()` as the project root (SpanSymbolIdProcessor.php:47). Under FPM that is usually `public/`, so closure symbols are rejected.
- A4. Instrumentation inflates DB durations. Each Laravel query takes up to three `debug_backtrace` passes (processor `onStart`, `SqlSpanRunner`, `QueryExecuted` listener). `QueryExecuted` fires inside `Connection::run`, so listener cost is counted in the `db.query` span.
- A5. Fallback mode corrupts the root span. Without `ext-opentelemetry` there is no DB span. The `QueryExecuted` listener then writes `indexer.table_ids` and `indexer.caller.symbol_id` onto the active `http.server` span, last query wins, and aggregation links the controller to the wrong tables.
- A6. The server span starts late. Laravel opens `http.server` at `RouteMatched`; Symfony at `kernel.controller`. Bootstrap, global middleware, routing and the Symfony firewall are excluded from duration, CPU and memory. 404/405/early-middleware errors produce no server span. Doctrine queries in the firewall run with no active parent and become separate root traces.
- A7. Symfony requests are invisible. `KernelSubscriber` never sets `http.route`, `http.request.method` or `http.response.status_code`. The request list requires `http.route != ''` (runtime.py:256), and 5xx responses without an exception are never marked as errors.
- A8. Laravel job spans leak on retry. Only `JobProcessed`/`JobFailed` close the span. A job that throws and is released for retry fires neither, so its span stays active and the next job is nested in the previous job's trace.
- A9. The route ID uses the first declared method (`$methods[0]`) instead of the request method, in both adapters. `Route::match(['PUT','PATCH'])` records PATCH traffic as PUT.
- A10. No context propagation. Incoming `traceparent` is not extracted, and queued jobs do not carry the dispatcher's context, so cross-service and request→job causality is lost.
- A11. Silent sampling and truncation. With `APP_ENV=production` and no `OTEL_TRACES_SAMPLER`, head sampling is 10% (TelemetryBootstrap.php:79-81). Counts, error rates and p95 are then computed on a sample that is not labelled as one. The BSP queue (2048), the request-detail `LIMIT 500` and the hook-edge caps drop data without a flag.
- A12. Information removed at the Collector. All span names become `runtime.span`; `error.type` and the status message are stripped. Span type is inferred only from kind and attributes, and spans of the same kind cannot be told apart.
- A13. The slow-trace threshold is per project. p95 is computed per project across all routes (policy.py:22-31), so a regression on a fast route never crosses the threshold.
- A14. Dead attributes. The cache listener overwrites `indexer.cache.operation` with the last operation, and the Collector drops it anyway. Pure overhead.

## Tasks

### Phase 1 — Collector and ClickHouse (local mode)

1. Decide whether the request list reads `otel_traces` or `otel_spans_full`, and record it as an ADR.
2. In Local mode, disable the sampled pipeline and span metrics when nothing reads them.
3. Add the `forward` connector to @api/telemetry-dev/collector-builder.yaml and rebuild the runtime Collector image.
4. Run memory limit, privacy filter and transform once in an ingest pipeline, then fan out by connector.
5. Raise batching to roughly 5,000 spans or 5 s, and measure parts per minute before and after.
6. Evaluate `async_insert=1` with `wait_for_async_insert=1` as an alternative to larger client batches.
7. Set `create_schema: false` and own versioned DDL for `otel_spans_full` in the Python service.
8. Add materialized `ProjectId`, `RevisionId`, `Eligible`, `SymbolId`, `HttpRoute` columns from the resource and span maps.
9. Order the table by `(ProjectId, RevisionId, SpanKind, Timestamp)`, partition by day, add a TraceId bloom index.
10. Rewrite `_SCOPE`, policy, profile and summary queries to filter on the materialized columns.
11. Skip a revision's aggregation when `max(Timestamp)` has not advanced since the previous run.
12. Replace the self-joins with materialized views into hourly `AggregatingMergeTree` rollups per revision.
13. Make `summary()` read the rollups so its cost no longer grows with the 24 h window.
14. Replace `ALTER TABLE … DELETE` with lightweight `DELETE FROM` for request deletion.
15. Add a migration that copies or expires existing tables without truncating the named volume.

### Phase 2 — Span boundaries and structure (indexr-otel-library)

16. Start Laravel `http.server` from an early global middleware, with its start time taken from `REQUEST_TIME_FLOAT`.
17. Start Symfony `http.server` on high-priority `kernel.request`, with its start time taken from `REQUEST_TIME_FLOAT`.
18. Set route, controller symbol and route ID later, at `RouteMatched` / `kernel.controller`.
19. Set `http.route`, `http.request.method` and `http.response.status_code` in the Symfony adapter.
20. Mark the server span as error for any 5xx status, with or without an exception event.
21. Emit server spans for unmatched routes with an `http.route` placeholder, so 404/405 storms stay visible.
22. Take CPU and memory baselines at server-span start, not at route match.
23. Extract incoming W3C `traceparent` before starting the server span.
24. Propagate trace context into Laravel queue payloads and Symfony Messenger stamps.
25. Close Laravel job spans on `JobExceptionOccurred` and `JobReleasedAfterException`.
26. Finish any open job span before pushing a new one on `JobProcessing`.
27. Use the actual request method for the route ID when it is among the route's methods.

### Phase 3 — Attribution and overhead (indexr-otel-library)

28. Recognise both `{closure}` and PHP 8.4 `{closure:…}` frame names in `CallerAttributor`.
29. Skip a single bad frame instead of discarding the whole attribution result on a normalizer exception.
30. Exclude Composer `vendor-dir` and configured non-application paths from closure attribution.
31. Pass the configured project root into `SpanSymbolIdProcessor` instead of `getcwd()`.
32. Compute caller attribution once per DB span and remove the duplicate processor and listener backtraces.
33. Stop enriching the active span from `QueryExecuted` when the `Connection::run` hook is active.
34. In fallback mode, create a `db.query` span from `QueryExecuted` with its start back-dated by `$event->time`.
35. Measure how often the first application frame lies beyond backtrace depth 20, then tune or make the depth configurable.
36. Remove the cache-event listener, or replace it with bounded per-request counters on the root span.
37. Set short OTLP export and transport timeouts, so an unreachable Collector cannot hold FPM workers.

### Phase 4 — Honest data in the pipeline and UI

38. Record the head-sampling ratio as a resource attribute and add it to the Collector allowlist.
39. Show the effective sampling ratio in Runtime views and scale counts by it where shown.
40. Add root-span attributes for suppressed DB spans and dropped hook edges; render a truncation notice.
41. Return a `truncated` flag from `request_detail` when the 500-row limit is reached.
42. Keep a bounded span-name allowlist in the Collector transform, rewriting only unknown names.
43. Allow `error.type` through the Collector; it carries a class name like `indexer.exception.type`.
44. Compute slow thresholds per route for the top N routes, with the project p95 as fallback.
45. Add a tail-sampling decision cache and a longer `decision_wait` if the sampled pipeline is kept.
46. Correct @runtime-telemetry-flow.md about which table the request list reads.

## Acceptance Criteria

- Local mode with representative Pizza Builder traffic shows lower Collector CPU, lower ClickHouse merges per minute, and lower active part count than the recorded baseline.
- Runtime request list, request detail and summary queries read fewer rows per call than the baseline; verified with `system.query_log` sampling enabled temporarily.
- Aggregation does not query ClickHouse for a revision with no new spans.
- On PHP 8.1, 8.3 and 8.4 fixtures, an Eloquent query issued from `App\Http\Controllers\X::show` is attributed to that controller method, never to a `vendor/` closure.
- Server-span duration for a Laravel and a Symfony fixture matches the web server's request time within a documented tolerance [assumption: 5 ms].
- A Symfony request appears in the Runtime list with route, method and status; a 404 and a firewall-rejected request appear as server spans.
- Doctrine queries run by the Symfony firewall are children of the server span; no orphan DB-only traces remain.
- A Laravel job that throws and retries yields two separate traces, neither nested in the other.
- Runtime views show the sampling ratio and a truncation notice whenever data was suppressed.

## Dependencies

- @.archcore/runtime-telemetry/clickhouse-background-and-retention.plan.md — retention and system-log TTLs; this plan assumes those are in place.
- @.archcore/runtime-telemetry/runtime-trace-summary.spec.md — request-summary contract; check before changing request queries.
- Rebuilt Collector image (forward connector) for tasks 3–4.
- PHP 8.1/8.3/8.4 test matrix for the library, and the demo apps under `indexr-otel-library/demo` and Pizza Builder for end-to-end checks.
- Privacy review for tasks 38, 42 and 43, because they widen the Collector allowlist.

## Risks

- Owning the DDL (tasks 7–9) changes table shape. Old rows need a copy or a TTL-based cut-over, and remote ClickHouse provisioning has to apply the same migration.
- Disabling the sampled pipeline in Local mode removes data that remote or production setups may later rely on. Task 1 decides the behaviour per mode.
- Starting the span earlier (tasks 16–17) makes every reported duration larger. Release notes and a baseline reset are needed, so this is not read as a regression.
- Propagating context into job payloads changes the payload format. It has to stay backward-compatible with jobs already queued.
- Rollups (task 12) make the Runtime summary eventually consistent with ingest, up to the rollup flush interval.

## Verification

- Capture a baseline first: `system.parts` count per table, `system.merges`, Collector and ClickHouse container CPU, and per-query `read_rows` for the four Runtime query families, over a fixed traffic script.
- Re-run the same traffic script after each phase and compare.
- Library: add PHPUnit cases for PHP 8.4 closure frames, vendor-closure exclusion, job retry, Symfony route/status attributes and route-ID method selection. Then run `composer test` on the PHP version matrix.
- End to end: run `tests/e2e/run.sh` in the library and `api/telemetry-dev/test_runtime_stack.py`, then compare span trees in `otel_spans_full` for one request per fixture.

## Verification Results (2026-11-07)

All 46 tasks are implemented. Two differ from their wording:

- Tasks 12–13: closed hours roll up into a ReplacingMergeTree table (`runtime_span_rollups`) instead of materialized views, because the parent and route joins need spans from other insert batches. See @runtime-summary-reads-hourly-rollups.adr.md.
- Task 25: the job span closes on `JobExceptionOccurred`, which a released job always fires first, so `JobReleasedAfterException` is not needed.

### Baseline

No numbers were recorded before Phase 1. The Collector baseline is therefore the old design (128 spans / 1 s batches, synchronous inserts, three exporters). The query baseline was measured by running the query code from commit `c8f01d9` (the last commit before Phase 1) against the same data as the current code, with each query tagged by `log_comment` and logged through `system.query_log`.

Traffic script: 7 Pizza Builder routes plus a 404, about 10 requests/s, 3 minutes (1,736 requests, 35,326 spans).

### Collector and ClickHouse writes

- New `otel_spans_full` parts: 36 in 3 minutes (12/min, about 980 spans per part). The old 128-span / 1 s batching would create at least one part per second per exporter.
- Merges in the window: 7, taking 182 ms in total.
- Collector CPU: 0.7% under traffic.
- `otel_traces` and the span-metrics tables receive no new rows in Local mode (last rows 2026-11-06).

### ClickHouse CPU: refetch storm found and fixed

The first run showed ClickHouse at 94% CPU (`docker stats`) while idle was 5%. The cause was reads, not writes: with a Runtime view open, every profile upload and every host-agent report (about 34 per minute, even without changes) published `runtime.updated`, and each event made every open view rebuild the revision summary. That was 449 SELECTs and 72.8 million rows read per 30 s, with or without traffic.

Fix (api): each event stream sends at most one event per 5 s and coalesces the rest; a host report publishes only when a deployment record changed (spec requirements 41–42).

With a Runtime view subscribed to updates, per 60 s:

| | Before | After |
|---|---|---|
| ClickHouse user CPU, traffic | about 36 s | 2.6 s |
| ClickHouse user CPU, idle | about 42 s | 0.9 s |
| `docker stats` ClickHouse CPU, traffic | 94% | 10% |

### Rows read per Runtime query family (old code vs current, same data)

The 24 h window had about 46,000 spans in rolled-up hours and 28,856 in the open hour, which is always read raw.

| Family | Rows read | Bytes read | Time | Queries |
|---|---|---|---|---|
| Summary | 843k → 600k (−29%) | 424 → 105 MiB (−75%) | 350 → 83 ms | 9 → 3 |
| Request list | 18.5k → 10.3k (−44%) | 12.8 → 10.8 MiB | about equal | 1 → 1 |
| Request detail | equal | 12.4 → 10.3 MiB | equal | 2 → 2 |

The raw read of the open hour dominates the remaining summary cost.

### Span accuracy

- Server-span duration, Laravel (Pizza Builder, PHP 8.3, nginx + FPM): 40 sequential requests; the client time minus the span is 0.7–2.9 ms (median 1.5 ms). 40/40 are within 5 ms.
- Server-span duration, Symfony (demo, PHP 8.4, `php -S`): measured to the first response byte, the gap is a median 1.3 ms, with 19/20 within 5 ms (one at 7 ms). `php -S` keeps the connection open for about 9.7 ms of post-response work (span export), which the span correctly excludes.
- Eloquent attribution: on Pizza, the query from `HomeController::pizza` is attributed to that method, never to a vendor closure. PHP 8.1–8.4 attribution is covered by the PHPUnit suite (53 tests on 8.1, 8.2, 8.3 and 8.4).
- Symfony: every request is a server span with route, method and status, including 404 and 405 (`(unmatched)`) and 500 (Error). A request rejected at the firewall's priority keeps its matched route and has no controller symbol.
- Symfony Doctrine: all 86 DB spans were direct children of their request's server span, and no DB-only traces remained. The guard's queries sit in the rejected request's trace.
- Laravel job retry: two attempt spans (Error, then Unset), both children of the dispatching span (task 24 propagates context into the payload), so they share the dispatcher's trace and are siblings. Neither is nested in the other.

### Library defects found during verification (fixed)

- Symfony Doctrine queries produced no DB spans at all. The DBAL middleware was tagged in a compiler pass that ran after DoctrineBundle's `MiddlewaresPass`. It is now registered in `IndexerOtelExtension::load()`.
- A request answered before its controller (for example by the firewall) was recorded as `(unmatched)`. `KernelSubscriber::onResponse` now records the router-matched route without a controller symbol.

`tests/e2e/run.sh` now asserts Symfony DB spans, no orphan DB traces, the guarded 403 route, the unmatched 404, two job attempts and no nested job spans. It passes from a clean stack.

### Not covered

- A real Symfony security firewall: the demo uses a `kernel.request` listener at the firewall's priority instead of security-bundle.
- Remote-mode ClickHouse.
