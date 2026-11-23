---
title: "Implement Automatic Upload Revisions"
status: draft
tags:
  - "api"
  - "frontend"
---

## Goal

Remove the manual Git requirement for ZIP uploads while preserving immutable indexing, retrieval, and assessment identity.

## Tasks

1. Extend snapshot identity with nullable Git metadata and canonical upload source references.
2. Adapt registry lookup and archive storage while preserving existing Git collection keys and revision IDs.
3. Register direct and completed resumable uploads with checksums and expose their server-assigned revision IDs.
4. Update parsing, chat authorization, rebuilds, retrieval, and assessment to handle upload revisions.
5. Make Commit SHA optional and display upload revision IDs in Knowledge Base and Assessment.
6. Verify upload, identity isolation, Git compatibility, frontend typing, and production build.

## Acceptance Criteria

A user can upload source using only a project ID and ZIP. The server computes checksum and revision identity. Git metadata remains optional and truthful. Unchanged source uploads reuse revision identity within their project. Assessment retains full graph readiness checks. Existing Git revisions and reports remain usable.

## Dependencies

@.archcore/knowledge-base/knowledge-base-upload-identity.spec.md and @.archcore/code-quality/codebase-quality-assessment.spec.md.

Implementation extends existing identity owners. No new identity service or alternate indexing profile is introduced.