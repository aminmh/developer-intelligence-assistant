---
title: "Tune Runtime ClickHouse Background Work and Retention"
status: draft
tags:
  - "api"
  - "runtime-telemetry"
---

## Goal

Reduce idle ClickHouse diagnostic work and keep Runtime telemetry storage bounded by age, while preserving Runtime request visibility and reporting when disk space is getting low.

## Architecture

Keep the changes within the existing Runtime profile. Add a separate ClickHouse server configuration for diagnostic logging and system-log TTLs, use the existing Collector TTL environment variables for application telemetry, and extend the existing Runtime setup CLI with a read-only storage report. Do not change telemetry schemas or request APIs.

## Tech Stack

ClickHouse 25.8.4.13, OpenTelemetry Collector Contrib 0.136.0, Docker Compose, Python setup CLI, and the root justfile.

## Baseline/Authority Refs

- @api/docker-compose.yml mounts clickhouse-memory.xml and clickhouse-users-memory.xml for the Runtime ClickHouse service.
- @api/telemetry-dev/clickhouse-memory.xml caps tracked server memory at about 1.2 GiB and sets small cache limits.
- @api/telemetry-dev/clickhouse-users-memory.xml caps per-query memory at 256 MiB.
- @api/telemetry-dev/collector-runtime.yaml applies TTL from TELEMETRY_RETENTION_HOURS and TELEMETRY_AGGREGATION_RETENTION_HOURS, both defaulting to 168 hours.
- @api/telemetry-dev/runtime_setup.py owns just runtime-status.
- @justfile exposes Runtime setup and status commands.
- @README.md documents the Runtime service, restart behavior, and current memory bounds.
- Prior measurement found system.trace_log and system.text_log substantially larger than Runtime telemetry tables; the Docker socket was unavailable for a fresh live measurement during planning.

## Compatibility Boundary

The Runtime Collector continues writing the same tables with the same schema. Request details and Runtime metrics remain queryable for the configured retention window. ClickHouse diagnostic records expire independently. Proposed values are 72 hours for Runtime telemetry, 48 hours for trace samples, 72 hours for text and metric logs, and seven days for query logs.

Enabling TTL can expire existing rows older than those windows. Do not truncate tables or remove the named volume. Confirm the proposed retention windows before enabling TTL against existing stored rows.

This provides age-based retention and an operator warning, not a hard byte quota on the Docker/OrbStack volume.

## Declared Delta

- Creates: a read-only Runtime ClickHouse storage status command.
- Modifies: Runtime ClickHouse diagnostic logging and Runtime telemetry retention.
- Retires: none.
- Decision: none; retention values remain proposed pending user confirmation.
- Intent gap: no; this is an operational improvement to the existing Runtime service.
- [assumption] Proposed retention windows: 72 hours for Runtime telemetry, 48 hours for trace samples, 72 hours for text and metric logs, and seven days for query logs.

## TDD Route

Mode: off. Decision: skipped. Authority: project default. Test posture: no test-first work. Reason: this is a bounded operational configuration change with existing runtime smoke and configuration checks. Verification: validate rendered Compose, effective ClickHouse settings and TTLs, storage report output, and Runtime request visibility after restart.

## Tasks

1. Add ClickHouse diagnostic log levels, collection intervals, and system-log TTLs in @api/telemetry-dev/clickhouse-runtime.xml.
2. Mount the new server configuration read-only in @api/docker-compose.yml.
3. Increase ClickHouse profiler sampling periods from one second to ten seconds in @api/telemetry-dev/clickhouse-users-memory.xml.
4. Set Runtime telemetry retention defaults to 72 hours in @api/telemetry-dev/collector-runtime.yaml, @api/docker-compose.yml, and @api/.env.example.
5. Add a read-only ClickHouse disk and table-size report to @api/telemetry-dev/runtime_setup.py.
6. Expose the storage report through a dedicated root @justfile recipe.
7. Document retention, the storage command, warning thresholds, and idle shutdown guidance in @README.md and @api/telemetry-dev/README.md.
8. Capture table sizes and idle CPU before and after applying the settings.
9. Verify Runtime request summaries and metrics remain visible after the retention and logging changes.

## Acceptance Criteria

- ClickHouse text logging records warnings and errors rather than trace-level messages.
- ClickHouse query profiler sampling runs at ten-second intervals; PHP Excimer profiling remains unchanged.
- Diagnostic system logs have explicit age-based TTLs matching the proposed windows.
- Runtime sampled telemetry and full-span aggregation use 72-hour retention by default.
- The storage report shows ClickHouse disk capacity/free space and largest system and telemetry tables, with a warning below 15% free.
- Runtime schemas, request APIs, sampling policy, and selected-revision behavior remain unchanged.
- Rollout does not truncate tables or delete the named volume.
- README guidance matches the implemented settings and commands.

## Dependencies

- A running Docker daemon for live configuration, storage, and CPU verification.
- Confirmation of the proposed TTL windows before those TTLs are applied to existing tables.
- Coordination with the current dirty API and Web working trees; preserve the in-progress Phase 5 changes and local .env/generated dictionary edits.

## Risks

- Shorter retention removes older telemetry and diagnostic records through normal TTL cleanup.
- TTL cleanup is asynchronous, so disk usage may remain above its target temporarily.
- Docker named-volume storage has no application-level byte cap here; age-based retention and monitoring reduce risk but cannot guarantee a strict host-volume maximum.
- Ten-second ClickHouse profiler sampling reduces ClickHouse internals profiling detail; it does not change PHP Excimer data.
- The current API and Web repositories contain uncommitted changes. Implementation must preserve them and review overlapping diffs before editing shared files.

## Verification

- Render docker compose --profile runtime config and confirm all mounts and retention defaults.
- Query system.settings, system.tables, system.parts, and system.disks to verify effective profiler periods, TTLs, table sizes, and available space.
- Run the existing telemetry smoke check and issue representative Pizza requests; confirm Runtime request rows and metrics still appear.
- Compare idle CPU and table growth over equal observation windows before and after the changes.
- Review git diff in both repositories and confirm unrelated dirty work remains intact.