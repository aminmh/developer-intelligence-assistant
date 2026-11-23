---
title: "Replay Recordings Live in App Storage, Not in Telemetry"
status: draft
tags:
  - "api"
  - "code-quality"
  - "runtime-telemetry"
  - "security"
---

## Context

Response comparison needs full bodies. The runtime collector keeps a fixed attribute list and drops bodies (@api/telemetry-dev/collector-runtime.yaml `transform/runtime_privacy`); response key names in spans are still an open decision (@.archcore/frontend-assist/response-shapes-from-runtime-telemetry.rnd.md). Data management inventories and deletes stored project data (@.archcore/data-management/data-management.spec.md).

## Decision

1. Baseline and candidate responses are stored per project and check in the app's own storage (MinIO objects indexed in SQLite), not in ClickHouse.
2. Recordings have size caps and a retention period, and appear in the data-management inventory for planned deletion.
3. Telemetry privacy rules stay unchanged; capturing bodies in telemetry needs its own decision later.

## Alternatives

- **Store bodies as span attributes.** Rejected: changes the privacy keep-list and the PHP library, and only works for languages with an adapter.
- **Keep bodies only in memory during a run.** Rejected: golden-master baselines must outlive the old version.

## Consequences

- Golden-master baselines work without telemetry and for any language.
- Recordings may hold personal data from the developer's app; they are scoped to the project's access rules and deletable.
- Agreed with the user on 2026-11-22 as part of the regression-check plan.
- 2026-11-23 implementation (c13f342): recordings are JSON files under `<snapshot root>/regression_recordings/<project hash>/<check>/<run>/`, not MinIO objects, which keeps them next to the regression SQLite store; caps are 1 MB per body and 50 MB per check, retention 30 days, deletion with the check (@api/Python-Service/app/regression/recordings.py). Listing them in the data-management inventory waits until that work is committed. [assumption: confirm with the user]
