---
title: "Assessment index precheck and revision selection"
status: draft
tags:
  - "api"
  - "code-quality"
  - "frontend"
---

## Purpose & Scope

Assessment revision selection includes Knowledge Base uploads and registered source revisions. Assessment requires an available revision index before source analysis or model requests.

## Surface

- Readiness: @api/Python-Service/app/code_quality/index_readiness.py and @api/Python-Service/app/code_quality/http.py.
- Revision inventory: @web/src/code-quality/projectRevisions.ts and @web/src/server/codeQualityRoutes.ts.
- Selection and recovery: @web/src/App.vue and @web/src/components/CodeQualityView.vue.
- Published vector generation resolution: @api/Python-Service/app/index/embedding_rebuild.py.
- Revision payload readers: @api/Python-Service/app/code_quality/design_metrics.py and @api/Python-Service/app/code_quality/flow_tracing.py.
- Source folder preview: @api/Python-Service/app/code_quality/source_view.py.

## Normative Behavior

1. WHEN listing assessment revisions, the UI MUST combine Knowledge Base uploads and registered source revisions by project and commit identity.
2. WHEN an upload lacks a registered revision, the UI MUST display it with an indexing instruction.
3. WHEN selecting a registered revision, the UI MUST request its authenticated precheck before enabling assessment.
4. WHEN assessing a revision, the API MUST repeat the index precheck before retrieving archives or running analyzers.
5. WHEN checking an index, the precheck MUST verify registry readiness, archive registration, matching vector payloads, and matching graph symbol counts.
6. WHEN a precheck blocks assessment, the UI MUST provide its reason, Knowledge Base navigation, and a retry action.
7. WHEN listing revision options, the UI MUST display project IDs and preserve immutable revision identities as option values.
8. WHEN Cohere is selected, revision readers MUST resolve vectors through the published active generation.
9. WHEN checking graph completeness, the precheck MUST use the logical snapshot collection for Neo4j.
10. WHEN reading generation payloads, revision readers MUST preserve project, revision, source reference, archive checksum, and clean-source filters.
11. WHEN the UI loads excluded-folder choices, the API MUST derive directories from the selected revision archive without applying the assessment/model archive limit.
12. WHEN the directory archive exceeds the metadata preview limit, the API MUST use only the selected revision's indexed source paths.
13. WHEN the UI opens a cited source window, the API MUST apply the source-preview limit rather than the assessment/model archive limit.
14. WHEN building folder choices, the API MUST omit archive metadata paths such as `__MACOSX`.
15. WHEN all descendant folders match exclusions, the UI MUST show the parent folder as selected.
16. WHEN a selected parent folder is cleared, the UI MUST clear descendant exclusion patterns.
17. WHEN listing revision options, the UI MUST show working-tree revisions as disabled and never select one by default.
18. WHEN checking a working-tree revision, the precheck MUST block assessment with status `working_tree_unavailable` and its reason.
19. WHEN reusing vectors from earlier revisions, the indexer MUST read only each revision's active Cohere generation, never a retained one.

## Constraints & Invariants

Project and commit scope distinguish revisions. The API verifies project, revision, commit, archive checksum, collection, and clean-source identity. Counts check persisted symbol presence; relationship completeness remains an indexing-time invariant.

## Failure Behavior

1. IF a revision index is absent or incomplete, THEN the API MUST block assessment with an instruction to index in Knowledge Base.
2. IF an index backend is unreachable, THEN the precheck MUST report temporary unavailability without claiming the project is unindexed.
3. IF project access is denied, THEN the Web proxy MUST reject the precheck without contacting the API.
4. IF active Cohere vectors are missing, THEN the precheck MUST request a Cohere rebuild without substituting retained generations.
5. IF the directory archive is empty or invalid, THEN the API MUST report unavailable folder choices.
6. IF the directory archive exceeds its metadata preview limit and no indexed paths are available, THEN the API MUST report unavailable folder choices.

## Conformance

Regression checks cover uploads without snapshots, revision identity isolation, missing vectors or graph symbols, unavailable backends, authorization, and rejection before assessment work.