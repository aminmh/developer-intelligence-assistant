---
title: "Requests Record Their Browser Screen View Instead of Joining Its Trace"
status: accepted
tags:
  - "frontend"
  - "runtime-telemetry"
---

## Context

The user approved @.archcore/frontend-assist/browser-telemetry-through-a-same-origin-relay.rfc.md on 2026-11-14. Its third decision made each back-end request a child of the browser screen view that sent it, by trusting the browser's `traceparent` header.

That was built first and run against the pizza-builder application and the local ClickHouse. Storage was correct, but one screen view's trace then held several `http.server` spans. The Runtime page assumes one request per trace:

- The request list groups by trace and keeps the first server span (`requests` in @api/Python-Service/app/telemetry/runtime.py), so only the first request of a screen view would be listed.
- Request detail takes the first server span of a trace and every database span in it (`request_detail`), so one request would be shown with the SQL of all the others.
- The collector's tail sampling and the request-deletion path also decide per trace.

Trusting the header also lets a page force its requests to be sampled.

## Decision

- A back-end request stays the root of its own trace. The PHP adapter does not use the browser's `traceparent` as a parent.
- For a request from the application's own page (same-origin, with the session cookie), the adapter records the screen view's IDs on the request span as `indexer.browser.trace_id` and `indexer.browser.span_id`.
- A screen view is a trace of its own holding only browser spans: `browser.page`, `browser.interaction`, `browser.error`.
- Reading joins the two by those IDs (@api/Python-Service/app/telemetry/browser.py). The UI map's Live tab shows a screen view with the requests that name it.
- The collector keeps the two ID attributes only when they are 32 and 16 hex characters (@api/telemetry-dev/collector-runtime.yaml).
- Decisions 1 and 2 of the RFC stand as approved: the relay on the application's own origin, and the fixed attribute list.
- The change to decision 3 was made during the build, after the RFC's approval. The user accepted it on 2026-11-14.

## Alternatives

- **Join the trace, as approved.** Rejected: it breaks the Runtime request list and detail unless every per-trace query is rewritten to be per request, and it lets a page force sampling.
- **Join the trace and rewrite the Runtime queries.** Rejected for now: it touches the request list, request detail, AI trace summary, deletion, and collector sampling, for no gain the link does not already give.

## Consequences

- Nothing on the Runtime page changes, and request sampling is unchanged.
- A screen view lists only the requests the back end sampled. With a low back-end sampling rate, many views will show few or no requests.
- Finding a view's requests is a lookup in the span attributes map, not on a sort key. It is scoped by project and the runtime window.
- `INDEXER_OTEL_TRUST_TRACEPARENT` keeps its old meaning and is not needed for browser telemetry.
- Verified on 2026-11-14 against pizza-builder: a screen view with an LCP value and one error, and three requests that named it, each still alone in its own trace.
