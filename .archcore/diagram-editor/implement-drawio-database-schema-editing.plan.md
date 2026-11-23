---
title: "Implement Draw.io Database Schema Editing"
status: draft
tags:
  - "api"
  - "diagram-editor"
  - "frontend"
---

## Goal

Replace only the Database Schema diagram canvas with a self-hosted Draw.io editor launched in a dedicated subdomain tab, preserving app integrations and enabling Laravel and Symfony/Doctrine migration downloads.

### Change Necessity

The current Database Schema view uses Vue and X6. Dragged positions are recomputed on load, and existing migration suggestions are review-only rather than downloadable framework migration files. The requested schema editing, persistent layouts, and framework-specific file generation require a new owner for diagram editing and a bounded migration capability.

### Architecture

After a user action, the Vue host opens one Draw.io editor tab on a dedicated, configured subdomain and communicates through a validated `window.opener.postMessage` bridge. The application-owned normalized schema and stable IDs remain canonical; Draw.io XML stores layout and references.

The API persists schema drafts with analysis artifacts and computes explicit before-and-after schema diffs. An AI migration service generates candidate files through framework adapters, validates them, and returns downloads only. An optional adapter capability integrates with the existing framework plugin registry without requiring every FrameworkPlugin to change.

### Tech Stack

Vue, TypeScript, Draw.io popup/embed protocol, Docker Compose, FastAPI/Python, analysis artifact storage, and the existing framework plugin registry. Initial migration adapters target Laravel and Symfony with Doctrine.

### Baseline References

- `web/src/components/DatabaseSchemaView.vue` and `web/src/components/ErdCanvas.vue`: current schema view and X6 rendering.
- `web/docker-compose.yml`: web services and shared `devassistant_net`; browser traffic needs a reachable published or proxied Draw.io origin.
- `api/Python-Service/app/analysis/analysis_store.py`: per-analysis artifact persistence.
- `api/Python-Service/app/agent/interfaces/framework.py` and `api/Python-Service/app/agent/core/registry.py`: framework plugin contract and discovery.
- `api/Python-Service/app/agent/plugins/frameworks/laravel/schema_visualization.py` and `.../symfony/schema_visualization.py`: existing schema knowledge.
- `api/Python-Service/app/evaluator/service.py`: review-only migration suggestion behavior.
- `docs/UI_OVERVIEW.md`: existing Database Schema interactions and page behavior.

### Compatibility Boundary

Keep SQL, DBML, and normalized JSON imports; existing analysis snapshots; stable entity navigation; search and runtime overlays; and all non-Database-Schema diagram views. Preserve existing FrameworkPlugin behavior for plugins without migration capability. Never write generated migration files into the analyzed project or apply them.

### Architecture Review

Required before implementation because the change crosses the browser origin and opener boundary, persisted analysis artifacts, backend generation APIs, framework plugins, and Docker deployment.

### TDD Route

Off. Use focused verification after each slice and acceptance testing at the end; no strict RED/GREEN sequence is required.

## Tasks

1. Inventory Database Schema actions, imports, runtime overlays, navigation, and current X6 references.
2. Define normalized schema draft and diff contracts with stable table, column, key, index, and relationship IDs.
3. Define Draw.io serialization rules and a validated, size-bounded postMessage bridge.
4. Add analysis artifact persistence and restoration for schema drafts and diagram layout.
5. Add an optional migration adapter contract discovered through the existing framework registry.
6. Implement Laravel and Symfony/Doctrine adapters for schema operations and target-specific validation.
7. Add AI migration generation from structured diffs and adapter guidance, with bounded diagnostics.
8. Add migration preview, destructive-change review, explicit confirmation, and download responses.
9. Add target framework and database-platform selection for imported schemas without detected metadata.
10. Expose the pinned Draw.io Docker service through a browser-reachable dedicated subdomain and reviewed proxy configuration.
11. Add a user-triggered launch action for the Draw.io editor tab.
12. Implement schema-aware editing controls in the Draw.io editor.
13. Preserve app selection, search, runtime overlays, and navigation through stable schema IDs.
14. Reject invalid bridge origins, source windows, message types, payloads, oversized documents, and invalid schema changes.
15. Remove obsolete X6 schema rendering after interaction parity, retaining reusable parsers and normalizers.
16. Profile the old canvas and the Draw.io editor tab with matching datasets across load, idle, edit, pan, zoom, and close.
17. Verify adapter output, draft lifecycle, destructive safeguards, downloads, deployment access, and existing imports.
18. Verify opener behavior under production browser security headers and add a backend session bridge if those headers sever it.
19. Update deployment and user documentation with subdomain setup, supported adapters, and migration-download behavior.

### Remaining TODO — November 2026

The following work remains open for later; supported migration operations remain bounded and require review.

1. Complete deployment acceptance for read-only inspection and Runtime schema loading with a real remote SSH host and authenticated Draw.io service.
2. Capture complete column definitions and distinguish unknown values from absent or verified attributes.
3. Preserve supported imported column metadata and surface missing attributes for confirmation in Draw.io.
4. Expand migration coverage for unsupported attributes, expressions, PostgreSQL auto-increment, and dependent multi-object changes.
5. Verify generated migrations with real AI providers, MySQL, PostgreSQL, Laravel, and Doctrine in disposable environments.
6. Preserve Draw.io navigation, search, runtime overlays, exports, and entity selection before retiring the X6 schema canvas.
7. Compare X6 and Draw.io resource use and latency on representative schemas, including closed-editor resource release.
8. Verify production origin, security headers, and opener behavior; implement a backend bridge if isolation requires it.
9. Review the pinned Draw.io image and deployment configuration before production rollout.

Independent PostgreSQL default and nullability changes across columns are implemented. Confirmed collation changes are diffed and generate reviewed candidates for PostgreSQL and MySQL; a newly supplied definition remains a confirmation when the baseline is unknown. Normalized JSON preserves MySQL definitions on re-import, and SQL imports recognize PostgreSQL serial variants. Draw.io provides table and column search. The focused backend suite passed 124 tests; 18 frontend checks and TypeScript validation passed. Representative statements ran on PostgreSQL 16 and MariaDB 10.6, and generated additive candidates ran with Laravel 12 and Doctrine Migrations 3.9 against disposable MariaDB. A live AI-provider preview was blocked because the provider was unavailable. Missing auto-increment values now remain unknown through saved analyses, imports, and Draw.io, and generation blocks operations that would have to guess the setting. The focused backend suite passed 126 tests, eight frontend import/runtime checks passed, TypeScript validation passed, and 49 browser round-trip checks passed. Earlier drafts that already recorded false cannot recover unknown source values. Laravel and Doctrine now generate explicit unique-index create and drop statements for both supported platforms; removal still requires confirmation. The backend suite passed 127 tests, and isolated PostgreSQL 16 and MariaDB 10.6 checks confirmed uniqueness and removal semantics. These database checks exercised SQL rather than a live AI provider or complete framework migrations. Structured schema JSON import and export retain named composite keys, indexes, relationships, actions, and stable IDs; the visual import preview identifies that composite relationships display their first column. Ten frontend checks, TypeScript validation, and 50 browser round-trip checks passed. The API remains responsible for validating the complete structure before draft persistence. DBML imports now preserve named column indexes, composite primary keys, relationship names, column lists, actions, and endpoint direction; unrepresentable index expressions, explicit index types, unnamed indexes and many-to-many references block draft creation. Fourteen frontend checks and TypeScript validation passed, and an imported composite DBML draft passed API validation and persistence/restoration. SQL imports retain supported named primary and unique constraints, composite foreign keys and named inline or standalone column indexes, including uniqueness, supported actions and qualified table identities. Expression, partial and ordered indexes, checks, and unsupported foreign-key attributes block draft creation. Twenty frontend checks and TypeScript validation passed; an imported SQL index draft passed API persistence/restoration and produced a stable-ID removal diff. SQL parser coverage remains limited. Draw.io now offers JSON export of the current visual schema, including unsaved edits, through the bounded editor bridge; export does not save the draft. The bridge and editor action passed focused automated checks and TypeScript validation, but live-browser download verification remains open. Draw.io also exports representable DBML from the current diagram after warning that IDs and some metadata are not preserved; unsafe identifiers or types are rejected. Focused checks cover DBML re-import, composite constraints, relationship actions, defaults, and the editor request flow; a live-browser download remains unverified. The connected application and Draw.io editor can now explicitly reveal a selected table or relationship by stable ID in either direction; a selected column opens its original table details. Missing original or draft entities are reported, and the application details remain the original schema. Bridge and plugin checks passed; the complete cross-tab interaction remains unverified in a live browser. Laravel and Doctrine migration previews now support multiple independent index additions, including mixed unique and non-unique indexes, on existing columns. Validation permits reordering those additions while still rejecting missing or extra generated statements; combined removals and mixed dependent operations remain blocked. Migration and schema-source checks passed 127 tests with a mocked AI provider and PHP lint; the new combined operation was not run against live databases or frameworks. Migration previews now also support adding supported nullable columns and their indexes in one draft; generated statements preserve column-before-index order, and reordered model output is rejected. Migration and schema-source suites passed 137 tests with a mocked model and PHP lint across both framework adapters and database targets. The new combined operation has not been executed against live databases or frameworks. New unnamespaced tables with named primary and unique keys now generate Laravel and Doctrine migration candidates for supported columns; MySQL primary keys require the name PRIMARY. Explicit column collation or MySQL definition metadata now blocks column additions until it can be retained instead of being silently omitted. Migration and schema-source suites passed 153 tests using a mocked AI provider and PHP lint; new-table key output has not been executed against live databases or frameworks. New-table migration candidates also create named unique and non-unique indexes after table creation. Candidate validation rejects missing or reordered index statements and conflicting new key and index names. Migration and schema-source suites passed 163 tests with a mocked provider and PHP lint; the new statements have not been executed against live databases or frameworks. Other import coverage, column metadata coverage, read-only inspection, editor parity, full framework/database coverage, performance, and production verification remain open.

## Runtime Database Loading — November 14, 2026

Implemented the supplied manual Runtime loading workflow. Runtime Containers links preserve project, environment and selected container. Database Schema manually verifies PostgreSQL/MySQL/MariaDB candidates and selects accessible databases and PostgreSQL schemas, defaulting to the configured database/public schema. The final Load schema in Draw.io click reserves a tab, extracts metadata, creates a new immutable baseline and editable draft with provenance, then opens the saved draft. Page selection and Runtime refresh do not extract or launch editors. Separate Open Draw.io shortcuts open the configured service independently. Extraction failures preserve the current draft; popup/editor failures retain the saved draft with a reopen action.

The desktop helper owns local verified-Docker and explicitly SSH-associated extraction; telemetry reporters remain reporting-only. Scoped authorization and signed receipts bind user, origin, source, operation, database and schemas. Inventory and inspected container identity, running state, labels, image and reporting host are checked before access; extraction rechecks the container afterward. Configured credentials and supported secret files stay on the execution host. Existing clients run fixed read-only catalog queries, with 60-second and 4-MiB failure limits. Ordered columns, exact types, literal versus expression defaults, identity/auto-increment, composite keys, indexes, foreign keys and actions survive normalization. Unsupported definitions remain visible with coverage warnings; baseline and draft metadata restrict unsafe migrations. No model call loads or draws a schema. Owners and setup: @.archcore/diagram-editor/runtime-schema-loading.doc.md and @web/README.md.

Verification on November 14, 2026:

- 229 API tests passed; one unrelated PHP-parser test skipped. Coverage includes receipts, provenance persistence, immutable baselines, administrator/origin checks, stale sources, metadata preservation and migration restrictions.
- 22 helper tests passed, including scope/expiry, exact identity, SSH fingerprint/association checks, configured accounts and secret files, read-only commands, diagnostic suppression, time/output bounds, literal defaults and generated metadata.
- Three real disposable catalog tests passed on PostgreSQL 16, MySQL 5.7 and MariaDB 11.4. They cover multiple databases/PostgreSQL schemas, composite constraints/actions, identity/auto-increment, literal and expression defaults, unsupported indexes, deterministic IDs and JSON transport. Tests used cached images, isolated networks, no published ports and automatic cleanup.
- 50 focused Node tests passed for manual loading, imports, bridge validation, authorization and proxying. TypeScript validation passed. A helper-sign → Node JSON parse/stringify → API verification probe preserved numeric strings, booleans, Unicode and long metadata.
- An actual Vue browser harness passed manual discovery/extraction, saved adoption, edit/save/restore with stable IDs, failure preservation, blocked popup reopen, editor timeout/retry, standalone/missing-configuration shortcuts and a 375px layout. HTTP helper/API boundaries and the separate-origin editor protocol were fixtures. 54 browser XML checks passed, including maximum expression/identity fields and long notes.

Live remote SSH and the complete authenticated application with actual Draw.io UI remain unverified. Broader editor profiling, framework/AI migration coverage, production origin/opener/security headers, image review and rollout remain open; Runtime tests do not close those tasks.

## Acceptance Criteria

- Database Schema uses the Draw.io editor while Entity Model and Domain Knowledge remain unchanged.
- Draw.io runs from a pinned self-hosted container and opens in a new tab on its configured dedicated subdomain after explicit user action.
- Schema edits and layouts survive save, navigation, and restore without mutating the original analysis.
- Migration diffs come from normalized schema changes; annotations and unrecognized shapes never affect migrations.
- Laravel and Symfony/Doctrine produce validated, previewable migration files for supported operations.
- Additional frameworks can register migration adapters without implementing new methods on legacy plugins.
- Destructive or ambiguous changes require review and explicit confirmation before download.
- Migration output is downloaded only; no project file is written and no migration is executed.
- The cross-origin editor bridge validates exact origins, expected source windows, message types, and payloads; browser security headers preserve the bridge or use the supported backend session fallback.
- Existing imports, schema navigation, search, runtime overlays, and stable entity references continue working.
- Invalid messages, unsupported changes, unavailable adapters, and invalid generated code fail with actionable feedback.
- Closed-editor browser CPU returns to baseline measurement noise after the editor tab closes and its session resources are released.
- Active-editor CPU, memory, and interaction latency are recorded against the current canvas; rollout uses an agreed baseline-derived budget.

## Dependencies

- The Draw.io container image, version, distribution terms, and deployment configuration receive review before rollout.
- The Draw.io subdomain is reachable from the app and permitted by network and content-security policies.
- Saved analyses expose framework and database-platform metadata where available; imports collect missing target details.
- Laravel and Doctrine migration conventions are validated against supported framework and database versions.
- Product review sets the active-editor performance budget after baseline profiling.
- The Draw.io Database Schema Editing specification defines the required behavior and failure handling.