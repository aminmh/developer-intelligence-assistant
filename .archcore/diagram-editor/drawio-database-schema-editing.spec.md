---
title: "Draw.io Database Schema Editing"
status: draft
tags:
  - "api"
  - "diagram-editor"
  - "frontend"
---

## Purpose & Scope

This specification replaces the Database Schema view's diagram canvas with a self-hosted Draw.io editor launched in a dedicated subdomain tab. It covers structured schema editing, persisted diagram drafts, and downloadable migration generation.

Entity Model and Domain Knowledge diagrams remain outside this change. Laravel and Symfony with Doctrine are the initial migration targets; the adapter contract supports later frameworks and languages.

## Surface

The Vue Database Schema view opens Draw.io in a new browser tab on a configured, dedicated subdomain served by a self-hosted Docker deployment. The browser uses that browser-reachable origin; Docker service DNS is not assumed to be reachable from the browser.

The application owns a normalized database schema model and stable IDs. The Draw.io document carries diagram layout and references those IDs. The host and editor exchange schema and save messages through Draw.io's popup/embed protocol over `window.opener.postMessage`, validating the configured origins and expected window.

Schema tables, columns, types, nullability, defaults, primary keys, foreign keys, indexes, and relationships are structured data. Visual annotations remain presentation-only.

An edited schema draft belongs to its saved analysis or imported-schema draft. The original analysis remains an immutable baseline. Migration generation returns downloadable files and never writes to or executes against the analyzed project.

## Normative Behavior

1. WHEN a user opens the schema editor, the application MUST open Draw.io in a new browser tab on the configured dedicated subdomain.
2. WHEN the editor tab initializes, the host MUST provide the normalized schema, stable IDs, and saved diagram layout through the validated opener message channel.
3. WHEN the editor sends a message, the host MUST validate its origin, source window, event type, and payload before processing it.
4. WHEN a user edits schema through schema-aware controls, the application MUST update and validate the normalized schema model.
5. WHEN an unrecognized shape or annotation is edited, the application MUST retain it as visual-only data excluded from schema diffs.
6. WHEN a user saves a draft, the application MUST persist its normalized schema and diagram layout against the analysis or import draft.
7. WHEN a saved draft is restored, the application MUST restore its validated schema, stable IDs, and layout.
8. WHEN a user leaves Database Schema or the editor reports exit, the host MUST detach bridge listeners and release editor-session resources.
9. WHEN a saved analysis has a detected framework, the application MUST select that framework's available migration adapter.
10. WHEN an imported schema lacks framework metadata, the application MUST request a target framework and any required database platform details.
11. WHEN computing a schema diff, the application MUST send only structured schema data and adapter guidance to the AI migration service.
12. WHEN Laravel or Symfony with Doctrine is selected, its adapter MUST generate migration files in that framework's conventions.
13. WHEN a new framework adapter is registered, the application MUST discover it through the migration adapter extension contract.
14. WHEN generated migration files pass adapter validation, the application MUST preview them before offering downloads.
15. WHEN a user downloads a migration, the application MUST return the file without writing to or executing against the analyzed project.
16. WHEN a diff includes a drop, destructive type change, or ambiguous rename, the application MUST show before-and-after details and require explicit confirmation before enabling download.
17. WHEN migration generation or validation fails, the application MUST block downloads and explain the actionable failure.
18. WHEN schema entities are selected or navigated, the application MUST preserve Database Schema integrations through stable IDs.
19. WHEN the editor has no pending change or save event, the host MUST avoid polling for document updates.

## Constraints & Invariants

- The normalized schema model and validated schema diff are the only inputs that determine migration semantics.
- Draw.io XML stores presentation and stable-ID references; it is not a migration source of truth.
- The original analysis remains unchanged when a diagram draft is edited.
- Migration generation is limited to Database Schema edits; other diagram types do not create migrations.
- Downloads are reviewable artifacts; the application does not modify or migrate the analyzed project.
- Draw.io opens only after explicit user action; the app keeps no hidden editor view alive. A separate tab does not guarantee lower total CPU use; profiling determines active-editor cost.
- The deployment pins a reviewed Draw.io image version and configures an explicit browser-accessible subdomain and a policy that preserves or safely replaces the opener bridge.

## Failure Behavior

1. IF the Draw.io service is unavailable or the browser blocks the new tab, THEN the application MUST preserve drafts and provide a retry action.
2. IF a bridge message has an invalid origin, source window, type, size, or payload, THEN the host MUST reject it without changing the schema draft.
3. IF schema validation fails, THEN the application MUST retain the last valid draft and identify the invalid entity or field.
4. IF the target framework, platform details, or adapter is unavailable, THEN the application MUST block generation and state what input is missing.
5. IF generated files fail syntax or schema-diff validation, THEN the application MUST block downloads and show bounded diagnostics.
6. IF destructive confirmation is missing, THEN the application MUST keep the affected migration download disabled.
7. IF browser isolation severs the opener bridge, THEN the application MUST preserve the draft and offer a safe recovery path.

## Conformance

Verification covers schema-to-diagram round trips, stable IDs, draft persistence and restoration, bridge rejection, schema diffs, framework adapters, migration validation and downloads, destructive-change confirmation, and preservation of existing Database Schema actions.

Performance profiling compares the current canvas and Draw.io in a separate tab under the same browser and representative schema for load, idle, edit, pan, zoom, close, CPU, memory, and interaction latency. Closed-editor resource use returns to baseline measurement noise after the editor session ends; active-editor costs are reported against a baseline-derived budget before release.