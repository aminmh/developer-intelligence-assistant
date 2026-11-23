---
title: "Remote Analyzer Worker and SonarQube"
status: draft
tags:
  - "api"
  - "code-quality"
---

## Purpose & Scope

This specification defines a remote, isolated Developer Mode analyzer worker running SonarScanner and Joern beside SonarQube. Production Mode continues to run analysis locally.

## Surface

- Project settings and analyzer dispatch: @api/Python-Service/app/code_quality/external_analyzers.py
- Worker transport and validation: @api/Python-Service/app/code_quality/remote_worker.py
- Worker runtime and image: @api/analyzer-worker/
- Remote SonarQube services: @api/analyzer-worker/sonarqube-only.compose.yaml
- Setup and deployment guidance: @web/src/components/code-quality/ExternalAnalyzerSetup.vue and @README.md

## Normative Behavior

1. WHEN an administrator configures a worker, the settings API MUST scope its endpoint and certificate identity to the authorized project.
2. WHEN a Developer Mode project enables the worker, the project owner MUST consent to sending selected source to that worker.
3. WHEN an assessment starts, the API MUST send only the selected immutable revision files and effective exclusions.
4. WHEN the API connects to a remote worker, the API MUST authenticate with mutual TLS.
5. WHEN the API connects to a remote worker, the API MUST verify the saved certificate fingerprint.
6. WHEN the worker accepts a job, the worker MUST validate its schema, revision identity, bundle digest, inventory, and size limits.
7. WHEN the worker runs an analyzer, the worker MUST use fixed application-owned arguments.
8. WHEN the worker runs an analyzer, the worker MUST NOT execute project scripts.
9. WHEN the worker runs SonarScanner, the worker MUST require separate project consent for the configured SonarQube destination.
10. WHEN the worker runs a SonarQube job, the worker MUST keep its token in memory only for that job.
11. WHEN the worker runs Joern, the worker MUST use the pinned Joern installation and bounded application-owned queries.
12. WHEN a worker job executes, the worker MUST enforce CPU, memory, disk, wall-time, concurrency, and output limits.
13. WHEN a worker completes or stops a job, the worker MUST delete source bundles and temporary artifacts.
14. WHEN the worker accepts a SonarQube endpoint, the worker MUST restrict it to the colocated approved SonarQube service.
15. WHEN the worker returns findings, the worker MUST include job identity, analyzer versions, coverage, and normalized results.
16. WHEN the API receives results, the API MUST validate schema, size, revision identity, paths, and line ranges.
17. WHEN the application runs in Production Mode, deployment MUST use its deployment-local worker.
18. WHEN the application runs in Production Mode, the settings API MUST reject remote worker targets.
19. WHEN a worker image is built, the build MUST pin analyzer versions and verify downloaded artifacts.
20. WHILE a worker job executes, the worker MUST NOT download analyzer binaries or rules.
21. WHEN Developer Mode has no saved global preference, the application MUST enable Developer Mode remote-analyzer features by default.
22. WHEN a global administrator disables Developer Mode in a Developer deployment, the API MUST prevent remote-worker access for every project.
23. WHEN a non-administrator changes the global Developer Mode setting, the API MUST reject the request.
24. WHEN the application runs in Production Mode, the Developer Mode switch MUST remain disabled.
25. WHEN the application runs in Production Mode, the API MUST reject remote-worker targets.

## Constraints & Invariants

- The remote host runs SonarQube, PostgreSQL, and the isolated analyzer-worker container, not the Developer Assistant API or repository checkout.
- Fingerprint discovery verifies the worker certificate chain and endpoint hostname before returning only its SHA-256 fingerprint; it sends no source.
- Only an explicitly consented selected source bundle crosses from the application host to the remote worker.
- SonarQube source upload requires separate per-project consent.
- The remote worker API is reachable only over a private approved network and mutual TLS.
- The worker has no unnecessary outbound network access and can reach only the colocated SonarQube service.
- Worker execution remains non-root, read-only where applicable, and resource-limited.
- SonarQube credentials remain in the application host OS credential store and are omitted from reports and logs.
- Worker results are untrusted until the API validates and normalizes them.
- Production Mode keeps source analysis and SonarQube on the production application host.
- Developer Mode is a global administrative gate for project-configured remote workers; disabling it preserves project settings but prevents worker checks, analyzer dispatch, and source transfer.
- Production Mode deployment configuration remains authoritative and cannot be changed through the Developer Mode control.

## Failure Behavior

1. IF worker consent is absent, THEN the API MUST NOT transfer source to the worker.
2. IF worker TLS identity or fingerprint validation fails, THEN the API MUST stop before source transfer.
3. IF SonarQube consent is absent, THEN the worker MUST NOT submit source to SonarQube.
4. IF a worker job exceeds a configured limit, THEN the worker MUST stop it, remove temporary data, and return a bounded failure.
5. IF a worker response is malformed, oversized, or mismatched to the revision, THEN the API MUST reject it.
6. IF an analyzer fails, THEN the report MUST mark its coverage unavailable or partial without treating missing findings as zero findings.
7. IF Developer Mode is globally disabled, THEN worker connection checks and SonarQube access checks MUST fail before contacting a project-configured worker.
8. IF a non-administrator attempts to change the global setting, THEN the gateway MUST return an authorization error without changing persisted state.
9. IF worker transport fails, THEN the API MUST distinguish DNS, timeout, connection refusal, certificate verification, and other TLS failures without exposing raw exceptions.

## Conformance

A conforming Developer Mode setup runs the isolated worker and SonarQube services remotely, uses mutual TLS and explicit project consent for source transfer, and keeps Production Mode local.