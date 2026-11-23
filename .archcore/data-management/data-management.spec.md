---
title: "Data management: inventory, planned deletion and audit"
status: draft
tags:
  - "api"
  - "auth"
  - "frontend"
---

## Purpose & Scope

Show every piece of data the app stores (Qdrant, Neo4j, MinIO, ClickHouse, the local SQLite stores, files on disk). Global administrators can remove data after a dry run. Every other user sees only their own projects, read-only.

## Surface

- Inventory, ownership, planning, jobs and audit: @api/Python-Service/app/data_admin/ (`inventory.py`, `ownership.py`, `deletion.py`, `journal.py`, `preview.py`, `http.py`).
- Authorization and per-user scoping: @web/src/server/dataAdminRoutes.ts. The global administrator list is in @web/src/server/oidcAuth.ts.
- UI: @web/src/components/DataAdminView.vue and @web/src/components/data-admin/DeletionDialog.vue (the System → Data management tab).

## Normative Behavior

1. WHEN the inventory runs, the service MUST classify each item as revision, project, shared, legacy, orphan, global or transient.
2. WHEN a store cannot be reached, the inventory MUST report that store as unavailable and still list the other stores.
3. WHEN a non-administrator reads the inventory, the web server MUST return only items of their projects, plus their own rows of shared items.
4. WHEN a non-administrator requests a preview of shared or global data, the web server MUST refuse it.
5. WHEN a preview returns stored values, the service MUST mask values whose keys look like secrets.
6. WHEN a user who is not a global administrator plans or executes a deletion, the web server MUST refuse it with 403.
7. WHEN an administrator requests a deletion, the service MUST return a dry-run plan of the steps, the store each step touches and estimated counts.
8. WHEN an administrator executes a plan, the service MUST plan again and refuse with 409 if the hash of the steps changed.
9. WHEN a project purge is executed, the web server MUST require the typed project id as confirmation.
10. WHEN a project purge runs, the service MUST remove each revision through revision removal, and only then remove the project's leftover rows, points, nodes, objects and settings.
11. WHEN an item deletion targets data that a revision or project owns, the planner MUST refuse it and direct the administrator to revision removal or project purge.
12. WHEN a deletion job runs, the service MUST run one job at a time and record the status of each step.
13. WHEN a dry run or execution happens, the service MUST write an audit entry with the actor, the target, the plan and the result.
14. WHEN a project purge job succeeds, the web server MUST remove that project's memberships.
15. WHEN listing revisions, the inventory MUST derive usage labels and reasons from the retention classifier in @api/Python-Service/app/telemetry/retention.py.
16. WHEN retention considers a ready revision removable, Data management MUST label that revision Unused.
17. IF revision protection checks fail, THEN Data management MUST label affected revisions Unknown and offer refresh guidance.
18. WHEN displaying revision usage, Data management MUST distinguish retained, not-ready, and unused revisions without changing deletion authorization.

## Constraints & Invariants

- Raw tus upload objects and unregistered workspaces stay protected from deletion for 24 hours after their last change.
- File deletion stays inside the managed directories and never follows symlinks.
- Registry tables (`snapshots`, `deployments`) change only through revision removal.
- Python endpoints require the internal secret. They trust the `X-User-Id` and `X-User-Email` headers only when the secret is present.

## Failure Behavior

1. IF a step fails, THEN the job MUST stop, mark the remaining steps skipped, and report the error so the administrator can retry.
2. IF the service restarts while a job runs, THEN the journal MUST mark that job failed and allow a retry.
3. IF another deletion job is running, THEN the service MUST refuse a new execution with 409.

## Conformance

- @api/Python-Service/app/tests/test_data_admin.py covers ownership, plan hash refusal, project purge isolation, orphan deletion, path containment, the internal secret and the running-job guard.
- @web/tests/dataAdmin.test.ts covers viewer scoping, the admin-only deletion endpoints, purge confirmation, membership removal and the last-administrator guard.
