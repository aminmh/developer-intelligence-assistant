---
title: "Replay Sends Writes Only to Targets Marked Disposable"
status: draft
tags:
  - "api"
  - "code-quality"
---

## Context

Replaying POST, PUT, PATCH or DELETE changes data in the developer's app; replaying them twice for noise detection changes it again, and base and candidate may share a database. Benchmarks send GET only (@api/Python-Service/app/code_quality/performance.py `build_script`).

## Decision

1. Replay sends GET and HEAD to every target.
2. Other methods are sent only to a target the developer marks `disposable`, stating its data may be changed or reset.
3. On a disposable target, write probes replay in recorded order, after an optional developer-given reset URL is called.
4. Write probes on a non-disposable target are skipped and listed as skipped in the report.

## Alternatives

- **GET/HEAD only.** Rejected by the user: most regressions in business code sit behind writes.
- **All methods always.** Rejected: one replay can corrupt a developer's shared or production-like database.

## Consequences

- Read-only checks are safe by default; write coverage needs a resettable environment.
- Noise detection on writes depends on the reset URL; without it, differences after a write are reported with lower confidence. [assumption]
- Chosen by the user on 2026-11-22.
