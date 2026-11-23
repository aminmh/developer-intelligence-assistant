---
title: "Resumable Local Installer"
status: draft
---

## Purpose & Scope

Install the complete downloaded application folder on macOS Intel/ARM and Ubuntu/Debian desktop/headless hosts. Public hosting, LAN access, upgrades and uninstall are excluded.

## Surface

@install.sh and @scripts/installer/bootstrap.sh bootstrap Just. @justfile exposes install, install-check, install-resume, install-status and install-info. @scripts/installer/install.sh owns stages. @scripts/installer/config.py generates service configuration; @scripts/installer/state.py owns atomic state and redaction; @scripts/installer/system.py owns host preflight and certificates; @scripts/installer/verify.py owns live probes; @scripts/installer/cli.py connects these owners. @docs/installation.md describes installation and recovery.

## Normative Behavior

1. WHEN installation starts, the installer MUST report dependencies and review changes before installing host packages or changing system trust.
2. WHEN the user selects services, the installer MUST validate editable hostnames and loopback published ports.
3. WHEN configuration exists, the installer MUST preserve credentials, databases, volumes and private service addresses.
4. WHEN configuration is missing, the installer MUST generate strong credentials and consistent public HTTPS URLs.
5. WHEN HTTPS is configured, the installer MUST mount service identities without mounting the local certificate authority private key.
6. WHEN installation resumes, the installer MUST revalidate stage evidence and invalidate changed configuration inputs.
7. WHEN installation records progress, the installer MUST exclude credentials from ordinary logs and checkpoints.
8. WHEN installation reports service credentials, the installer MUST save the report with directory mode 0700 and file mode 0600.
9. WHEN verification runs, the installer MUST check HTTPS, application login/logout, resumed upload transfer, database authentication, model connectivity and Runtime helpers before declaring success.
10. WHEN network operations fail, the installer MUST retry three times with backoff and preserve reusable work.
11. WHEN recovery runs, the installer MUST preserve Docker images, volumes, build caches and model data.
12. WHEN headless installation completes, the installer MUST identify unavailable desktop approval and credential-store capabilities.

## Constraints & Invariants

The dedicated browser HTTPS proxy is separate from Runtime ingress and preserves its mTLS boundary. Qdrant public REST/gRPC endpoints authenticate at the proxy; container peers retain private access. Public Neo4j Bolt uses verified TLS; private API connections retain their existing transport. Host Python dependencies stay isolated. Certificate fingerprints in generated Compose labels recreate certificate consumers after renewal. Optional analyzer identities use the existing mTLS provisioner and never replace partial existing bundles. SonarQube factory-password initialization requires absence of its persisted volumes, recorded before startup; existing volumes disable that path. Installer credential reports are private operator artifacts, separate from application assessment reports.

## Failure Behavior

1. IF stored bootstrap credentials fail authentication, THEN the installer MUST report the mismatch without resetting accounts.
2. IF Docker or OS setup needs manual interaction, THEN the installer MUST pause with a resume command.
3. IF another installer holds the lock, THEN the installer MUST refuse concurrent mutation.
4. IF a generated override conflicts with an existing user file, THEN the installer MUST stop before overwriting that file.

## Conformance

Controlled tests in @scripts/installer/tests/ and @tests/installer/ cover bootstrap verification, dependency reporting, secret preservation, checkpoints, invalidation, hosts idempotency, output redaction, retry/cancellation, real OS locking and live-probe failure handling. Compose parser tests validate merged overrides without a daemon. Live probes exercise public REST/gRPC authentication, encrypted Bolt, an application-issued TUS upload in two chunks and finish-hook bytes, application login/logout, container model connectivity and Runtime helpers. Optional analyzer probes check SonarQube credentials and mTLS capabilities. Real desktop/headless acceptance still requires live Docker, trust stores, browser interaction and fresh/existing installations; controlled tests do not establish platform acceptance.