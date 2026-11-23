---
title: "The Developer Runs Both Versions; the App Only Sends HTTP to Them"
status: draft
tags:
  - "api"
  - "code-quality"
---

## Context

A regression check needs dynamic evidence: the same requests answered by the old and the new version. Several specs forbid the app from executing project code, scripts or tests (@.archcore/code-quality/codebase-quality-assessment.spec.md clause 26, @.archcore/code-quality/remote-analyzer-worker.spec.md clause 8, @.archcore/agent-tools/change-impact-analysis.spec.md). The app already sends HTTP to a developer's running app for benchmarks, with egress rules in @api/Python-Service/app/code_quality/performance.py `validate_origin`. The roadmap's open Phase 4 (@docs/plans/2026-10-01-ai-tool-use-and-refactoring-roadmap.md) planned running tests "in a constrained environment", which conflicts with those rules.

## Decision

1. The developer starts the base and candidate versions and gives their URLs as targets; the app never builds, starts or runs project code or tests.
2. The app's only action against a target is HTTP replay, through the same origin validation, DNS pinning, auth modes and run limits as benchmarks.
3. A baseline may be recorded once before refactoring, so the old version need not run when the candidate is checked.
4. When the developer cannot run a version, the check falls back to static evidence and generated characterization tests the developer runs.

## Alternatives

- **Run the project in a sandbox container.** Rejected: needs per-language build knowledge, heavy resources on an 8 GB machine (@.archcore/installation/local-mode-fits-an-8-gb-machine-with-hosted-models.adr.md), and breaks the no-execution rules.
- **Run the project's tests in the analyzer worker.** Rejected for the same reason; most target projects have no tests.

## Consequences

- The existing no-execution rules stay intact; replay reuses benchmark infrastructure.
- Getting the app running is the developer's job; the check offers a baseline checklist.
- Data-changing requests need a separate rule (@.archcore/regression/replay-writes-only-on-disposable-targets.adr.md).
- Chosen by the user on 2026-11-22.
