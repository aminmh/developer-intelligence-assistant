---
title: "Contract Conformance Findings"
status: draft
tags:
  - "api"
  - "frontend"
---

## Purpose & Scope

This spec is normative for the **conformance report**: the result of comparing one side of an API with one contract version. Three checks produce it and share its shape, so the web app, chat tools, and later automation read one format:

- **consumer** — a front-end revision's requests and models against the contract (built in Phase 3).
- **provider** — a back-end revision's routes against the contract (built in Phase 4, Laravel and Symfony).
- **runtime** — the routes and status codes the running back end was seen to answer, against the contract (built in Phase 4). Response fields are not compared: telemetry holds no response bodies.

It also covers the **change impact report**, which lists what a contract change touches in a consumer project.

Out of scope: how contracts are stored (@.archcore/frontend-assist/api-contract-sources-and-versions.spec.md) and how the UI map is extracted.

## Surface

- Checker: @api/Python-Service/app/api_contracts/conformance.py.
- Inputs: a contract version model (@api/Python-Service/app/api_contracts/model.py) and, per check, the UI map of a revision (@api/Python-Service/app/projects/frontend/ui_map.py), a revision's back-end routes (@api/Python-Service/app/projects/backend_routes.py), or request counts per method, route, and status code (@api/Python-Service/app/api_contracts/runtime_observations.py).
- Report fields: project, revision, attachment, contract source and version, checker version, `summary`, `coverage`, `findings`.
- Finding fields: `finding_id`, `check`, `code`, `severity` (`error`, `warning`, `info`), `message`, `operation`, `location` (file, line, symbol), `evidence`.
- Consumer codes: `unknown_operation`, `method_not_in_contract`, `deprecated_operation`, `unresolved_address`, `unknown_query_parameter`, `missing_required_parameter`, `missing_request_body`, `unexpected_request_body`, `unknown_body_property`, `missing_required_property`, `model_property_not_in_contract`, `model_missing_property`.
- Provider codes: `undocumented_route`, `unimplemented_operation`.
- Runtime codes: `undocumented_route_observed`, `undocumented_status`.
- Impact entry fields: `operation`, `change` (`removed`, `changed`, `schema`), `parts`, `schemas`, `calls`, `components`, `routes`.

## Normative Behavior

1. WHEN a report is requested for an attachment, the checker MUST use the contract version that attachment resolves to.
2. WHEN a report is requested without a revision, the checker MUST use the project's newest ready revision.
3. The checker MUST name the project, revision, contract source, contract version, and checker version in every report.
4. WHEN the same revision is checked against the same contract version, the checker MUST return the same findings with the same IDs.
5. WHEN a request address matches no contract path, the checker MUST report `unknown_operation` with severity `error`.
6. WHEN a request matches a contract path under a method the contract lacks, the checker MUST report `method_not_in_contract` with severity `error`.
7. WHEN a request address is built at run time, the checker MUST report `unresolved_address` with severity `info`.
8. WHEN a request matches a deprecated operation, the checker MUST report `deprecated_operation` with severity `warning`.
9. WHEN the checker saw only part of a request's parameters or body, the checker MUST NOT report missing parameters or properties for it.
10. WHEN a front-end model and a contract schema share a name, the checker MUST report model properties the schema lacks as `model_property_not_in_contract`.
11. The checker MUST count requests checked, requests unresolved, operations used, and operations no request uses under `coverage`.
12. WHEN an impact report compares two versions, the checker MUST list each removed operation and each operation whose parameters, request body, or responses changed.
13. WHEN a changed schema is referenced by an otherwise unchanged operation, the checker MUST list that operation with change `schema`.
14. WHEN an impact entry has matching requests, the checker MUST list the components that make them and the routes that render those components.
15. WHEN an impact report is requested for a `provider` attachment, the checker MUST refuse the request.
16. WHEN a report is requested for a `provider` attachment, the checker MUST compare the revision's routes with the contract.
17. WHEN a route under the API's base path matches no contract operation, the checker MUST report `undocumented_route` with severity `warning`.
18. WHEN no route serves a contract operation, the checker MUST report `unimplemented_operation` with severity `error`.
19. WHEN a provider's contract is a file in that project, the checker MUST use the version read from the checked revision.
20. WHEN a route lies outside the API's base path, the provider check MUST NOT report it.
21. WHEN an operation answered with a status code its contract responses do not cover, the runtime check MUST report `undocumented_status`.
22. WHEN a route under the API's base path received requests but is not in the contract, the runtime check MUST report `undocumented_route_observed`.
23. WHEN a runtime check is requested for a `consumer` attachment, the checker MUST refuse the request.

## Constraints & Invariants

- Constraint: a finding's severity is `error` only when the request cannot work against the contract as written. Everything the checker infers from partial information is `warning` or `info`.
- Constraint: a report MUST NOT hold more than 2,000 findings; it states how many were cut.
- Invariant: a report is derived data. It is computed from an immutable revision and an immutable contract version and stores nothing of its own.
- Invariant: a finding always cites a file and line, or the model or operation it is about.
- Invariant: path matching ignores the host and any leading run-time variable of a request address.

## Failure Behavior

1. IF the attachment resolves to no valid version, THEN the checker MUST return an explicit unavailable state with no findings.
2. IF the project has no indexed revision, THEN the checker MUST refuse and say that indexing is required.
3. IF the revision holds no Angular, React, or Vue application, THEN the checker MUST return a report with zero requests and a note.
4. IF either version of an impact comparison is not a valid version of the attachment's source, THEN the checker MUST reject the request.
5. IF the revision holds no Laravel or Symfony application, THEN the provider check MUST return a report with zero routes and a note.
6. IF runtime telemetry cannot be read, THEN the runtime check MUST return an explicit unavailable state with the reason.

## Conformance

An implementation conforms when it satisfies behaviors 1–23, holds the invariants, and follows failure rules 1–6.

- Given a consumer project whose code calls `POST /api/orders/{id}/pay` and a contract that defines only `GET /orders/{id}` under base path `/api`,
- When the consumer check runs,
- Then the report holds one `unknown_operation` finding citing the call's file and line,
- And `coverage` lists `GET /orders/{id}` as an operation no request uses.
