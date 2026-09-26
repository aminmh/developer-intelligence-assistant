# Phase 0 telemetry attributes and privacy contract

Schema: `indexer-telemetry-v1`. OpenTelemetry Semantic Conventions are pinned
to v1.44.0 for Phase 0; upgrade only with an explicit schema review. The
Collector image is pinned independently. The indexer attributes below are
project-specific and do not claim to be upstream semantic conventions.

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

Every span attributed to source must include `indexer.project_id` (provisioned
project slug), `vcs.ref.head.revision` (full lowercase commit SHA),
`indexer.symbol_id` (canonical readable ID), `indexer.dirty` (boolean), and
`indexer.schema.version` (`indexer-telemetry-v1`). Dirty work is approximate
evidence associated with a nearby clean commit and must never update that
commit's exact snapshot. No unobserved event carries forward to a new commit.

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
links. This is a sanitizer smoke fixture, not a production receiver policy or
an application telemetry exporter. Runtime SDKs, production mTLS deployment,
sampling, and framework instrumentation are later-phase work.

## Sampling and counts

Phase 0 emits no application evidence, aggregate metrics, or sampled counts.
Future dashboards must identify whether a value is an exact hook count,
pre-sampling aggregate, or sampled span observation. Collector tail sampling
cannot restore spans already discarded by SDK head sampling. Initial validation
must use full recording; any reduced-head-sampling profile must disclose
incomplete coverage and error capture.

Raw span retention in the optional local stack is seven days. Production
cross-host ingestion requires mutually authenticated TLS and independently
managed credentials. No telemetry is exported to Neo4j or Qdrant in Phase 0.
