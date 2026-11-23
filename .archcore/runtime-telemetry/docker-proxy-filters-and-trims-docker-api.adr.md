---
title: "Docker Read Proxy Filters and Trims the Docker API"
status: draft
tags:
  - "api"
  - "runtime-telemetry"
---

## Context

The per-host telemetry agent (`api/telemetry-agent/`) discovers which opted-in project commit runs on a Docker host. The Docker socket is root-equivalent, so the agent never mounts it. A separate proxy container mounts the socket, sits on an internal-only network and is the only thing the agent can reach Docker through. The agent is the only container with network egress (mutual TLS to the API's Caddy ingress).

The first proxy, `docker_api_proxy.py` on a Python slim image, allowlisted four GET paths but forwarded the client's query string and Docker's full responses:

- `containers/json` returned every container's command line, all its labels, mounts and addresses, and accepted `size=1` (an expensive disk scan).
- `/events` streamed every container event. Exec events carry the command that was run, and health checks emit exec events on every probe; in a live check one container with a 1 s health check produced 27 exec events in about 8 s.
- The opt-in filter lived only in the agent, so a compromised agent could read the whole host's container metadata. The agent ran a full reconcile and a backend POST for every opted-in event, including health and exec events, and hard-coded Docker API v1.41.

The proxy runs in the socket's group, so any code-execution bug in it is host-root. On a Python image that bug would land next to a shell, an interpreter and a package manager. The `:ro` socket mount was described as a safeguard; it does not restrict API calls over the socket.

## Decision

The proxy is a static Go binary (`api/telemetry-agent/proxy/`, standard library only) on a `scratch` image, and it enforces data minimisation itself, so the agent never receives anything it would discard:

1. It accepts only GET `/_ping`, `/version`, `/v1.N/containers/json` and `/v1.N/events`, with an exact `v1.N` version segment, and refuses absolute-form targets, encoded path characters and malformed queries.
2. It never forwards the client's query string. It rebuilds each Docker request, always adds the `com.devassistant.telemetry.enabled=true` label filter, keeps only numeric `since` and `until` on events, and requests only container `start`, `die`, `destroy`, `rename`, `pause` and `unpause` events.
3. It returns only the container ID, image ID, state and `com.devassistant.telemetry.*` labels (and only the version fields from `/version`), checking the opt-in label again on every item and event.
4. It caps concurrency at 8, bounds request headers (8 KiB, 10 s), times out Docker calls other than the event stream, limits response and event sizes, and logs refusals and failures without paths or queries.
5. The image contains only the binary: no shell, interpreter or package manager. The build runs `go vet` and the tests before producing it. The container runs as UID 10001 with a read-only filesystem, no capabilities, `no-new-privileges`, a 64-process limit and 64 MiB of memory. The binary's `-healthcheck` flag serves the Compose health check without a shell.

The agent negotiates the API version from `/version` (preferring 1.44, clamped to the engine's range), coalesces event bursts into one reconcile after 1.5 s, retries failed reports with backoff, reconnects the event stream after errors or five idle minutes, and logs failures without request data. Compose starts the agent only after the proxy's health check passes.

## Alternatives Considered

- **`tecnativa/docker-socket-proxy` (HAProxy).** Grants access by API section; `CONTAINERS=1` also opens inspect, logs and archive, and it cannot rewrite queries or trim responses. Rejected as less strict.
- **Keep filtering in the agent.** Simpler, but leaves other containers' commands and exec activity one compromise away from the only container with egress.
- **Keep the proxy in Python.** Same rules, no new toolchain, but an image of about 200 MB with a shell and interpreter beside the socket. Replaced by the Go binary, whose image is about 9 MB and contains nothing else.
- **Distroless instead of scratch.** Adds CA certificates and timezone data the proxy does not use.

## Consequences

- Health checks and exec activity no longer cause reconciles or backend reports; a `docker compose up` causes one report instead of one per event.
- The agent can no longer list unlabelled containers or arbitrary events through the proxy. Any new Docker data the agent needs must be added to the proxy's trimmed fields deliberately, in Go.
- The agent works on engines whose minimum API version is above 1.41.
- An exploit in the proxy would still have Docker socket access, but would find no shell or tools in the container. Go code without `unsafe` or cgo (the build sets `CGO_ENABLED=0`) is not exposed to the buffer-overflow class of bugs that usually leads to code execution.
- Changing the proxy needs Go only to run tests outside Docker; the image build brings its own toolchain (`golang:1.25.4-bookworm`).