---
title: "Implement Runtime ClickHouse Remote Provisioning"
status: draft
tags:
  - "api"
  - "frontend"
  - "runtime-telemetry"
---

## Goal

Implement Runtime ClickHouse storage modes and automated remote provisioning with SSH-only transport and OS-managed SSH credentials on macOS and Linux desktop hosts.

## Architecture

Keep Local ClickHouse as the default. For Remote mode, install ClickHouse on the SSH host with persistent storage, authenticated access, and HTTP/native listeners bound only to remote loopback. Expose only the SSH service to the network.

Add a narrowly scoped host-side credential and tunnel helper. It stores and retrieves SSH private keys and optional passphrases through macOS Keychain or the Linux Secret Service API, then owns the authenticated SSH tunnel. It does not expose raw credential retrieval to API or Collector containers. Provide ClickHouse HTTP and native forwards only across a restricted host-to-container bridge that is inaccessible from the LAN. Validate that boundary on supported macOS and Linux desktop runtimes.

Persist the selected storage target and a non-secret credential reference; never persist key material in Runtime settings or containers. When the OS credential store is locked, keep Remote selected, report it disconnected, and tell the user to unlock Keychain or the Linux keyring and retry. When the credential is absent, allow secure re-entry and saving. Never silently fall back between Remote and Local.

## Tech Stack

FastAPI, SQLite Runtime settings, AsyncSSH or host OpenSSH tunnel helper, macOS Keychain, Linux Secret Service API, OpenTelemetry Collector Contrib 0.136.0, Docker Compose, Vue RuntimeTelemetryCard, Node/Express authenticated proxy.

## Baseline/Authority Refs


- @api/Python-Service/app/telemetry/http.py
- @api/Python-Service/app/telemetry/runtime.py
- @api/Python-Service/app/telemetry/registry.py
- @api/docker-compose.yml
- @api/telemetry-dev/collector-runtime.yaml
- @api/telemetry-dev/clickhouse-router/main.go
- @justfile
- @web/src/components/config/RuntimeTelemetryCard.vue
- @web/server.ts
- @web/src/server/oidcAuth.ts
- @README.md
- @api/telemetry-dev/README.md

## Compatibility Boundary

Local ClickHouse remains the default and existing local data is preserved. Full-stack startup reads the persisted Runtime storage mode before bringing up Runtime data services. Local starts and verifies local ClickHouse before the Collector; Remote verifies ClickHouse over the SSH tunnel and does not start local ClickHouse; Disabled stops local ClickHouse and the Collector without deleting data. Router and SSH tunnel services remain available to support Settings provisioning and mode changes. `just runtime-storage <mode>` verifies the requested backend as part of mode selection; failed verification restores the prior selection and keeps the Collector stopped. Switching targets never copies or deletes old telemetry. PHP instrumentation, capture rate, sampling policy, and table schemas remain unchanged. Existing ClickHouse credentials are used for both Local and Remote modes. A locked credential store reports Remote unavailable and never redirects traffic to Local.

## Change Necessity

- User-visible need: choose Disabled, Local, or Remote; provision a supported remote ClickHouse host; and reconnect securely across application restarts.
- No-change / non-code option: manual environment editing cannot satisfy the approved UI, sudo-check, automated installation, OS credential storage, and synchronized API/Collector target behavior.
- Why code change is necessary: the API currently reads environment variables and Collector exporters target a fixed local hostname.
- Minimum change boundary: Runtime target persistence, host credential/tunnel helper, restricted host-to-container forwarding, authenticated UI flow, and setup/readme guidance.
- Decision: code-change

## TDD Route

Mode: off. Decision: skipped. Authority: project default. Test posture: no test-first work. Reason: the user did not request test-first work. Verification: targeted static checks, configuration validation, lint/build where available, and non-mutating SSH/sudo preflight only when a remote host is supplied.

## Tasks

1. Persist validated Runtime storage modes and non-secret credential references.
2. Add the mode-aware startup orchestrator used by `just up` and `just up-dev`.
3. Add `just runtime-storage <mode>` for starting, verifying, and selecting a backend.
4. Keep the API, router, and tunnel available for provisioning and mode changes.
5. Start only the selected ClickHouse backend and Collector after verification.
6. Stop startup on backend failure without falling back to another target.
7. Implement macOS Keychain and Linux Secret Service credential providers.
8. Add a host-side helper for credentials and SSH tunnel lifecycle.
9. Validate SSH fingerprints and sudo access before remote changes.
10. Install ClickHouse with persistent storage, authentication, and loopback-only listeners.
11. Route API reads and Collector writes through the same restricted tunnel.
12. Add authenticated endpoints for settings, provisioning, tunnel status, and mode changes.
13. Add admin UI for storage modes, remote setup, credential management, and recovery.
14. Document SSH requirements, credential stores, startup modes, and data preservation.
15. Validate startup selection, syntax, configuration, authorization, and routing.

## Acceptance Criteria

- Local and Remote route Collector writes and API reads to the same selected ClickHouse target.
- Only the configured SSH service needs to be network reachable on the remote host; ClickHouse ports remain loopback-only.
- The host credential helper uses macOS Keychain on macOS and Secret Service API on Linux desktop.
- SSH keys and optional key passphrases never appear in SQLite, environment files, container images, API responses, or logs.
- A locked keychain/keyring produces an actionable unlock-and-retry message; absent credentials produce a secure re-entry path.
- The host tunnel helper exposes no raw credential retrieval endpoint to containers.
- API and Collector reach remote ClickHouse only through the restricted SSH tunnel bridge.
- SSH fingerprint, authentication, sudo, operating system, architecture, disk, and package network checks precede installation.
- Failed preflight or installation does not change the active target; sudo passwords are never persisted.
- Tunnel loss reports Remote unavailable and never routes data to Local implicitly.
- Full-stack startup reads the saved mode: Local starts local ClickHouse; Remote verifies SSH-tunneled ClickHouse without starting local ClickHouse; Disabled starts neither ClickHouse nor the Collector.
- `just runtime-storage <mode>` verifies the requested target before starting the Collector; failure never triggers fallback and restores the prior selection.
- Settings shows the matching `just runtime-storage <mode>` recovery command when the selected backend is stopped.
- Existing local telemetry, PHP capture settings, and sampling configuration remain unchanged.
- Documentation explains SSH reachability, host-key verification, credential storage, keyring unlock, reconnect behavior, and recovery.

## Verification

- Validate Python syntax and dependencies; validate Docker Compose and generated Collector configuration for Local and Remote modes.
- Run Web typecheck and production build.
- Inspect endpoint authorization and ensure SSH credentials never appear in API responses or logs.
- Verify macOS Keychain and Linux Secret Service locked, missing, save, retrieve, and delete behaviors.
- Verify the host-to-container bridge is reachable by the API and Collector and inaccessible from the LAN on each supported desktop runtime.
- Inspect remote listener sockets after provisioning and record evidence for the loopback-only network constraint.
- Confirm API reads and Collector writes use the same SSH-forwarded target.
- Confirm locked credential store, tunnel failure, and helper restart report Remote unavailable without changing the selected target or routing to Local.
- Confirm failed SSH/sudo preflight does not invoke installation and failed install retains the prior target.
- Check documentation describes implemented behavior and existing PHP capture/sampling settings remain unchanged.
- A live remote installation is outside verification unless an actual remote host and credentials are supplied.

## Risks

- A live SSH install cannot be verified without a specific Ubuntu/Debian target and credentials.
- macOS and Linux desktop credential-store APIs have different availability and unlock behavior; supported Linux desktop sessions need Secret Service.
- The host tunnel helper becomes a new trusted component and needs a narrowly scoped, authenticated channel to the app containers.
- A compromised logged-in user session or helper can use stored credentials; OS credential storage does not protect against compromise of the authorized process.
- Collector exporters may temporarily queue telemetry during tunnel loss; their in-memory queue is not durable across process failure.
- Remote mode avoids local ClickHouse memory use, but switching to a stopped backend requires `just runtime-storage <mode>`; Settings explains this recovery path.

## Retirement

The environment-based local ClickHouse configuration remains supported for Local mode. It is not removed because existing installs depend on it. The Runtime settings target becomes the canonical selection after explicit mode configuration; a migration/default path preserves current behavior.