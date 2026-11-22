# Phase 0 telemetry attributes and privacy contract

Schema: `indexer-telemetry-v1` describes the Phase 0 synthetic canary fixture.
The application Runtime Collector uses `indexer-telemetry-v2`. OpenTelemetry
Semantic Conventions are pinned to v1.44.0 for Phase 0; upgrade only with an
explicit schema review. The Collector image is pinned independently. Indexer
attributes are project-specific and do not claim to be upstream semantic
conventions.

## Required identity

Every future exported span must carry these resource attributes:

| Attribute | Type | Requirement |
| --- | --- | --- |
| `service.name` | string | stable deployed service name |
| `service.version` | string | deployed application version |
| `service.namespace` | string | `devassistant` |
| `deployment.environment.name` | string | exactly `development`, `staging`, or `production` |
| `container.id` | string | opaque runtime container identifier |
| `container.image.name` | string | image name without registry credentials |
| `container.image.id` | string | immutable image digest when available |

The Phase 0 synthetic canary uses `vcs.ref.head.revision` and
`indexer.schema.version=indexer-telemetry-v1`. Do not use that identity for
application Runtime telemetry.

HTTP, database, messaging, and exception fields should follow the pinned
conventions only when explicitly allow-listed by the signal schema. Semantic
convention attributes do not override the privacy rules below.

## Privacy boundary

Redact in the SDK/exporter before data leaves the application host and repeat
the allow-list at Collector ingestion. Drop unknown attributes and events by
default. Never export request/response bodies, authorization/cookie headers,
credentials, tokens, full cache keys, raw SQL, SQL literals, exception messages,
or stack traces. If a database span is useful, retain only a reviewed normalized
statement shape and safe operation metadata. Normalize or discard exception
details before export; do not rely on ClickHouse TTL as a privacy control.

The Phase 0 development Collector accepts only its synthetic canary identity,
strips unapproved fields, empties events and status detail, and drops spans with
links. This remains a sanitizer smoke fixture, not a production receiver policy.
Phase 1 adds a separate opt-in local runtime Collector; it does not change the
synthetic-only pipeline.

## Sampling and counts

Phase 0 emits no application evidence, aggregate metrics, or sampled counts.
Phase 1 dashboards identify observations as counts from retained sampled spans;
they do not represent exact production call counts. The runtime Collector uses
`TELEMETRY_RECORDING_PERCENT` (0–100; default 100) for probabilistic sampling.
Settings shows the Collector value; keep every app SDK sampler aligned with it.
Collector tail sampling cannot restore spans already discarded by SDK head
sampling. Reduced sampling means incomplete coverage and error capture.

Raw span retention in the optional local stack defaults to seven days and is
configured by `TELEMETRY_RETENTION_HOURS`. Production cross-host ingestion
requires mutually authenticated TLS and independently managed credentials. Raw
spans stay in ClickHouse; runtime aggregates do not write to Neo4j or Qdrant.

## Phase 1 runtime attributes

Start the separate local runtime pipeline with `docker compose --profile runtime
up -d` from `api/`. It accepts OTLP/HTTP at loopback port 24318, applies an
allow-list, and stores raw traces in ClickHouse. Application resource attributes
must include `indexer.project_id`, the immutable `indexer.revision_id` assigned
by Developer Assistant, `indexer.code_dirty=false`, and
`indexer.schema.version=indexer-telemetry-v2`. The PHP installer selects a ready
revision and verifies the target Git checkout internally. Git SHA remains
internal source/deployment provenance. The runtime summary joins
`indexer.symbol_id` to the exact immutable `Symbol` snapshot for that
project/revision. Caller, route, and table relationships are inferred only
within a shared trace ID; they remain observations, not static graph edges.

### Request and database details

The Laravel adapter may add the route template (`http.route`), HTTP method
(`http.request.method`), and numeric response status
(`http.response.status_code`) to a server request span. Database spans may add
the driver (`db.system.name`), a validated database namespace (`db.namespace`),
and the SQL operation from the fixed verb set
`SELECT|INSERT|UPDATE|DELETE|REPLACE|CREATE|ALTER|DROP|TRUNCATE`
(`db.operation.name`). Existing `indexer.table_ids` and safe application
symbol identifiers attribute table activity to request traces. Request and DB
details are returned only for the selected project and immutable revision.

The PHP adapter reports `indexer.php.cpu_time_us`, the request interval's
worker user-plus-system CPU time in microseconds, from `getrusage()`. It reports
`indexer.php.peak_memory_bytes`, the PHP allocator peak above the request-start
baseline in bytes, only when the runtime supports resetting its peak counter.
These are estimates for one PHP worker request; they do not measure whole
container CPU or resident set size. Unsupported measurements are omitted.

The Runtime Collector allow-list admits these fields explicitly. It continues
to drop `db.query.text`, raw SQL, bindings, request/response payloads, headers,
cookies, credentials, and all other unapproved span attributes.

The local pipeline is a development receiver and has no remote authentication.
Do not expose port 24318 outside the local host. Host deployment reports still
require the separately configured verified-client-certificate ingress described
in `snapshots.md`.
