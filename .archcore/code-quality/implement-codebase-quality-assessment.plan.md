---
title: "Implement Codebase Quality Assessment"
status: draft
tags:
  - "api"
  - "code-quality"
  - "frontend"
---

## Declared Delta

- Creates: live, user-configured HTTP performance benchmarks and separate benchmark history.
- Modifies: Code Quality UI/API, report persistence, project-scoped source exclusions, static analyzer evidence and coverage, host credential-helper operations, framework-agnostic source inventory, and project source synchronization.
- Retires: none; static assessment and Runtime request history remain in place.
- Decision: implement benchmark execution and history as a separate lifecycle inside the existing Code Quality owner; fetch Git source into immutable revisions and keep any semantic code retrieval scoped to that revision.
- Intent gap: resolved by the approved Code Quality assessment specification.

## Goal

Deliver user-selected HTTP benchmarks with target-scoped authentication, adjustable workloads, saved results, and comparison history separate from static quality assessment. Support Git-backed source revisions, project-scoped assessment exclusions, broader static analyzer evidence, and bounded revision-scoped retrieval so semantic review can use targeted evidence without sending the full source tree to the model. The assessment does not execute project test suites.

Aegis Visibility: this durable plan coordinates the Code Quality API/UI, benchmark persistence, Runtime route suggestions, and OS credential boundary.

## Requirement Ready Check

- Approved source: Codebase Quality Assessment specification.
- Goals and scope: Setup Performance, selected GET routes, target-scoped authentication, adjustable workloads, and separate benchmark history.
- Scenarios: public routes, saved Bearer token, and cookie session obtained through HTTP login with optional CSRF bootstrap.
- Acceptance evidence: route-level metrics, workload provenance, non-equivalent comparisons, credential isolation, and static-assessment compatibility.
- Open blockers: none for planning; implementation must confirm the supported host-helper execution boundary.
- Decision: ready.

## Architecture

The approved Code Quality spec owns normative behavior; this plan owns implementation sequencing, file mapping, and verification. Reuse the Code Quality API, authenticated Web proxy, view, existing SQLite database, Runtime request history, and host OS credential helper. Add benchmark execution and result handling as a Code Quality submodule with separate records; keep static assessment reports unchanged. Runtime remains the owner of sampled route history. The host helper remains the only owner of OS credential access and provides purpose-limited per-run Bearer-token use without a general-purpose retrieval endpoint. The benchmark runner uses pinned k6, validates target origin and selected routes, and enforces workload bounds. HTTP login credentials, CSRF values, and cookies remain in memory for one run; each virtual user logs in once and reuses its session. A Git source connector fetches a user-selected branch, compares its full head commit SHA with the latest indexed revision, and stages a clean immutable archive for the existing snapshot/index pipeline. Provider webhooks may trigger reconciliation, while user-triggered or scheduled fetch-and-compare remains authoritative. Local developer hooks are optional notifications only. Any AI semantic-review search tool is read-only, bounded, and filters every Qdrant result by project and immutable revision; it supplies cited snippets and trace evidence, never an unrestricted source dump. Project exclusions are loaded before inventory and shared by static analyzers, semantic retrieval, and flow tracing; each report records the patterns and excluded-source count.

## Tech Stack

FastAPI Code Quality router, existing SQLite report database, authenticated Node/Express Web proxy, Vue CodeQualityView, macOS Keychain or Linux Secret Service through the host helper, and a pinned k6 runner.

## Baseline/Authority Refs

- Codebase Quality Assessment specification — normative behavior.
- Runtime Request Detail and AI Analysis specification — sampled request-history contract.
- Runtime ClickHouse Remote Provisioning specification — OS credential-store boundary.
- @api/Python-Service/app/code_quality/http.py
- @api/telemetry-dev/runtime_ssh_credentials.py
- @web/src/components/CodeQualityView.vue
- @web/src/server/codeQualityRoutes.ts
- @api/Python-Service/app/tests/test_code_quality.py
- @web/package.json
- @web/tests/entityModelSearch.test.ts

## Compatibility Boundary

Git-backed projects use the same immutable snapshot, inventory, analyzer, and report pipeline as uploaded projects. Each report records the fetched commit SHA and content identity. Qdrant retrieval is revision-filtered and cannot silently fall back to a rolling or latest collection. Static assessment requests, reports, history, and scores remain unchanged. Benchmark runs use separate records and history. Runtime history remains a sampled source of GET-route suggestions and exposes no credentials. Bearer tokens remain per-target OS credentials; HTTP login credentials, CSRF values, and cookies remain transient. The host helper retains its no-general-purpose-secret-retrieval boundary.

## Change Necessity

A docs-only or configuration-only change cannot provide live route execution, authentication, bounded workloads, or comparable saved results. The minimum code boundary is the existing Code Quality owner, a focused benchmark submodule and UI component, the host credential helper, and its authenticated Web/API integrations.

## Existence Check

- Proposed new surface: Code Quality benchmark runner and setup component with a separate benchmark record type.
- Existing owner / reuse candidate: Code Quality API, report database, view, Runtime request history, and host credential helper.
- Why existing surface is insufficient: static assessment scans immutable source archives and cannot execute selected routes against a live target or represent workload measurements.
- Creation proof: separate immutable results must retain target, routes, auth mode, workload, measurements, and runner provenance without changing static scores.
- Entropy / retirement impact: keep one Code Quality owner, reuse the existing database and credential provider, and add no competing score or secret store.
- Minimality reference: the project reference was not present; the design reuses existing owners and limits additions to a runner module, a view component, and benchmark records.
- Decision: add-with-proof.

## Plan Pressure Test

- Owner / contract / retirement: Code Quality owns setup and results; Runtime owns sampled route history; the host helper owns OS credentials.
- Architecture integrity / higher-level path: add a benchmark submodule under Code Quality and keep the static scoring pipeline separate.
- Verification scope: cover API authorization, origin and route restrictions, credential isolation, login recovery, report comparison, and static compatibility.
- Task executability: existing routers, stores, views, credential helper, and tests provide concrete integration points.
- Pressure result: proceed.

## Plan-Time Complexity Check

- Target files: Code Quality service/router/view, host credential helper, a new benchmark runner module, and a focused setup component.
- Existing size / shape signals: service.py is 412 lines, http.py 145, CodeQualityView.vue 391, and runtime_ssh_credentials.py 399.
- Owner fit: benchmark configuration and results belong under Code Quality; route history and OS credential access remain with existing owners.
- Add-in-place risk: placing runner logic in service.py or setup controls in CodeQualityView.vue would mix distinct lifecycles in already substantial files.
- Better file boundary: isolate runner and report behavior in app/code_quality/performance.py and setup controls in a Code Quality child component.
- Recommendation: add owner file within Code Quality; extend existing router and helper narrowly.

## TDD Route

Mode: off. Decision: skipped. Authority: project default. Test posture: no test-first sequence. Reason: the user did not request TDD. Verification: focused API and Web checks after implementation.

## Tasks

1. Trace the existing Code Quality router, service, proxy, Runtime request sampler, report database, and host credential-helper boundaries.
2. Inventory source files across framework layouts and exclude generated or operational artifacts.
3. Add benchmark configuration, execution, report, and history models in @api/Python-Service/app/code_quality/performance.py.
3. Persist target profiles and workload settings separately in @api/Python-Service/app/code_quality/performance.py.
4. Add authorized route-suggestion endpoints in @api/Python-Service/app/code_quality/http.py and @web/src/server/codeQualityRoutes.ts.
5. Extend @api/telemetry-dev/runtime_ssh_credentials.py with per-target Bearer save, replace, removal, and scope-limited benchmark-use operations.
6. Implement bounded k6 execution in @api/Python-Service/app/code_quality/performance.py with origin validation, selected-route enforcement, and workload limits.
7. Implement transient HTTP login, optional CSRF bootstrap, per-virtual-user session reuse, and excluded login timing in the runner.
8. Persist immutable, sanitized benchmark runs in SQLite and expose history and comparison metadata through Code Quality endpoints.
9. Add @web/src/components/code-quality/PerformanceSetup.vue for target, route, auth, workload, progress, and recovery controls.
10. Integrate Setup Performance and separate benchmark history into @web/src/components/CodeQualityView.vue.
11. Register benchmark endpoints through @api/Python-Service/app/main.py and the authenticated Web proxy.
12. Document target setup, credential handling, workload limits, result interpretation, and recovery in README.md.
13. Add API tests in @api/Python-Service/app/tests/test_code_quality_performance.py and Web tests in @web/tests/codeQualityPerformance.test.ts.
14. Capture provider token usage or a labeled estimate in each static report.
15. Expose token usage in report history and per-report totals in comparisons.
16. Implement Git source synchronization using @.archcore/code-quality/git-source-synchronization.plan.md.
17. Implement bounded semantic flow tracing using @.archcore/code-quality/semantic-flow-tracing.plan.md.
18. Persist validated project-level exclusions as project-relative path patterns.
19. Apply one filtered business-source inventory to static analyzers, retrieval, and flow tracing.
20. Add a Code Quality editor and record effective exclusions in each report.
21. Mark report comparisons non-equivalent when exclusion patterns differ.
22. Register targeted language/framework analyzers for Reliability and Performance alongside the Semgrep baseline.
23. Expand category-specific retrieval and report analyzer gaps without executing project test commands.

## Acceptance Criteria

- The Setup Performance flow conforms to the approved target, route-selection, authentication, and workload behavior.
- Users can select sampled GET routes, adjust virtual users and duration, and inspect saved benchmark runs.
- Bearer tokens are stored per target in the OS credential store and reused only for selected scenarios.
- Cookie-based scenarios use per-run HTTP login credentials, optional CSRF bootstrap, and one session per virtual user.
- Login and CSRF requests are excluded from selected-route measurements.
- Benchmark reports retain per-route latency percentiles, throughput, errors, target, routes, auth mode, workload, timestamp, and runner version.
- Benchmark history and comparisons remain separate from static assessment reports and identify non-equivalent inputs.
- Reports, logs, model context, and application databases contain no Bearer tokens, login credentials, CSRF values, or session cookies.
- Static source inventory is independent of `src/` layout, excludes generated and operational artifacts, and reports excluded source-like files.
- Git Sync compares the fetched branch head with indexed revisions, creates immutable snapshots for new commits, and can recover from missed webhook events through fetch-and-compare.
- Developer-installed Git hooks are optional and cannot be the only change-detection mechanism.
- AI semantic retrieval returns bounded evidence only from the revision under assessment; reports identify stale or incomplete indexes and unresolved flow edges.
- Static reports persist provider-reported token usage or labeled input/output estimates; history and comparisons show per-report totals.
- Existing static scoring, assessment history, and Runtime request history retain their behavior.
- Users can edit project-scoped file and directory patterns; the analyzer, semantic retriever, and flow tracer honor the same saved source scope.
- Reports preserve effective exclusions and excluded-source counts; comparisons identify changed exclusion scopes.
- Reliability and static Performance use normalized static analyzer findings and revision-scoped semantic evidence; missing runtime behavior evidence leaves affected claims unassessed.

## Verification

- API: verify static inventory across framework-specific source layouts and confirm build, dependency, cache, log, generated, temporary, and user-configured paths are excluded from source-based evidence.
- API: verify exclusion validation, project persistence, report provenance, comparison equivalence, and empty-inventory behavior.
- API: verify static analyzer selection, normalized findings, analyzer coverage by language and category, malformed output, timeouts, and missing-evidence semantics.
- Runtime image: verify the configured pinned Semgrep executable exists and reports its actual version; retain an actionable analyzer failure when unavailable.
- API: run focused tests for benchmark configuration, route selection, target isolation, credential-store failures, login/CSRF behavior, persistence, and comparisons.
- API: run existing code-quality tests and the new benchmark-focused tests.
- Web: run `npm test`, `npm run lint`, and `npm run build` from `web/`.
- Runtime: confirm the local Docker target is reachable and the runner enforces origin, route, concurrency, and duration limits.
- Security: confirm credentials never enter API responses, SQLite, logs, reports, or model context.
- Usage accounting: confirm provider usage is retained exactly when available and fallback counts are labeled estimates.
- UI: verify target and route selection, auth configuration, per-project exclusions, progress and recovery states, separate history, keyboard operation, and responsive scrolling.

## Dependencies

- Runtime telemetry provides sampled GET route history.
- The local Docker target exposes an address reachable by the benchmark runner.
- Supported desktop hosts provide Keychain or Linux Secret Service through the existing credential helper.
- The configured test identity can authenticate and access selected routes.
- The deployment includes the pinned k6 runner.

## Risks

- Runtime history is sampled and may omit routes, limiting available suggestions.
- Webhook delivery can be delayed or missed; explicit and scheduled fetch-and-compare must reconcile branch state.
- Git hosting providers differ in authentication and webhook formats; the connector must isolate provider adapters behind one source-sync contract.
- Qdrant search may return incomplete semantic matches; deterministic symbol/flow traversal and explicit unresolved states are required for claims about complete paths.
- Login and CSRF flows vary; the HTTP configuration must bound request shapes and token extraction without arbitrary scripts.
- Expired or insufficient credentials produce incomplete runs that cannot be compared as complete benchmarks.
- Configurable workload can affect target health; explicit run controls and enforced maximums limit accidental load.
- The credential handoff must preserve host-helper isolation while allowing only the selected benchmark run to use its target token.

## Retirement

Keep static scoring and Runtime history as independent owners. Do not add a second static Performance score or a parallel credential store. No existing benchmark path requires retirement.
