---
title: "Browser Telemetry Through a Same-Origin Relay"
status: draft
tags:
  - "frontend"
  - "runtime-telemetry"
---

## Summary

Proposal for tasks 29–31 of @.archcore/frontend-assist/frontend-developer-assistance.plan.md: let a front-end application report page loads, Web Vitals, JavaScript errors, and slow interactions, and join each to the back-end trace of the requests it made, so one trace runs from a click to its SQL queries. Nothing here is built. It needs three decisions from the user before work starts, listed under Detailed Design.

## Motivation

The Runtime page explains a slow request from the server inward. A front-end developer's question starts earlier: which screen was slow, and was it the browser or the API. The pieces to answer it mostly exist:

- The PHP adapter already accepts an incoming `traceparent` header and continues that trace, behind the setting `indexer-otel.trust_traceparent`, which is off by default (`packages/laravel/src/RequestTelemetryMiddleware.php` in the `indexr-otel-library` checkout).
- The UI map (@api/Python-Service/app/projects/frontend/ui_map.py) knows each project's route templates, so a browser span can carry a route template instead of a URL.

What is missing is a way for browser spans to reach storage, and permission for them to pass the collector's privacy filter.

## Detailed Design

**Decision 1 — how browser spans reach the collector.** The collector listens only on the local machine (@api/docker-compose.yml publishes `127.0.0.1:24318`), and its privacy filter drops any span whose resource lacks a project, a revision, and the schema marker (@api/telemetry-dev/collector-runtime.yaml). A browser on the internet cannot reach it, and should not: an open ingestion address lets anyone write spans. Proposed: the browser posts to a path on its own application's back end, and the PHP adapter relays the batch to the collector, stamping the project and revision itself. The browser never learns the collector's address, and the back end's own session and rate limits apply.

**Decision 2 — what a browser span may carry.** The collector keeps a fixed list of span names and attributes. Proposed additions: span names `browser.page`, `browser.interaction`, `browser.error`; attributes `browser.route` (a route template from the client router, never a URL), `browser.vital.lcp_ms`, `browser.vital.inp_ms`, `browser.vital.cls`, `browser.interaction.duration_ms`, and the existing `error.type`. No URL, query string, element text, selector, or user agent string.

**Decision 3 — trusting the browser's trace ID.** Joining needs `trust_traceparent` on for the back end. A caller can then choose the trace ID of its own request. Proposed: accept it only for requests that carry the application's session, and only on the relay's own origin.

**The snippet.** A dependency-free script of about 3 KB, configured at build time with the relay path and a sampling rate. It reads Web Vitals through `PerformanceObserver`, listens for `error` and `unhandledrejection`, times interactions longer than 200 ms, adds `traceparent` to same-origin `fetch` and `XMLHttpRequest` calls only, and sends batches with `navigator.sendBeacon` when the page is hidden.

**Front-end revision.** The browser span is stamped with the back-end project's revision, because the relay is the back end. The front-end project and revision travel as two extra attributes that the relay copies from the snippet's build-time configuration, checked against `^[A-Za-z0-9_.-]{1,128}$` and `^rev_[0-9a-f]{32}$`.

**Reading.** The Runtime request detail already loads a trace by ID (@api/Python-Service/app/telemetry/runtime.py). It would show a browser span as the root when one exists, and the UI map would link a route to its recent page loads.

**Work, in order.** Relay route and `trust_traceparent` rule in `indexr-otel-library`; collector keep-list change; the snippet with unit tests; Runtime detail and UI map links; a guide for installing the snippet.

## Drawbacks

- Three places change, one a separate repository, and the collector's privacy filter is loosened for the first time since it was written.
- Every sampled page view adds a request to the application's back end.
- A browser can send false timings. The data is evidence about the user's experience, not a measurement the system can vouch for.
- Front-end and back-end code deploy separately, so a browser span can name a front-end revision that the index does not hold.

## Alternatives

- **Publish the collector with a token.** The token ships in the page, so it protects nothing. Rejected.
- **Relay through the Developer Assistant web server.** End users' browsers cannot reach it in most deployments, and it would need cross-origin access from every monitored site. Rejected.
- **Back end only: add `Server-Timing` and stop there.** No Web Vitals and no JavaScript errors; it answers "was it the API" but not "which screen". Kept as the fallback if decision 1 is refused.
- **A hosted real-user-monitoring product.** Sends user traffic data to a third party, which the project has avoided so far.
