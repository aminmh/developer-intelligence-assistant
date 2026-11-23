---
title: "Checking Real Responses Against the Contract Without Storing Bodies"
status: draft
tags:
  - "api"
  - "runtime-telemetry"
---

## Goal

Decide how the runtime check (check C of @.archcore/frontend-assist/frontend-developer-assistance.plan.md, tasks 22–23) can compare what the back end really answers with the API contract, given that runtime telemetry stores no response bodies. Timebox: one working session, reading existing code only, on 2026-11-14.

## Questions

1. What does a server request span carry today?
2. Can any contract comparison be made from that alone?
3. What would have to change to compare response fields with contract schemas?

## Approach

Read the span table schema (@api/Python-Service/app/telemetry/span_schema.py), the collector's privacy transform (@api/telemetry-dev/collector-runtime.yaml), and the PHP adapter that emits spans (the separate `indexr-otel-library` checkout: `packages/core/src/ServerRequestSpan.php`, `packages/laravel/src/RequestTelemetryMiddleware.php`, `docs/attribute-schema.md`).

## Findings

1. A server span carries `http.route` (the route template, never the concrete path), `http.request.method`, and `http.response.status_code`. The span table materializes `HttpRoute` (@api/Python-Service/app/telemetry/span_schema.py).
2. The collector keeps a fixed list of span attributes and drops everything else (`transform/runtime_privacy`, line 37 of @api/telemetry-dev/collector-runtime.yaml). No attribute describes a response body.
3. The adapter's own schema forbids exporting request or response bodies (`docs/attribute-schema.md`, "Privacy and identity requirements").
4. Route, method, and status code are enough for two comparisons: routes that receive traffic but are not in the contract, and status codes an operation returned that its contract responses do not list.
5. Comparing response fields needs a new span attribute. It would have to be produced inside the PHP process (the only place the body exists), added to the collector's keep list, and read from the span attributes map. That is a change in three places, one of them a separate repository.
6. A response "shape" (key paths and value types, no values) is not a body, but key names can still be data: a JSON object keyed by email address or by record ID exposes those keys. A shape attribute therefore needs its own privacy rule, such as keeping only keys that the contract schema already names.

## Recommendation

- Do now: the route and status-code comparison from data that already exists. Built on 2026-11-14 as `runtime_report` in @api/Python-Service/app/api_contracts/conformance.py, reading counts through @api/Python-Service/app/api_contracts/runtime_observations.py.
- Defer: response-field comparison. It needs a decision from the user on changing the privacy rule in finding 3, and work in `indexr-otel-library`.
- If it is approved, the lowest-risk design is: the adapter computes, for JSON responses only, a bounded list of key paths with value types (depth 4, 200 paths, no array indexes, no values); it keeps only paths whose keys match `^[A-Za-z_][A-Za-z0-9_]{0,63}$`; it sends the list as one attribute on sampled spans; the collector keep list gains that one attribute.

## Next Action

Ask the user whether response key names may leave the PHP process under the rule above. Until then, task 23 stays limited to routes and status codes. Not verified against a running ClickHouse: the local stack was stopped during this session, so the query in `runtime_observations.py` is covered by unit tests with a stand-in reader only.
