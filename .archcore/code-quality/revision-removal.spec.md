---
title: "Project revision removal"
status: draft
tags:
  - "api"
  - "code-quality"
  - "frontend"
---

## Purpose & Scope

Remove one immutable revision and its application-owned source and index data while preserving other revisions and shared project settings.

## Surface

- Removal service: @api/Python-Service/app/code_quality/revision_deletion.py.
- Authorized route: @api/Python-Service/app/code_quality/http.py and @web/src/server/codeQualityRoutes.ts.
- Confirmation and selection: @web/src/components/CodeQualityView.vue.
- Retention (automatic removal): @api/Python-Service/app/telemetry/retention.py, run by the index worker loop in @api/Python-Service/app/main.py; settings in Project access (@web/src/App.vue).

## Normative Behavior

1. WHEN a project administrator confirms removal, the service MUST scope cleanup to the selected project and revision.
2. WHEN removing a revision, the service MUST remove its stored source archives, graph nodes, vector data, and assessment reports.
3. WHEN runtime storage is configured, the service MUST remove supported revision-scoped telemetry records.
4. WHEN an indexing, assessment, or rebuild job holds the revision lock, the service MUST reject removal for retry.
5. WHEN a storage operation fails, the service MUST retain a non-ready revision record for retry.
6. WHEN removal succeeds, the service MUST remove the revision registry record.
7. WHEN removal succeeds and no other revision has the same source archive checksum, the service MUST remove the archive's stored repo map and code-tools source cache.
8. WHEN retention runs, it MUST remove only ready revisions beyond the project's `keep_latest` (default 3, minimum 1) newest non-working-tree revisions and beyond the newest working-tree revision.
9. WHEN retention runs, it MUST NOT remove the project's latest ready revision, a revision with an assessment report, a revision with a running deployment, a revision pinned in Project access, or a Git source's last synced revision.
10. WHEN retention runs, it MUST run between index jobs and leave a busy or failing revision for the next pass.

## Constraints & Invariants

Shared project settings and other revisions remain available. Runtime tables must expose revision identity for scoped deletion. External analyzer data is outside the local removal surface.

## Failure Behavior

1. IF a running deployment uses the revision, THEN the service MUST reject removal.
2. IF cleanup is incomplete, THEN the API MUST report failure and permit retry.

## Conformance

Verification covers authorization, confirmation, scoped deletion, active job exclusion, partial failure, and retry.