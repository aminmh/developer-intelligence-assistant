---
title: "Benchmarks Run in the Background, One at a Time"
status: draft
tags:
  - "api"
  - "code-quality"
  - "frontend"
---

## Context

Setup Performance ran k6 inside the HTTP request that started it (@api/Python-Service/app/code_quality/performance.py). A 300 s run plus warm-up and k6's graceful stop outlasted the web server's 300 s proxy timeout, so users saw a failure while the run was still saved; leaving the page lost the result; nothing limited parallel runs, which held API worker threads and distorted each other's numbers. Targets were per user rather than per project, and the role check skipped project IDs in the URL path (@web/src/server/oidcAuth.ts).

## Decision

1. `POST /projects/{id}/performance/runs` validates, registers the run in `performance_jobs` and returns 202; a background thread runs k6.
2. At most one active run per user and two in total; a further start returns 409 with the active run's id.
3. Each k6 process exposes its REST API on a pooled local port (6565/6566). The API reads live request counts from it, and cancelling stops k6 through it, so the summary of a cancelled run is still saved as partial.
4. On API start, running jobs are marked failed and leftover k6 scripts (which can hold secrets) are deleted from tmpfs.
5. Targets belong to a user and a project, have a name (several profiles per origin), and only project operators or admins may change them or start runs. The access middleware reads the project from the URL path and refuses a different project in the body or query.
6. Each route runs as its own k6 scenario. Redirects, 4xx and 5xx make a run partial with a stated reason; a login counts only when it does not redirect back to the login page and the optional check route returns 2xx.
7. Benchmark requests carry `X-DevAssistant-Benchmark: <run id>`; the PHP library records a well-formed id as `indexer.benchmark_run_id`, and the collector keeps only UUID-shaped values.
8. The run's traces endpoint also returns a per-route time breakdown built from up to 300 tagged traces: mean server time split into database, cache (Redis/Memcached/Valkey), outbound calls (other Client/Producer spans) and application code (the remainder, with mean PHP CPU time). Overlapping child spans count once (`span_time_breakdown` in @api/Python-Service/app/telemetry/runtime.py, shared with Request Detail). Network and queueing time is k6's request-weighted mean latency for the route template minus the mean server time. Because traces are sampled, the breakdown is an estimate.

## Alternatives

- **Raise the proxy timeout.** Rejected: still blocks a worker thread and loses the result on navigation.
- **A separate benchmark worker service.** Deferred: more deployment work; the run registry keeps this possible later.

## Consequences

- Long runs work and survive navigation; one user's run cannot starve the API.
- Targets saved before this change appear as "unassigned" until saved under a project.
- Runs record their code revision, so comparisons show deltas across revisions.
