---
title: "Synchronize Code Quality Sources from Git"
status: draft
tags:
  - "api"
  - "code-quality"
---

## Goal

Allow a user to connect a Git repository and selected branch, reconcile branch commits with immutable indexed revisions, and assess only complete fetched snapshots.

## Tasks

1. Trace project authorization, source archive, snapshot registry, and indexing job ownership.
2. Define provider-neutral repository and branch configuration using the existing project access boundary.
3. Add secret-safe credential storage and retrieval for supported Git providers.
4. Fetch the selected branch head and resolve its full commit SHA.
5. Compare the fetched SHA and source checksum with registered immutable revisions.
6. Reuse an existing complete indexed revision when commit and source identity match.
7. Create and queue an immutable snapshot when the fetched commit is not indexed.
8. Verify webhook signatures and treat events only as synchronization triggers.
9. Add user-triggered fetch-and-compare reconciliation for missed webhook events.
10. Expose repository status, current commit, indexing state, and recoverable errors through authorized API routes.
11. Add authenticated Web controls for repository, branch, connection status, and synchronization.
12. Add API and Web tests for authorization, credential redaction, reconciliation, duplicate commits, and indexing failures.
13. Document supported providers, credentials, webhook setup, synchronization behavior, and recovery.

## Acceptance Criteria

- Repository and branch settings remain scoped to the authorized user and project.
- Repository credentials never appear in API responses, reports, model context, or logs.
- A fetched commit creates at most one immutable snapshot for matching project, commit, and source checksum.
- Existing complete indexed snapshots are reused without duplicate indexed content.
- Assessments cannot use a fetched revision until indexing reaches the ready state.
- Webhooks trigger a verified fetch; webhook payload commits alone never become trusted current revisions.
- User-triggered fetch-and-compare reconciles missed or invalid webhook events.
- Synchronization failures identify the affected project and actionable recovery without exposing credentials.
- Uploaded-source projects retain their existing assessment behavior.

## Dependencies

- The accepted Codebase Quality Assessment specification defines repository identity, branch reconciliation, immutable revisions, webhook behavior, and failure handling.
- The snapshot registry, archive storage, and indexing queue provide the immutable revision pipeline.
- The project access layer provides user-scoped authorization.
- Supported Git providers and credential mechanisms require confirmation during implementation planning.