# Runtime trace summaries

Phase 5 adds a privacy-shaped summary to each available sampled request detail.
The ai_summary response contains trace_id, endpoint, total_latency_ms,
otel_breakdown, excimer_hotspots, profile_event_type, and actionable_insight.
For example:

    trace_id: 4bf92f3577b34da6a3ce929d0e0e4736
    endpoint: POST /api/v1/orders
    total_latency_ms: 420.5
    otel_breakdown:
      mysql_queries_ms: 110.2
      redis_get_ms: 12.0
      php_app_time_ms: 298.3
    excimer_hotspots:
      - frame: App\Services\OrderService::calculateTaxes
        sample_percentage: 68.4
    profile_event_type: EXCIMER_REAL

The summary is returned as ai_summary by
GET /telemetry/runtime/projects/{project_id}/revisions/{revision_id}/requests/{trace_id}.
The existing web proxy returns the same field to the Runtime request details UI.

The API computes MySQL and Redis spans as non-overlapping intervals within the
server request span. php_app_time_ms is the remaining request time; it can
include work from uninstrumented dependencies and is not a direct PHP CPU
measurement. Other database systems are not included in these named buckets.
When ClickHouse has no matching retained profile, excimer_hotspots is empty
and profile_event_type is null.

Hotspot percentages are Excimer stack sample estimates for one trace. EXCIMER_REAL
samples reflect wall-clock stacks; EXCIMER_CPU samples reflect CPU-time stacks.
The API emits canonical method IDs and numeric durations only. It does not return
request bodies, SQL text or bindings, headers, or cache keys.
