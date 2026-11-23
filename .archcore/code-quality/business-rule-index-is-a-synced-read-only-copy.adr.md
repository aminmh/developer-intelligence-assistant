---
title: "Business Rule Index Is a Synced Read-Only Copy of Outline"
status: draft
tags:
  - "api"
  - "code-quality"
  - "frontend"
---

## Context

Business rules live in one Outline collection per project. Today they are read live, at assessment start, and only the assessment report keeps a snapshot (@api/Python-Service/app/code_quality/outline.py, `fetch_business_rules`). The semantic-review spec forbids stale Outline content for assessments.

Developers also need the rules outside assessments: linked to the methods, routes and tables that implement them, in chat answers, in diff impact checks, next to runtime metrics, and as test skeletons. Fetching Outline live for each of those is not possible: the Outline token never reaches Python. Node obtains it per request from the desktop credential helper (@web/src/server/codeQualityRoutes.ts, `callOutlineCredentialHelper`).

## Decision

1. The api keeps a read-only **rule index**: one row per Outline document, keyed by project, document id and revision, with `synced_at`. Removed documents are soft-deleted.
2. Node starts every sync: a "Sync now" action, a 10-minute scheduler for connected projects, and every assessment (which already fetches the rules). Python stores the result and records added, changed and removed events.
3. Assessments keep fetching Outline live. The no-stale-rules rule of the semantic-review spec still applies to assessments.
4. Every feature that shows content from the index also shows its sync time.
5. The app never creates, edits or deletes Outline documents. Links, review states and drift flags live in the app.
6. A per-project setting, off by default, decides whether rule text may be sent to a remote model or a remote embedding provider. Assessments keep their current behaviour.

## Alternatives

- **Live fetch everywhere.** Rejected: Python has no token, and chat would wait on Outline for every question.
- **Store the token in Python.** Rejected: the token stays in the OS credential store by design.
- **Write links back into Outline.** Rejected: the integration is read-only and the token may lack write rights.

## Consequences

- Chat, links and drift work while Outline is unreachable, using the last sync, labelled with its time.
- A rule edited in Outline shows up after the next sync, at most about 10 minutes later while the desktop helper runs.
- Rule text is stored in `code_quality.sqlite` and, when allowed, embedded in a per-project Qdrant collection.
