---
title: "Data Deletions Are Dry-Run Planned, Hash-Checked and Audited"
status: draft
tags:
  - "api"
  - "frontend"
---

## Context

A data-management page that can delete data in six stores is dangerous. The admin might delete something other than what they reviewed, delete half of a revision, or leave no trace of who deleted what.

## Decision

- Every deletion starts as a plan from @api/Python-Service/app/data_admin/deletion.py. The plan is a list of steps derived from the current inventory.
- The plan hash covers each step's action and arguments, not the row estimates. Execution plans again and refuses with 409 when the hash differs.
- Data owned by a revision or a project is removed only through `remove_revision` or a project purge, which calls `remove_revision` first. Item deletion is limited to orphan, legacy, shared and transient items.
- Jobs run one at a time in a background thread. Step status is persisted in the `data_admin_jobs` table in `snapshots.sqlite`.
- Every plan and execution writes to the `data_admin_audit` table in the same file.

## Alternatives

- **Delete immediately with only a confirm dialog:** the admin would not see what would be removed in each store.
- **Store plans server-side by id:** re-planning and comparing a hash is stateless and catches the same drift.

## Consequences

- New data arriving between the dry run and execution (for example, a new revision) forces a new review. Counts that only grew do not.
- Audit entries carry the project ids, so viewers can see the history of their own projects.
