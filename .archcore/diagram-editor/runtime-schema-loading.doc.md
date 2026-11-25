---
title: "Load Runtime databases into Draw.io"
status: draft
tags:
  - "api"
  - "diagram-editor"
  - "frontend"
  - "runtime-telemetry"
---

## Overview

Runtime database loading creates a new immutable schema baseline and editable draft after an administrator explicitly selects a reporting container and authorizes extraction. The supplied Runtime loading plan defines this workflow; the existing Draw.io serializer, persistence, and bridge remain its owners.

## Content

### Workflow and owners

@web/src/components/runtime/RuntimeSchemaLoader.vue discovers sources manually, verifies engines through the desktop helper, selects databases and PostgreSQL schemas, and reserves an editor window during the final click. @web/src/utils/runtimeSchema.ts obtains fresh operation-scoped authorization and forwards the helper's signed extraction envelope without altering it. Runtime navigation preserves project, environment and container. Standalone Open Draw.io shortcuts open only the configured service.

@web/src/server/runtimeSchemaRoutes.ts requires administrator access and the application origin for authorization. Its short-lived tokens bind user, origin, project, environment, host, exact container and image identity, operation, database and schema selection. @api/Python-Service/app/analysis/schema_runtime.py checks fresh reporting identities and verifies the helper receipt before persistence. @api/Python-Service/app/analysis/schema_drafts.py saves each successful Runtime load as a separate draft with baseline and provenance; reloading never replaces an older draft.

@api/telemetry-dev/runtime_schema.py owns narrowly scoped helper discovery, SSH association and extraction. @api/telemetry-dev/runtime_setup.py exposes those operations on the authenticated local helper; telemetry reporters retain reporting duties only. Local execution pins the verified local Docker endpoint. Remote execution requires a separately stored schema-host association, explicit fingerprint verification, the existing Runtime SSH agent and OS credential store, and existing Docker permissions. Host association verification compares the reporting certificate and inspected container identity. ClickHouse storage settings do not define schema host associations.

Before discovery and extraction, the helper validates exact running container ID, project, environment, Runtime opt-in labels, image and reporting host. It rechecks the container after extraction. Stale or mismatched sources fail without creating drafts. @api/telemetry-dev/runtime_schema_catalog.py detects engines from inspected image/configuration and executes fixed metadata queries through existing in-container clients under read-only transactions. Configured credentials and supported secret-file settings are resolved within the execution container, never returned to the browser or placed in client command arguments. Application users take precedence over bootstrap root settings; failures never retry as another account.

Extraction is bounded to 60 seconds and 4 MiB of command output. Oversized imports fail instead of truncating their baseline. Captured structure includes ordered columns, exact types, nullable/default/generation attributes, composite constraints, indexes, foreign keys and referential actions. Source identity and qualified names determine stable IDs. Runtime provenance records project, environment, reporting host, container/image, engine/version, database, schema selection, extraction time and coverage.

Unsupported definitions remain inspection metadata with visible coverage warnings. @api/Python-Service/app/analysis/schema_migrations.py checks both immutable baseline and draft metadata, preventing removal of warnings from bypassing generation restrictions. MySQL/MariaDB retain SHOW CREATE metadata and conservatively restrict affected table migrations. One narrow exception (decided 2026-11-25): a plain new column, meaning one without its own expression default or identity, never re-declares the table's other columns, so it may be generated on a table that has catalog notes, expression defaults or identity columns, and on a MySQL table whose namespace is its database (the preview warns that the migration runs against the connected database). Changes, drops, renames, keys, indexes, relationships and PostgreSQL namespaces stay manual. New columns and new tables accept timestamp, varchar(n) and char(n) (n up to 65535) besides the base types; changing an existing column keeps the narrow base list. Supported structural editing, save/restore and downloads reuse existing controls; extraction and drawing make no model call.

### Verification boundary

@api/telemetry-dev/test_runtime_schema.py covers scoped authorization, stale and mismatched identities, secret handling, local endpoint verification, SSH key pinning/associations, timeout and output bounds. @api/telemetry-dev/test_runtime_schema_live.py is an opt-in disposable PostgreSQL 16, MySQL 5.7 and MariaDB 11.4 catalog acceptance test using cached images, no published ports, isolated networks and automatic cleanup.

The November 14, 2026 Vue browser harness exercised manual loading, edit/save/restore, failed extraction/import preservation, blocked popup recovery, editor handshake outage/retry, standalone shortcuts and a 375px layout. HTTP helper/API boundaries and the separate-origin editor protocol were fixtures. Actual authenticated application-to-Draw.io UI behavior and an actual remote SSH host remain deployment acceptance checks. Editor profiling and production rollout remain open in @.archcore/diagram-editor/implement-drawio-database-schema-editing.plan.md.

## Examples

Operator setup and workflow: @web/README.md. From api/, run `RUN_RUNTIME_SCHEMA_LIVE=1 python3 -m unittest discover -s telemetry-dev -p test_runtime_schema_live.py -v` with local Docker access and the three cached images. This test creates and removes only its own disposable containers.
