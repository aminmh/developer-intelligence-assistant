---
title: "Deploy Remote Analyzer Worker and SonarQube"
status: draft
tags:
  - "api"
  - "code-quality"
---

## Declared Delta

- Creates: a remote Developer Mode analyzer worker alongside remote SonarQube and PostgreSQL.
- Modifies: deployment guidance and remote analyzer setup instructions.
- Retires: the constraint that the analyzer worker always runs on the application host in Developer Mode.
- Decision: the remote host runs SonarQube, PostgreSQL, and an isolated analyzer-worker container; it does not run the Developer Assistant application or require its repository checkout.

## Goal

Run SonarQube and Joern on `amin-System-Product-Name.local` while keeping the application API and repository on the Developer Assistant host.

## Architecture

Build the pinned analyzer-worker image from the local repository, transfer the image and only its runtime Compose/certificate configuration, and run it remotely beside SonarQube. The worker accepts selected immutable source bundles from the Developer Assistant API only over mutual TLS and only after project-level source-transfer consent. The worker runs fixed SonarScanner or Joern jobs, restricts SonarQube egress to the colocated service, removes temporary source, and returns validated bounded results. Production Mode continues to use its deployment-local worker. Developer Mode has a global persisted administrator switch that gates all project-configured remote-worker checks and analyzer jobs while retaining project settings; Production Mode locks this switch off.

## Tasks

1. Keep the remote SonarQube and PostgreSQL services in `api/analyzer-worker/sonarqube-only.compose.yaml`.
2. Add a remote worker Compose configuration without copying the application repository to the remote host.
3. Build and transfer the pinned worker image for the remote host architecture.
4. Provision separate API-client and worker-server mutual-TLS identities for the remote connection.
5. Bind the worker port to the approved network interface and restrict ingress to the application host.
6. Attach the worker to a private network with access to SonarQube and no unnecessary egress.
7. Configure Developer Mode to use the remote worker endpoint and pinned certificate identity.
8. Require per-project worker source-transfer consent and separate SonarQube upload consent.
9. Preserve SonarQube credential storage in the application host OS credential store.
10. Keep Production Mode analysis local and reject remote worker targets.
11. Document remote setup, network controls, certificate renewal, backup, update, rollback, and source-transfer behavior.
12. Add a global persisted Developer Mode setting with authenticated status and administrator-only mutation.
13. Enforce the Developer Mode gate before worker checks, SonarQube access checks, capability claims, analyzer dispatch, and source transfer.
14. Keep Production Mode locked and preserve its deployment-owned local worker behavior.
15. Add an accessible shared-toggle control that displays enabled, disabled, locked, loading, and failure states.
16. Verify worker capabilities, mutual-TLS authorization, both analyzers, temporary-file cleanup, SonarQube readiness, and authorization boundaries.

## Acceptance Criteria

- SonarQube, PostgreSQL, and the isolated analyzer-worker container run on the remote host.
- The remote host does not require a Developer Assistant checkout, API service, or application database.
- Only the selected immutable source bundle reaches the remote worker after project-level consent.
- Only a separately consented SonarQube job sends source to the configured SonarQube service.
- The application API authenticates to the worker with mutual TLS and validates its pinned certificate fingerprint.
- Worker ingress is limited to the Developer Assistant host or an equivalent private network boundary.
- Worker network egress reaches only the colocated SonarQube service and required internal dependencies.
- Joern and SonarScanner versions remain pinned; jobs retain existing resource, time, concurrency, and output limits.
- The worker deletes source bundles and temporary artifacts after completion or failure.
- Production Mode remains local and cannot select the Developer Mode remote worker.
- A global administrator can disable and re-enable Developer Mode remote-worker features for all projects; saved project settings remain intact while disabled.
- Non-administrators can read the effective status but cannot change the global setting.
- Production Mode exposes the switch as locked and cannot enable remote workers through it.
- Existing assessment reports remain readable when the remote worker is unavailable.

## Dependencies

- The application host can reach the remote worker over an approved private network.
- The remote host has sufficient disk, memory, and CPU for both analysis and SonarQube.
- The application host OS credential store can supply the SonarQube token for a single job.
- Project owners explicitly consent to source transfer to the remote worker and, separately, to SonarQube.

## Risks

- Remote Joern requires transferring selected source files to the remote worker.
- A reachable worker endpoint must remain protected by mutual TLS and network ingress controls.
- Worker and SonarQube updates can affect result comparability.
- Remote SonarQube state needs independent backups and retention management.
- Disabling Developer Mode must stop new remote jobs immediately without corrupting retained per-project configuration or historical reports.