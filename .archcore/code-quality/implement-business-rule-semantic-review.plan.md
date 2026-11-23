---
title: "Implement Outline-Backed Business Rule Review"
status: draft
tags:
  - "api"
  - "code-quality"
  - "frontend"
---

## Goal

Use Outline as the canonical authoring source for project business rules and compare those rules with bounded, revision-scoped analyzer, retrieval, and flow evidence. Provide an optional local Outline stack for development.

## Tasks

1. Inspect existing rule storage, project authorization, OS credential storage, Outline API access, and report snapshots.
2. Define project settings for an Outline instance and one dedicated business-rule collection.
3. Store Outline API tokens in the OS credential store and keep secret values out of application persistence.
4. Implement bounded read-only synchronization of rule documents from the mapped collection.
5. Normalize each document into a rule using its stable ID, title, body, update time, and content hash.
6. Replace local rule creation and editing with Outline connection, collection selection, synchronization status, and recovery UI.
7. Add a Markdown export for existing local rules to support manual migration into Outline.
8. Preserve local rules and saved reports until users complete migration; stop using local rules in new assessments.
9. Generate deterministic bounded retrieval queries from synchronized Outline rules and restrict evidence to the assessed revision and effective source inventory.
10. Pass rule statements, relevant findings, semantic excerpts, and applicable traces into the evaluator within its evidence budget.
11. Validate model results against exact rule and evidence IDs, mapping incomplete or invalid results to unknown.
12. Persist immutable report snapshots with Outline document identity, content hash, update time, concise rationale, status, and citations.
13. Add report views that display rule reviews separately from category scores and link citations to source.
14. Add an optional Compose profile for Outline, PostgreSQL, and Redis with persistent file and database volumes.
15. Connect the local Outline service to the project's existing Keycloak OIDC realm and register an Outline client.
16. Add explicit start, stop, status, and log commands without enabling Outline in the normal development stack.
17. Document Outline setup, permissions, credential handling, migration, evidence limits, and unknown results.
18. Review the final diff against assessment, flow-tracing, external-analyzer, and LangChain contracts.

## Acceptance Criteria

- Outline is the only authoring source used for new business-rule assessments.
- Each project maps to one dedicated Outline collection, and each collection document represents one rule.
- The connector reads only the configured collection and never creates, edits, or deletes Outline content.
- Outline credentials are stored in the OS credential store and omitted from logs, model context, and reports.
- Collection synchronization is bounded, deterministic, and reports connection, permission, and parse failures.
- Rule evidence and flow traces remain pinned to the selected project, revision, commit, source checksum, and effective exclusions.
- Reports preserve the exact Outline document identity and content version used; later Outline edits do not change saved results.
- Existing assessment reports remain readable, and existing local rules remain exportable during migration.
- New assessments do not silently use local rules or stale Outline cache content.
- Model results cite supplied evidence only; absent or incomplete evidence produces unknown.
- Empty Outline collections preserve ordinary category scoring and clearly show that rule review is not configured.
- User interfaces expose Outline setup and rule reviews without mixing them with Semgrep rules or category scores.
- The local Outline profile starts Outline, PostgreSQL, and Redis as separate containers with persistent named volumes.
- The normal development start leaves Outline stopped; explicit Outline commands manage its lifecycle without deleting volumes.
- The local Outline host port binds to loopback, and the Outline service uses the project's OIDC provider.
- Rule-derived prompts remain bounded and report deterministic truncation.
- No project commands, tests, or arbitrary tools execute during review.

## Verification

- Inspect authorization and project scoping across Outline configuration and synchronization endpoints.
- Verify token storage uses the OS credential store and logs, responses, and reports never expose tokens.
- Verify connector requests remain read-only and restricted to the configured Outline instance and collection.
- Verify collection permission errors, unavailable server, malformed documents, size limits, and timeouts.
- Verify deterministic rule ordering, evidence-budget accounting, and truncation reporting.
- Verify historical reports preserve snapshots after Outline documents change or disappear.
- Verify legacy local-rule Markdown export and that new assessments exclude local rules.
- Verify Compose interpolation, dependency health checks, OIDC client configuration, loopback port binding, and named-volume persistence.
- Verify normal development startup does not select the Outline profile.
- Verify Outline stop commands preserve persistent volumes.
- Inspect report serialization to confirm transient source snippets and credentials are omitted.
- Inspect UI states for unconfigured, loading, synchronized, stale/error, empty, and cited-result cases.
- Do not claim execution or automated verification without fresh evidence.

## Dependencies

- The Evidence-Grounded Business Rule Review specification defines Outline mapping, snapshot, evidence, unknown-state, and local-stack behavior.
- The Codebase Quality Assessment specification defines project authorization, immutable revisions, analyzer evidence, and report persistence.
- The Semantic Flow Tracing specification defines applicable trace evidence and incomplete-flow semantics.
- An Outline API token with read-only access to the configured collection.
- The project's local Keycloak realm for Outline sign-in.
- An OS credential-store integration available to the application runtime.
