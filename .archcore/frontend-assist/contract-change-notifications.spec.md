---
title: "Contract Change Notifications"
status: draft
tags:
  - "api"
  - "frontend"
---

## Purpose & Scope

Normative for **change events**: what the system records when an API contract, or the provider code behind it, changes in a way consumers may feel, and how those events reach front-end developers in their IDE. Covers classification, code drift, event storage, delivery (SSE and polling), and the `/api/ide` surface.

Out of scope: how contract versions are stored (@.archcore/frontend-assist/api-contract-sources-and-versions.spec.md) and the impact report shape (@.archcore/frontend-assist/contract-conformance-findings.spec.md). Token storage is decided in @.archcore/frontend-assist/ide-tokens-are-personal-read-only-bearer-tokens.adr.md.

## Surface

- Classifier: @api/Python-Service/app/api_contracts/breaking.py (`classify`, `max_severity`).
- Code drift: @api/Python-Service/app/api_contracts/code_drift.py (`drift_items`, `changed_handlers`).
- Events: tables `contract_change_events`, `contract_change_acks` in @api/Python-Service/app/api_contracts/store.py; emission and reading in @api/Python-Service/app/api_contracts/service.py; push in @api/Python-Service/app/api_contracts/events.py.
- Python routes: `GET /contracts/events/stream`, `GET /contracts/projects/{p}/events`, `GET /contracts/projects/{p}/events/{e}`, `POST .../ack` in @api/Python-Service/app/routers/api_contracts.py.
- Web routes: `/api/ide/*` and `/api/ide-tokens` in @web/src/server/ideRoutes.ts.
- Client: VS Code extension in @ide/vscode/.
- Item fields: `severity` (`breaking`, `risky`, `safe`), `code`, `operation`, `pointer`, `message`.
- Event fields: `event_id`, `seq`, `source_id`, `provider_project_id`, `kind` (`contract`, `code`), from/to version and revision, `max_severity`, `summary`, `items`.

## Normative Behavior

1. WHEN a new valid version follows an earlier one, the service MUST record one `contract` event with the classified items.
2. WHEN a document is unchanged or is the first version, the service MUST NOT record an event.
3. WHEN a response property is removed or changes type, the classifier MUST mark it `breaking`.
4. WHEN a request property or parameter becomes required, the classifier MUST mark it `breaking`.
5. WHEN a request property is removed, the classifier MUST mark it `risky`.
6. WHEN an operation is removed, the classifier MUST mark it `breaking`; WHEN one is added, `safe`.
7. WHEN a response property becomes optional or nullable, or gains an enum value, the classifier MUST mark it `risky`.
8. WHEN a provider revision becomes ready, the service MUST compare its routes with the previous ready committed revision.
9. WHEN a documented route disappears from code and no other route serves it, the drift check MUST report `route_removed_in_code` as `breaking`.
10. WHEN a documented operation's handler file changed and the contract did not, the drift check MUST report `handler_changed_contract_unchanged` as `risky`.
11. WHEN an event's most severe item is `breaking` or `risky`, the service MUST push it to streams of `follow_latest` consumers.
12. The service MUST list every event of a consumed contract to its consumers, pinned ones included.
13. WHEN a client lists events with `after`, the service MUST return only events with a higher `seq`.
14. WHEN event detail is requested, the service MUST compute impact from the consumer's newest ready revision.
15. WHEN a user acknowledges an event, the service MUST record it for that user only.
16. WHEN an `/api/ide` request arrives, the web server MUST authenticate its IDE token and re-check project access.
17. WHEN an IDE token acknowledges an event, the web server MUST record the token owner, ignoring any user in the request.

## Constraints & Invariants

- An event holds at most 500 items, most severe first, in a stable order.
- A pushed message carries at most 5 headline items; full items come from the detail route.
- Impact stays derived: events store items, never consumer findings.
- Handler response shapes are not inferred; a handler change is only a signal.

## Failure Behavior

1. IF event classification fails, THEN the service MUST still store the new contract version.
2. IF code drift fails, THEN the revision hook MUST log it and indexing MUST continue.
3. IF a stream's queue is full, THEN the service MUST drop the message; the client backfills by polling.
4. IF the consumer has no UI map, THEN event detail MUST return no entries and a note.

## Conformance

Covered by @api/Python-Service/app/tests/test_contract_change_events.py, @web/tests/ideRoutes.test.ts and @ide/vscode/test/units.test.ts.

- Given web consumes Pets API with `follow_latest`,
- When the provider uploads a version without `Pet.tag`,
- Then web's stream receives one `contract` event headed by `response_property_removed`,
- And event detail cites each call to an operation returning `Pet`.
