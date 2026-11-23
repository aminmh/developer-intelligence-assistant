---
title: "Add Front-End Developer Assistance Around the API Contract"
status: draft
tags:
  - "api"
  - "frontend"
---

## Goal

Help front-end developers on Angular, React, and Vue projects the way the app already helps back-end developers. Front end and back end live in separate repositories and agree through a Swagger or OpenAPI document, so that document is the hub: each side, and real runtime traffic, is checked against it instead of against the other repository.

Three checks hang off the hub:

- **Check A** — front-end service calls against the contract.
- **Check B** — back-end routes against the contract.
- **Check C** — real responses seen at runtime against the contract.

The user set the frameworks, the separate-repository constraint, and the three contract sources on 2026-11-13.

## Declared Delta

- create spec api-contract-sources-and-versions (@.archcore/frontend-assist/api-contract-sources-and-versions.spec.md)
- create code contract store, ingester, URL fetcher, contracts view (Phase 1)
- create code Angular, React, and Vue framework plugins and the UI map (Phase 2)
- create spec contract conformance findings, shared by checks A, B, and C (Phase 3, not yet written)
- create code checks A, B, C and contract change impact (Phases 3–4)
- create code service, type, and mock generation (Phase 5)
- create code browser telemetry and front-end quality analyzers (Phase 6)
- amend code JS/TS indexing limits and file coverage (@api/Python-Service/app/projects/js_ts_index.py)
- none adr mcp-servers-reach-only-allowlisted-hosts-through-egress-proxy (reused as the model for outbound control, not changed)

## Tasks

### Phase 1 — Contract store

Done 2026-11-13. Code: @api/Python-Service/app/api_contracts/, @api/Python-Service/app/routers/api_contracts.py, @api/Python-Service/app/agent_tools/contracts.py, @web/src/server/contractRoutes.ts, @web/src/components/contracts/ApiContractManager.vue. Tests: @api/Python-Service/app/tests/test_api_contracts.py, @web/tests/contractRoutes.test.ts. The contracts view is a tab of the Knowledge base, not a separate page. Validation is structural, not a full OpenAPI meta-schema check. Working-tree revisions are not read for repository-path contracts.

1. Add tables and a store for contract sources, versions, and attachments. — new module beside @api/Python-Service/app/telemetry/registry.py
2. Add a parser and validator that normalises Swagger 2.0 and OpenAPI 3.x into one model. — no existing target found
3. Add the version diff that lists added, removed, and changed operations and schemas. — no existing target found
4. Add the upload endpoint for contract documents. — pattern in @api/Python-Service/app/index/upload_service.py, routes under @api/Python-Service/app/routers/
5. Ingest the configured path whenever a back-end revision registers. — @api/Python-Service/app/telemetry/git_source.py, @api/Python-Service/app/telemetry/working_tree.py, @api/Python-Service/app/index/indexing_service.py
6. Add the URL fetcher with address, redirect, size, and time checks. — no existing target found; Compose wiring in @api/docker-compose.yml
7. Add attachment endpoints with role, mode, and project access checks. — @api/Python-Service/app/routers/
8. Add the contracts view: sources, versions, validation errors, attachments. — @web/src/App.vue, sibling of @web/src/components/KnowledgeBaseView.vue
9. Add agent tools that list operations and read one operation's schema. — @api/Python-Service/app/agent_tools/catalog.py, @api/Python-Service/app/agent_tools/builtins.py, @api/Python-Service/app/infra/remote_consent.py

### Phase 2 — Front-end index and UI map

Done 2026-11-13. One shared extractor serves all three frameworks: @api/Python-Service/app/projects/frontend/ui_map.py and @api/Python-Service/app/projects/frontend/parsing.py; the plugins in @api/Python-Service/app/agent/plugins/frameworks/frontend/plugin.py only name their framework. Served by @api/Python-Service/app/projects/frontend/service.py and @api/Python-Service/app/routers/frontend.py; chat tool in @api/Python-Service/app/agent_tools/frontend.py; view in @web/src/components/UiMapView.vue. Tests: @api/Python-Service/app/tests/test_frontend_ui_map.py, @web/tests/uiMap.test.ts.

Deviation from task 14: the map is cached as one JSON file per revision and application, not written to the graph database; it already holds the edges that Phase 3 needs. Extraction is syntax-level: it does not resolve types, re-exported barrels, or addresses built at run time, and reports what it could not link. Inertia applications have no client routes; linking Laravel routes to Inertia pages is not done.

10. Index `.vue` single-file components and Angular HTML templates. — @api/Python-Service/app/projects/js_ts_index.py, @api/Python-Service/app/projects/ts_index.py
11. Raise the 400-file limit so real front-end projects index fully. — @api/Python-Service/app/projects/js_ts_index.py (`_MAX_FILES`)
12. Add Angular, React, and Vue framework plugins. — beside @api/Python-Service/app/agent/plugins/frameworks/laravel/ and @api/Python-Service/app/agent/plugins/frameworks/symfony/
13. Extract routes, pages, components, stores, and HTTP calls per framework. — the three new plugins
14. Store the route → page → component → store → HTTP call edges in the graph. — @api/Python-Service/app/index/graph_store.py
15. Add the UI map view for front-end projects. — @web/src/App.vue, new component under @web/src/components/

### Phase 3 — Check A and change impact

Done 2026-11-13. Spec: @.archcore/frontend-assist/contract-conformance-findings.spec.md. Code: @api/Python-Service/app/api_contracts/conformance.py (matching, consumer check, impact), service and routes in @api/Python-Service/app/api_contracts/service.py and @api/Python-Service/app/routers/api_contracts.py, chat tool `api_contract_check` in @api/Python-Service/app/agent_tools/contracts.py, view in @web/src/components/contracts/ContractCheckPanel.vue. Tests: @api/Python-Service/app/tests/test_contract_conformance.py. The UI map now records each request's query names, body keys, and the project's TypeScript models. Reports are computed on request and not stored. Response fields the code reads are not checked; only declared models are compared with schemas of the same name.

16. Write the conformance findings spec that checks A, B, and C share. — @.archcore/frontend-assist/
17. Match each extracted HTTP call to a contract operation by method and path template. — no existing target found
18. Report calls to unknown operations, wrong parameters, and models that disagree with schemas. — no existing target found
19. Map a version diff to the affected services, components, and routes. — uses the edges from task 14
20. Show findings and change impact per front-end project in the web app. — new component under @web/src/components/

### Phase 4 — Checks B and C

Built 2026-11-14, not yet run in the app (the local stack was stopped). Check B: route reading in @api/Python-Service/app/projects/backend_routes.py (Laravel through the existing parser, Symfony attributes, annotations, and YAML were added), `provider_report` in @api/Python-Service/app/api_contracts/conformance.py. Spike (task 22): @.archcore/frontend-assist/response-shapes-from-runtime-telemetry.rnd.md. Check C is limited to routes and status codes (`runtime_report`, @api/Python-Service/app/api_contracts/runtime_observations.py); comparing response fields waits on a privacy decision and work in `indexr-otel-library`. Tests: @api/Python-Service/app/tests/test_backend_routes.py. The ClickHouse query has unit tests with a stand-in reader only.

21. Compare extracted back-end routes with contract operations. — @api/Python-Service/app/agent/plugins/frameworks/laravel/routes.py, @api/Python-Service/app/agent/plugins/frameworks/symfony/plugin.py
22. Run a spike on capturing response shapes without storing response bodies. — @api/Python-Service/app/telemetry/span_schema.py, @api/telemetry-agent/agent.py
23. Compare captured response shapes with contract schemas per operation. — depends on the outcome of task 22
24. Show provider-side findings on the back-end project. — new component under @web/src/components/

### Phase 5 — Generation

Built 2026-11-14, not yet run in the app (the local stack was stopped). Generators: @api/Python-Service/app/api_contracts/codegen/typescript.py and @api/Python-Service/app/api_contracts/codegen/generators.py; targets `models`, `angular`, `react`, `vue`, `mocks`. Output is a pure function of the contract version; no model writes the code. Exposed through `generate_code` in @api/Python-Service/app/api_contracts/service.py, the chat tool `api_contract_generate`, and @web/src/components/contracts/ContractCodePanel.vue. Tests: @api/Python-Service/app/tests/test_contract_codegen.py. Output for the public Petstore contracts (OpenAPI 3 and Swagger 2) passed `tsc --strict` for every target on 2026-11-14, with stand-in typings for Angular, React, RxJS, and MSW and the real Vue typings.

Task 28 changed: no TypeScript language-server adapter was added, because it needs a language server installed in the service image. Each generation is syntax-checked with the Tree-sitter TypeScript parser instead. One service is generated per contract (no split by tag); header and cookie parameters are not sent by generated code.

25. Generate typed models from a contract version. — no existing target found
26. Generate Angular `HttpClient` services, React hooks, and Vue composables per operation. — the three plugins from task 12
27. Generate mock data and mock-server handlers from schemas and examples. — no existing target found
28. Add a TypeScript language-server adapter to validate generated code. — @api/Python-Service/app/lsp/adapters/, @api/Python-Service/app/config/lsps.yml

### Phase 6 — Browser telemetry and quality

Partly built 2026-11-14, not yet run in the app (the local stack was stopped).

- Task 32, changed: ESLint, `tsc`, axe, and bundle-size tools need Node and the project's dependencies in the analyzer image, so they were not added. Instead @api/Python-Service/app/code_quality/frontend_checks.py runs in-process rules for accessibility, security, reliability, performance, and maintainability on Vue, Angular, and JSX source. They are served per revision (`checks_for_revision` in @api/Python-Service/app/projects/frontend/service.py), shown in the Checks tab of @web/src/components/UiMapView.vue, and joined to assessments in @api/Python-Service/app/code_quality/service.py as the analyzer `frontend-checks`. Accessibility findings are reported but not scored, because accessibility is not one of the five assessment categories. Tests: @api/Python-Service/app/tests/test_frontend_checks.py. The assessment integration has no end-to-end test.
- Tasks 29–31 were built on 2026-11-14 after the user approved @.archcore/frontend-assist/browser-telemetry-through-a-same-origin-relay.rfc.md, with its third decision changed by @.archcore/runtime-telemetry/requests-record-their-browser-screen-view-instead-of-joining-its-trace.adr.md. In the `indexr-otel-library` checkout: the snippet `packages/core/resources/browser.js`, the relay `packages/core/src/BrowserTelemetry.php`, and the Laravel routes and screen-view tag. Here: the collector keep list in @api/telemetry-dev/collector-runtime.yaml, reading in @api/Python-Service/app/telemetry/browser.py, and the Live tab of @web/src/components/UiMapView.vue. Verified end to end against pizza-builder with the snippet injected by hand; the snippet is not yet in that application's layout. Laravel only: the Symfony adapter has no relay. The Runtime request detail does not show a request's screen view.
- Task 33 is not built.

29. Provide a browser telemetry snippet that propagates trace context to the back end. — setup pattern in @scripts/setup-php-telemetry.sh
30. Store Web Vitals, JavaScript errors, and slow interactions as spans. — @api/Python-Service/app/telemetry/span_schema.py
31. Show a browser-to-SQL trace in the runtime view. — @web/src/components/RuntimeView.vue
32. Register ESLint, `tsc`, accessibility, and bundle-size analyzers. — @api/Python-Service/app/code_quality/analyzers.py, @api/Python-Service/app/code_quality/external_analyzers.py, @api/analyzer-worker/worker.py
33. Compare form validation with indexed business rules. — @api/Python-Service/app/code_quality/business_rules.py

### Phase 7 — API change alerts in the IDE

Built 2026-11-21. Spec: @.archcore/frontend-assist/contract-change-notifications.spec.md; decision: @.archcore/frontend-assist/ide-tokens-are-personal-read-only-bearer-tokens.adr.md. User choices: VS Code first; contract changes and back-end code drift; live push over SSE with polling fallback; diagnostics, toast, side panel, and regenerate quick fix. Verified by unit tests (Python in Docker, web, extension) and an end-to-end smoke on throwaway ports (Python → `/api/ide` → extension client); not yet run inside a real VS Code window against the full stack.

34. Classify contract changes as breaking, risky, or safe. — @api/Python-Service/app/api_contracts/breaking.py
35. Detect provider code drift between revisions. — @api/Python-Service/app/api_contracts/code_drift.py
36. Store change events and push them to consumers. — @api/Python-Service/app/api_contracts/store.py, @api/Python-Service/app/api_contracts/events.py
37. Add IDE tokens and `/api/ide` routes. — @web/src/server/ideRoutes.ts, @web/src/server/oidcAuth.ts, @web/src/components/config/IdeTokensCard.vue
38. Build the VS Code extension. — @ide/vscode/

## Acceptance Criteria

- Phase 1: one contract arrives by each of the three sources; a second back-end sync with a changed contract file yields a second version; a front-end project attached as consumer resolves to it. Tests cover refused addresses and failed validation.
- Phase 2: one sample project per framework indexes without hitting a file limit, and its UI map lists its routes, components, and HTTP calls with file and line.
- Phase 3: on a sample project with seeded mismatches, check A reports each seeded mismatch and no finding on the matching calls; a contract change lists the components that use the changed operation.
- Phase 4: check B reports a seeded undocumented route and a seeded unimplemented operation on Laravel and Symfony samples. Check C is accepted only after the spike in task 22 closes.
- Phase 5: generated code for each framework passes TypeScript validation against its sample project.
- Phase 6: one browser interaction is visible as a single trace down to its SQL query.
- Every phase leaves chat, indexing, and assessment of existing PHP projects working.

## Dependencies

- @.archcore/frontend-assist/api-contract-sources-and-versions.spec.md — Phase 1 implements it.
- @.archcore/knowledge-base/sync-creates-new-revisions-and-reuses-unchanged-vectors.adr.md — contract versions from a repository path follow immutable revisions.
- @.archcore/agent-tools/remote-model-source-access-requires-project-consent.adr.md — contract text and front-end source obey the per-project consent setting.
- @.archcore/agent-tools/mcp-servers-reach-only-allowlisted-hosts-through-egress-proxy.adr.md — the egress proxy it describes is not built (task 12a of @.archcore/agent-tools/langchain-tool-orchestration.plan.md is deferred), so task 6 carries its own outbound checks.
- Runtime spans carry no response body fields today (@api/Python-Service/app/telemetry/span_schema.py), which is why check C starts with a spike.
- Embedding quota: front-end projects add indexed chunks under the same daily embedding limit as back-end projects.
- Phase order: 1 before 3 and 4; 2 before 3; 3 before 5; Phase 6 depends on none of the others.

## Clarifications

- 2026-11-23: committed locally on `feature/agent-code-tools`, split from other uncommitted work. In **api**, aa10243 holds the `api_contracts` package, `projects/frontend`, the frontend plugin, contract and frontend tools and routers, front-end checks, browser telemetry, JS/TS template chunks and their tests. It leaves out data admin, the assessment budget message and the runtime `hide_unmatched_404` filter. On a clean copy of the staged tree the suite had the same 37 failures as HEAD, with 128 more tests passing. In **web**, d9e45ef holds the UI map, the API contracts tab, IDE tokens, the global-administrator list (`authIsGlobalAdmin` everywhere; the user chose to include it because the new routes use it) and the shared toast stack the new components report through. It leaves out data admin, runtime schema loading, the assessment token limit, upload identity editing and file index status. Its staged tree passed `tsc`, 100 of 100 tests (HEAD: 77), and both builds. Not pushed; GitHub already carries this work.
- 2026-11-22: Laravel routes in @api/Python-Service/app/projects/backend_routes.py now come from the Laravel plugin's PHP facts (`routes_from_facts.provide_routes`), so `use … as Alias` controller imports resolve to the real class; the regex reader (`parse_routes_from_source`) remains only as a fallback when facts cannot be exported, and `Route::view`/`redirect` stay skipped. Found by the change impact evaluation on pizza-builder, whose `routes/api.php` imports admin controllers under aliases (@.archcore/agent-tools/implement-change-impact-analysis.plan.md, T19). The `withRouting` test fixture became a valid Laravel 11 `bootstrap/app.php`. Uncommitted, with this plan's other files.
