# Phase 1 — Runtime telemetry product surfaces

Status: implementation complete; local runtime ingestion still requires
application spans and deployment reports. Scope authorized by the user on
2026-09-26 after the Phase 0 review boundary.

## Intent and boundary

Make runtime data visible beside the existing project, symbol, schema, chat,
and settings views, and add a Runtime tab. Preserve Phase 0's canonical
`(project_id, commit_sha, symbol_id)` join, project roles, immutable source
snapshots, and immediate browser upload behavior. Raw spans remain in
ClickHouse; do not write span aggregates or runtime relationships to Neo4j or
Qdrant. Legacy uploads and saved analyses remain unattributed.

Counts are observed spans within the configured retention window. A symbol
without observed spans is a candidate for review, not proof of dead code.
Routes, tables, callers, latency, failures, sampling, and match coverage are
shown only when the incoming spans include the reviewed attributes. Missing
instrumentation or unavailable storage must be visible as such.

## Change necessity and complexity

The tab shell and upload identity already exist, but no runtime read API, live
collector profile, runtime tab, or telemetry-aware existing view exists.
Configuration or copy changes alone cannot supply those behaviors. The minimum
boundary is a ClickHouse read adapter, deployment/snapshot read methods, a
project-authorized Express proxy, and focused additions to existing tab owners.

Target owners: `telemetry/runtime.py`, `telemetry/http.py`,
`telemetry/registry.py`, `server.ts`, existing Vue view owners, and the
telemetry stack/config documentation. This adds one read-side runtime owner;
the collector fixture and static graph remain separate. Complexity is
at-risk because several existing views consume the same runtime contract;
centralize aggregation and keep UI components read-only.

## Tasks and checkpoint

1. Add bounded ClickHouse aggregation and deployment/snapshot listing — done.
2. Expose authenticated browser proxies and sanitized telemetry settings — done.
3. Add Runtime tab, per-symbol metrics and method deep links — done.
4. Add runtime evidence to chat results, route/table usage to schema, and
   running/mismatched deployment state to Knowledge Base files — done.
5. Add Runtime telemetry settings card and operator setup guidance — done.
6. Run static/configuration checks only. Do not add or run tests — done.

Compatibility: retain current tab query behavior, upload metadata, OIDC roles,
legacy `sym_<stem>` inspection, Phase 0 synthetic Collector fixture, and
existing telemetry retention defaults. No host certificates, deployment
inventory, archive bytes, or user state will be deleted or migrated.

## Verification boundary

No unit or integration tests will be added or run. Use TypeScript lint/build,
Python syntax compilation, Compose config validation, diff checks, and
unauthenticated endpoint/collector config checks where the local services allow.
Runtime login, production host discovery, and real application instrumentation
remain unverified unless the required target apps and credentials are present.

## Resume checkpoint

- Baseline: API `feat/php-telemetry-phase-0` at `08a20cb`; preserve pre-existing
  `.env`, domain-model/dictionary/test edits, snapshot data, and prompt files.
- Baseline: web `fix/x6-erd-rendering` at `4866c58`; initially clean.
- Current task: Phase 1 read-side aggregation contract and UI wiring.
- Completed: runtime ClickHouse service/API, role-scoped Express proxies, Runtime
  view, Definitions metrics/filters/deep links, chat runtime context and matched
  result metrics, schema table usage/highlighting, Knowledge Base deployment
  state, Runtime settings card, sampling config, and rollout/privacy docs.
- Verification: TypeScript lint/build, Python syntax compilation, Compose config,
  and `git diff --check` pass. Tests were neither added nor run.
- Limitation: application-specific PHP method-span instrumentation and verified
  host deployment reporter credentials/integration are still required for live
  data. The local receiver is opt-in and bound to loopback.
- Blockers: none; real project spans may not be present.
- Next: configure and instrument each target PHP application to emit the
  canonical identity and reviewed route/database attributes, then configure
  verified host deployment reporting to populate live container status.
