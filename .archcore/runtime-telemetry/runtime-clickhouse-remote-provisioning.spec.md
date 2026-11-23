---
title: "Runtime ClickHouse Remote Provisioning"
status: draft
tags:
  - "api"
  - "frontend"
  - "runtime-telemetry"
---

## Purpose & Scope

Owns the behavioral contract for optional local or remote ClickHouse storage using SSH-only provisioning and transport on macOS and Linux desktop hosts.

## Surface

- Runtime remote-storage setup and reconnect behavior
- SSH credential persistence through macOS Keychain or Linux Secret Service API
- SSH tunnel transport for ClickHouse HTTP and native protocols
- Provisioned remote ClickHouse network exposure

## Normative Behavior

1. WHEN a user opens Runtime storage settings, the UI MUST show Disabled, Local, and Remote choices separately from other settings.
2. WHEN a user selects Local storage, the application MUST route Collector writes and API reads to configured local ClickHouse.
3. WHEN a user selects Remote storage, the UI MUST request SSH host, port, user, and private key.
4. WHEN a user saves SSH credentials, the host credential helper MUST store them in the platform credential store.
5. WHEN the host credential helper stores SSH credentials, it MUST keep them out of application databases, environment files, container images, and logs.
6. WHEN a user submits Test connection, the provisioner MUST verify SSH host identity before authentication.
7. WHEN SSH authentication succeeds, the provisioner MUST run a non-mutating sudo access check.
8. WHEN the sudo check requires authentication, the setup UI MUST request the sudo password.
9. WHEN a user submits a sudo password, the provisioner MUST verify sudo access without storing the password.
10. WHEN SSH or sudo validation fails, the setup UI MUST identify the failed check and show host-side repair instructions.
11. WHEN SSH or sudo validation fails, the provisioner MUST NOT install packages or change remote configuration.
12. WHEN a user starts Install & connect, the provisioner MUST check supported operating system, architecture, disk space, and package network access.
13. WHEN preflight succeeds, the provisioner MUST install ClickHouse on supported Ubuntu or Debian Linux hosts.
14. WHEN the provisioner installs ClickHouse, it MUST configure persistent storage and authenticated access.
15. WHEN the provisioner configures ClickHouse listeners, it MUST bind them to remote loopback only.
16. WHEN Remote storage is active, the host tunnel helper MUST retrieve SSH credentials from the OS credential store and maintain the authenticated SSH tunnel.
17. WHEN the host tunnel helper opens SSH forwards, it MUST expose ClickHouse HTTP and native endpoints only to the internal application network.
18. WHEN the API or Collector accesses remote ClickHouse, it MUST use the SSH-forwarded endpoints.
19. WHEN Remote storage is active, the application MUST use the same remote ClickHouse target for Collector writes and API reads.
20. WHEN the OS credential store reports a locked keyring, the Runtime UI MUST tell the user to unlock Keychain or the Linux keyring and retry.
21. WHEN the OS credential store cannot find saved credentials, the Runtime UI MUST let the user re-enter and save the SSH credentials.
22. WHEN the SSH tunnel disconnects, the application MUST report telemetry unavailable.
23. WHEN the SSH tunnel disconnects, the application MUST NOT silently route to Local storage.
24. WHEN provisioning or connectivity succeeds, the settings UI MUST show the target, health status, and last verification result.
25. WHEN a user removes saved SSH credentials, the host credential helper MUST delete the application credential-store item.
26. WHEN a user selects Disabled storage, the application MUST stop Runtime telemetry ingestion and reads without deleting stored data.
27. WHEN remote provisioning is unavailable or unsupported, the settings UI MUST provide a remote setup guide and recovery steps.
28. WHEN full-stack startup begins, the Runtime orchestrator MUST read the persisted storage mode before starting ClickHouse services.
29. WHEN the saved mode is Local, the Runtime orchestrator MUST start and verify local ClickHouse before starting the Collector.
30. WHEN the saved mode is Remote, the Runtime orchestrator MUST verify SSH-tunneled ClickHouse before starting the Collector.
31. WHEN the saved mode is Remote, the Runtime orchestrator MUST NOT start local ClickHouse.
32. WHEN the saved mode is Disabled, the Runtime orchestrator MUST NOT start local ClickHouse or the Collector.
33. WHEN a developer requests another mode through `just runtime-storage <mode>`, the orchestrator MUST verify the requested backend before starting the Collector.
34. WHEN a selected backend is unavailable during startup, the Runtime orchestrator MUST stop before Collector startup.
35. WHEN a selected backend is unavailable during startup, the Runtime orchestrator MUST NOT fall back to another backend.
36. WHEN Settings cannot activate an unstarted backend, the Runtime UI MUST show the matching `just runtime-storage <mode>` command.

## Constraints & Invariants

- SSH on the configured port is the only network service that Remote ClickHouse requires to expose.
- ClickHouse HTTP and native listeners remain bound to remote loopback and are never directly reachable from the network.
- The same selected ClickHouse target owns Collector writes and Runtime API reads.
- SSH private keys and passphrases are stored only in the host OS credential store and held in process memory only while establishing or maintaining SSH.
- Sudo passwords are never persisted.
- The host tunnel helper exposes no general-purpose credential retrieval API to application containers.
- Test connection performs no installation or remote configuration changes.
- Failed provisioning leaves the previous active storage target unchanged.
- Remote setup does not reduce PHP request capture or change trace sampling.
- Switching targets does not copy or delete telemetry stored at the previous target.

## Failure Behavior

1. IF the host key is unknown or changed, THEN the setup UI MUST require explicit fingerprint verification before SSH authentication.
2. IF the SSH account lacks sudo access, THEN the setup UI MUST explain how an administrator can grant and verify it on the remote host.
3. IF the host is unsupported or preflight fails, THEN the provisioner MUST stop before changing the host.
4. IF installation or target verification fails, THEN the application MUST retain the previous active storage configuration.
5. IF the credential store is locked, THEN the Runtime UI MUST instruct the user to unlock it and retry without switching storage targets.
6. IF saved credentials are absent, THEN the Runtime UI MUST offer secure re-entry and credential-store save.
7. IF the SSH tunnel is unavailable, THEN the Runtime UI MUST report Remote disconnected without deleting stored data or switching targets.

## Conformance

- UI checks verify storage mode selection, OS credential-store locked and missing states, and recovery instructions.
- Provisioner checks verify host identity, SSH authentication, sudo access, and no changes after failed preflight.
- Credential checks verify secrets are stored and removed through the OS provider and never appear in app storage or logs.
- Configuration checks verify ClickHouse binds loopback and API plus Collector use the same forwarded target.
- Integration checks verify remote setup, SSH-tunneled telemetry write/read, failure rollback, and unchanged PHP capture settings.