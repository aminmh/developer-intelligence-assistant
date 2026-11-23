---
title: "Local Runtime keeps all received spans and skips tail sampling"
status: accepted
tags:
  - "api"
  - "frontend"
  - "runtime-telemetry"
---

## Context

The runtime Collector writes every accepted span twice: all of them to `otel_spans_full`, and a tail-sampled subset (errors, slow requests over the p95 threshold, plus a 10% baseline) to `otel_traces`. It also derives span metrics into ClickHouse metric tables.

The Runtime request list, request detail, aggregation and p95 policy all read `otel_spans_full` (@api/Python-Service/app/telemetry/runtime.py `self.table`). `otel_traces` is referenced only by revision deletion, and no code reads the span-metric tables. In Local mode the extra pipelines therefore cost Collector memory (a 50,000-trace tail-sampling buffer), CPU, and two of three ClickHouse insert streams, without serving any reader.

Local ClickHouse was observed exceeding its 1.2 GiB server memory cap on 2026-11-06, which dropped telemetry inserts (Collector "Exporting failed. Dropping data"), so local write load matters.

A developer running Local mode generates low traffic and expects to see the request they just made. With tail sampling, about 90% of normal requests would not be retained.

## Decision

- In Local storage mode, the generated Collector configuration keeps only the full-span pipeline. It omits the sampled-trace pipeline (p95 policy and tail sampling processors, `clickhouse/sampled` exporter) and the span-metrics connector, pipeline and exporter.
- The Runtime request list continues to read `otel_spans_full`.
- The request list gains an outcome filter (all, errors, slow), so errors and slow requests stay findable without sampling.
- Remote mode keeps the sampled pipeline unchanged. Whether Remote's request list should read `otel_traces` is left for a separate decision.
- Changing the storage mode restarts the Collector, because Local and Remote now generate different pipelines.

## Alternatives

- **Request list reads `otel_traces` in all modes.** It surfaces errors and slow requests first, but hides most normal requests from a local developer and keeps the full write load.
- **Keep both pipelines and change nothing.** The local ClickHouse memory pressure stays, with no reader for the extra data.
- **Disable the full pipeline instead.** Aggregation, request detail and the p95 policy depend on full spans.

## Consequences

- Local ClickHouse receives one trace insert stream instead of three; `otel_traces` and span-metric tables stop growing in Local mode. Existing rows expire through their TTLs.
- A busy local session can push errors out of the latest 50 requests; the errors and slow filters cover that case.
- Switching Local and Remote must regenerate the Collector config and restart it, which @api/Python-Service/app/telemetry/runtime_storage.py `set_storage_mode` handles.
- Implements task 1 of @.archcore/runtime-telemetry/local-clickhouse-performance-and-span-accuracy.plan.md.
