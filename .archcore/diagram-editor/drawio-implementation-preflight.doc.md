---
title: "Draw.io implementation preflight"
status: draft
tags:
  - "api"
  - "diagram-editor"
  - "frontend"
---

## Overview

Preparation evidence for @.archcore/diagram-editor/implement-drawio-database-schema-editing.plan.md. This is a draft; it does not approve deployment or establish a performance budget.

## Content

- On 2026-11-01, the local X6 Database Schema view loaded a saved Laravel analysis with 45 tables and 154 relationships. This is a candidate representative dataset; its analysis identity and data access need to remain available for comparison.
- A single Chrome DevTools 5-second observation of that view reported TaskDuration increasing by 0.908 seconds and 600 layouts; this is exploratory, not a reproducible CPU benchmark or release budget. It includes ambient application activity and has not been compared with Draw.io.
- Candidate local test image: `jgraph/drawio:32.6.9@sha256:8a25d78e91275a7ebc0cfcca782a5eb357292752c9c886286448d6951db684e2`, from Docker Hub's jgraph/drawio tag listing on 2026-11-01. This digest is not an approved production image.
- The official embed-mode reference documents opener messaging but says embed mode is supported on embed.diagrams.net. In a local browser test on 2026-11-01, the pinned self-hosted image at localhost:18081 sent `init`, accepted a `load` action, emitted `load`, displayed the supplied diagram, and emitted `save` with XML after Ctrl+S. The host accepted messages only from that origin and popup in the test harness. This is observed compatibility, not a production support commitment; exit, disconnection, rejection cases, and production headers remain untested.
- The upstream drawio source README states Apache 2.0 for source code and additional restrictions on included icon and stencil assets for Atlassian products. Review the exact release and all bundled components before production distribution.
- Local development web response headers did not include Cross-Origin-Opener-Policy. Production proxy and editor response headers have not been examined; preserving an opener locally does not prove production behavior.
- The existing schema normalization uses name-derived IDs and represents keys and indexes through column properties. Migration-safe stable identity and composite keys/indexes need an explicit contract before generating diffs.

## Examples

Upstream references checked on 2026-11-01: https://www.drawio.com/docs/reference/embed-mode/ ; https://github.com/jgraph/docker-drawio ; https://github.com/jgraph/drawio/blob/dev/README.md ; https://hub.docker.com/r/jgraph/drawio/tags .

Continue verification with exit, invalid messages, document-size limits, blocked popups, and opener loss. Capture current X6 and candidate editor metrics with the same dataset and browser. Set an active-editor budget from repeatable measurements; test production headers before rollout.