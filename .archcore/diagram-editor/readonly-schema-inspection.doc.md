---
title: "Read-only schema inspection"
status: draft
tags:
  - "api"
  - "diagram-editor"
---

## Overview

The Draw.io inspection action reads configured database column metadata without changing schema drafts or baselines.

## Content

@api/Python-Service/app/analysis/schema_inspection.py reads only administrator-configured PostgreSQL or MySQL targets. The service uses certificate-verified TLS, a read-only transaction, fixed metadata queries, bounded results, and generic errors. @api/Python-Service/app/analysis/schema_draft_routes.py requires explicit consent and draft authorization. @web/src/server/schemaDraftRoutes.ts requires an administrator and the application origin. @web/drawio/props.js offers the inspection action through the validated editor bridge.

The report compares column names against the original baseline. It does not verify keys, indexes, relationships, complete column definitions, or migration safety. When results are truncated, the report does not claim columns are missing.

## Examples

The deployment configuration and user workflow are described in @web/README.md. Live database and browser integration still require verification in an environment with a configured target.
