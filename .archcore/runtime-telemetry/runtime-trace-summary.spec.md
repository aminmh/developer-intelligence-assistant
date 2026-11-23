---
title: "Runtime Request Detail and AI Analysis"
status: draft
tags:
  - "api"
  - "runtime-telemetry"
---

## Purpose & Scope

Defines sampled request detail, evidence labeling, and on-demand AI analysis and draft-diff behavior.

## Surface

- API: @api/Python-Service/app/telemetry/http.py and @api/Python-Service/app/telemetry/runtime.py
- Model gateway: the application's configured chat model
- Web display: @web/src/components/RuntimeView.vue, @web/src/components/runtime/RuntimeSetupWizard.vue, @web/src/utils/runtimeProjects.ts and @web/src/types.ts
- Request detail: GET /telemetry/runtime/projects/{project_id}/revisions/{revision_id}/requests/{trace_id}
- Analysis: POST /telemetry/runtime/projects/{project_id}/revisions/{revision_id}/requests/{trace_id}/analysis
- Diff preview: POST /telemetry/runtime/projects/{project_id}/revisions/{revision_id}/requests/{trace_id}/analysis/diff

## Normative Behavior

1. WHEN an authorized request detail exists, the API MUST include an ai_summary object.
2. WHEN the API returns ai_summary, it MUST include trace_id, endpoint, total_latency_ms, otel_breakdown, excimer_hotspots, profile_event_type, and actionable_insight.
3. WHEN the API computes MySQL or Redis duration, it MUST de-overlap instrumented span intervals within the server request interval.
4. WHEN the API computes php_app_time_ms, it MUST report the request-time residual after instrumented MySQL and Redis spans.
5. WHEN a retained profile matches the trace, the API MUST return canonical method IDs and their sample percentages.
6. WHEN no retained profile matches the trace, the API MUST return an empty excimer_hotspots array and null profile_event_type.
7. WHEN request detail includes method-hook counts, the API MUST label them as bounded instrumentation observations.
8. WHEN the Runtime view displays method-hook counts, it MUST NOT imply call order or complete coverage.
9. WHEN the Runtime view displays timing or profile values, it MUST distinguish observed spans, residual estimates, statistical samples, and AI inferences.
10. WHEN the Runtime view lacks a telemetry signal, it MUST identify the coverage gap without implying that the corresponding work did not occur.
11. WHEN a developer requests analysis, the API MUST assemble available request signals and source from the request's indexed revision.
12. WHEN the Runtime view offers analysis, it MUST identify the configured model provider and data categories before submission.
13. WHEN the API sends model context, it MUST omit request and response bodies, headers, credentials, and cookies.
14. WHEN the API sends model context, it MUST omit raw SQL, SQL bindings, cache keys, and exception messages or stacks.
15. WHEN the model returns analysis, the API MUST provide evidence references, recommendations, and explicit coverage gaps.
16. WHEN source mapping is unavailable, the API MAY return telemetry-backed recommendations.
17. WHEN source mapping is unavailable, the API MUST withhold code-diff actions.
18. WHEN a developer requests a diff for a selected finding, the API MUST validate its source mapping against the request's indexed revision.
19. WHEN the API returns a draft diff, it MUST pin the diff to the indexed revision.
20. WHEN the API returns a draft diff, it MUST NOT apply it to a checkout.
21. WHEN the API returns analysis or a draft diff, it MUST NOT persist either result as telemetry or analysis history.
22. WHEN the Runtime view mounts, it MUST disable live updates by default and expose a labeled toggle to enable them.
23. WHEN the Runtime toggle disables live updates, the view MUST stop event-triggered refreshes while retaining manual and workflow-triggered refreshes.
24. WHEN the Runtime view displays Auto-refresh, it MUST use the shared card-style toggle with a visible On/Off state and keyboard operation.
25. WHEN the Runtime header displays actions, a project selector, or Auto-refresh, it MUST render them at the same 40 px height.
26. WHEN more than ten sampled requests are loaded, the Runtime view MUST display ten per page and provide navigation to the remaining loaded requests.
27. WHEN the Runtime view displays sampled requests, it MUST display the Containers panel as a separate section immediately before the Recent sampled requests section.
28. WHEN a project revision is selected, the Runtime view MUST omit the project/revision summary banner and overview metric cards.
29. WHEN the Runtime view renders indexed telemetry sections, it MUST omit the Dead-code candidates and Routes and database tables panels.
30. WHEN a developer expands a sampled request, the Runtime view MUST present Overview, Database, Method activity, and AI analysis tabs.
31. WHEN the Runtime view expands another request, it MUST select Overview by default.
32. WHEN a developer focuses a request-detail tab, the Runtime view MUST support ArrowLeft, ArrowRight, Home, and End navigation.
33. WHEN the Runtime view displays request details, it MUST render detail text at 12 px or larger.
34. WHEN request detail includes database calls, the API MUST return a database_summary with query count, total database time, its share of the request duration, the slowest call, and same-shape groups.
35. WHEN the API groups database calls, it MUST key a group by operation, table IDs, and the issuing application method, and MUST flag the group as repeated when it occurs at least REPEATED_QUERY_MIN (3) times in the request.
36. WHEN the Runtime view shows repeated database groups, it MUST state that the SQL text is not stored and that queries in a group may differ in columns or conditions.
37. WHEN request analysis runs, the API MUST include the database_summary groups (operation, table IDs, issuing and calling method IDs, counts and timings) without SQL text or span IDs, MUST put the source of methods behind repeated groups first in the source budget, and MUST return each finding with a category of database, methods, profile or general (unknown values become general).
38. WHEN an analysis of the request exists, the Database tab MUST show its database findings, mark the groups whose method a database finding cites, and offer the diff preview through the AI analysis tab.
39. WHEN the API summarizes database calls, it MUST label a group as framework only when every table is a framework table (FRAMEWORK_TABLES or a telescope_, pulse_ or horizon_ prefix) and no application method issued it, MUST report app and framework query counts and times separately, and MUST pass the label to request analysis, which MUST NOT recommend an application code change for framework groups.
40. WHEN a request-analysis or diff-preview model call fails, the API MUST log the provider and the exception chain (type and message, no prompt or evidence) and MUST log a remote failure before falling back to the local model.
41. WHEN ingest publishes runtime.updated, each open event stream MUST send at most one event per 5 seconds and MUST deliver the last change in a coalesced event at the end of the interval.
42. WHEN a host agent reports deployments, the API MUST publish runtime.updated only if a deployment record changed.
43. WHEN a revision has no Git commit, the Runtime view MUST preserve its revision selection and load details by revision ID.
44. WHILE local setup runs, the Runtime view MUST show a busy indicator until completion, cancellation, or failure.
45. WHEN the Runtime view displays setup status, it MUST place the message below the status heading.
46. WHEN preparing container setup, the wizard MUST distinguish a ready matching Git revision from missing, mismatched, or unavailable source inventory.
47. WHEN preparing container setup, the wizard MUST explain separate PHP tracing setup and display a quoted project-specific setup command.
48. WHEN source indexing is absent, the wizard MUST preserve container-only setup.
49. WHEN the Runtime view mounts, it MUST leave Hide unmatched 404s disabled by default.
50. WHEN Hide unmatched 404s is enabled, the API MUST exclude unmatched 404 requests without Error span status before the request-list limit.
51. WHEN Hide unmatched 404s is enabled, the Runtime view MUST explain that traces remain stored and explicit errors remain visible.

## Constraints & Invariants

- php_app_time_ms is a residual and may include uninstrumented dependency work.
- Method-hook counts cover configured hooks only and do not establish execution chronology.
- Excimer sample percentages are statistical estimates, not exact request-time or CPU percentages.
- EXCIMER_REAL describes wall-clock samples; EXCIMER_CPU describes CPU-time samples.
- Analysis and diff results remain transient in the Runtime view.
- A draft diff is a read-only proposal associated with one immutable indexed revision.
- A repeated database group is a hint (often N+1 or a per-call cache read), not proof that the queries are identical.
- A framework table read by application code (for example a settings cache read in a model) is application work, not framework work.
- A database finding's cause is a model inference from grouped metadata and source excerpts, labelled as AI inference.

## Failure Behavior

1. WHEN ClickHouse has no retained profile for a trace, the API MUST preserve the trace summary and report no hotspots.
2. WHEN a trace detail is unavailable, the existing request-detail response MUST preserve its unavailable status and reason.
3. WHEN telemetry signals are incomplete, the API MUST return available evidence and identify missing coverage.
4. WHEN the configured model provider fails or returns invalid analysis, the Runtime view MUST show an actionable error and retry control.
5. WHEN source mapping is missing or stale, the Runtime view MUST explain why diff preview is unavailable.
6. WHEN the selected finding cannot be validated against the indexed revision, the API MUST reject diff generation without modifying project files.

## Conformance

- The request detail exposes bounded method counts, database calls, and retained profile evidence available for its trace.
- The Runtime view separates deterministic ai_summary content from on-demand model analysis.
- The Runtime view groups request details into navigable category tabs and uses at least 12 px text in the expanded detail panel.
- Developers can request analysis, inspect evidence and coverage, select a finding, and preview a revision-pinned diff.
- Missing signals, provider errors, invalid model output, and stale source mappings expose the specified recovery states.
- Python syntax and frontend type/build checks verify implementation shape; live profile correlation requires runtime data.