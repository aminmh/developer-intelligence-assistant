---
title: "Knowledge Base upload revision identity"
status: draft
tags:
  - "frontend"
---

## Purpose & Scope

ZIP uploads use server-created immutable revisions without requiring Git metadata. Git synchronization retains fetched commit identity. The user approved automatic upload revisions on October 31, 2026.

## Surface

- Identity model and registry: @api/Python-Service/app/telemetry/snapshot.py and @api/Python-Service/app/telemetry/registry.py.
- Direct and resumable storage: @api/Python-Service/app/index/upload_service.py, @api/Python-Service/app/index/storage.py, and @api/Python-Service/app/main.py.
- Upload form and lifecycle: @web/src/services/uploadIdentity.ts, @web/src/services/tusUpload.ts, @web/src/App.vue, @web/src/components/KnowledgeBaseView.vue, @web/src/components/RAGFiles.vue, and @web/src/composables/useProjectFiles.ts.
- Revision selection: @web/src/code-quality/projectRevisions.ts and @web/src/components/CodeQualityView.vue.

## Normative Behavior

1. WHEN uploading ZIP source, the API MUST require an authorized project ID and accept an omitted Commit SHA.
2. WHEN Commit SHA is supplied, the API MUST validate its full Git SHA format.
3. WHEN receiving complete archive bytes, the server MUST compute SHA-256 and register an immutable revision ID.
4. WHEN an upload omits Git metadata, the identity model MUST set commit_sha to null and source_ref to upload_ followed by its archive checksum.
5. WHEN source bytes match an existing project source reference, the registry MUST reuse its revision ID.
6. WHEN source bytes conflict with an existing Git source reference, the registry MUST reject the archive.
7. WHEN showing Knowledge Base, the view MUST explain automatic revision creation and label Commit SHA optional.
8. WHEN uploads complete, the view MUST display their server-assigned revision IDs.
9. WHEN indexing or assessing source, each service MUST constrain evidence to the selected project, revision, source reference, and archive checksum.
10. WHEN authorizing uploaded source, the Web proxy MUST verify project access against authoritative archive metadata.
11. WHEN a file batch starts, the uploader MUST retain its selected files and project identity throughout that batch.
12. WHEN selecting assessment revisions, the view MUST include uploads without Git metadata.
13. WHEN registering a revision, the registry MUST record its source kind: `upload`, `git`, or `working_tree`.
14. WHEN a desktop helper uploads a working tree, the identity model MUST set `dirty` to true, `commit_sha` to null, and `source_ref` to `wt_` followed by the archive checksum.
15. WHEN a working tree has a Git `HEAD`, the registry MUST store it as `base_commit_sha` and never as `commit_sha`.
16. WHEN accepting a working-tree upload, the Web server MUST verify an unrevoked grant for the project.
17. WHEN accepting a working-tree upload, the Web server MUST verify that the granting user still has operator or administrator access.
18. WHEN storing a working-tree grant, the API MUST keep only the SHA-256 of its token.

## Constraints & Invariants

Git SHA and upload checksum remain separate values. Existing Git collections and revision IDs retain their identities. The registry's historical SQL commit_sha key column remains a private storage slot for source references; public metadata never represents an upload reference as a Git commit. Runtime deployment correlation requires actual Git metadata, so working-tree revisions never match a deployment. Working-tree revisions are not Knowledge Base uploads and do not appear in its file list. App owns upload identity refs; the form and uploader share one validator.

## Failure Behavior

1. IF project identity is invalid, THEN the form MUST block selection and explain the field error.
2. IF supplied Git metadata is malformed, THEN the uploader MUST stop before creating a session.
3. IF graph or vector persistence is incomplete, THEN the assessment precheck MUST keep the revision unavailable.
4. IF caller-supplied revision identity conflicts with stored source, THEN the API MUST reject the request.

## Conformance

Verification covers optional SHA validation, server checksums, repeated uploads, project isolation, immutable Git conflicts, direct and TUS storage, archive authorization, revision selection, and assessment filters.