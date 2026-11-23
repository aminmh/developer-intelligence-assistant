---
title: "Runtime Remote Host Monitor"
status: draft
tags:
  - "api"
  - "frontend"
  - "runtime-telemetry"
---

## Purpose & Scope

Defines temporary host-resource monitoring for the configured Remote ClickHouse target while the Runtime view is open.

## Surface

- Internal API: GET /telemetry/runtime/host-metrics
- Authenticated Web proxy: GET /api/runtime/host-metrics
- Runtime view remote-host monitor and its polling lifecycle

## Normative Behavior

1. WHEN an internal caller requests host metrics, the API MUST require the internal service credential.
2. WHEN storage mode is Local or Disabled, the API MUST return the selected mode without opening SSH.
3. WHEN storage mode is Remote, the API MUST connect using the saved SSH target and verified host fingerprint.
4. WHEN SSH connects, the API MUST run only a fixed, read-only Linux metrics query without sudo.
5. WHEN SSH metrics are available, the API MUST report CPU, RAM, load average, and free and total ClickHouse-path disk.
6. WHEN the API returns a sample, it MUST report sample time and SSH and ClickHouse health separately.
7. WHEN no CPU counter delta exists, the Runtime view MUST show CPU warming instead of a numeric value.
8. WHEN Remote mode is active, the Runtime view MUST poll every five seconds while open and the browser tab is visible.
9. WHEN the browser tab becomes hidden or the Runtime view unmounts, the Runtime view MUST stop polling and abort its request.
10. WHEN a poll is active, the Runtime view MUST NOT start another poll.
11. WHEN Remote mode is inactive, the Runtime view MUST omit the remote-host monitor.
12. WHEN the Runtime view updates metric values, it MUST NOT animate the changing numbers.
13. WHEN the user prefers reduced motion, the application MUST suppress nonessential page, panel, dialog, toast, and control transitions.

## Constraints & Invariants

- The API caches only the latest sample and CPU counters in process memory.
- The API does not persist host metrics or return credentials, arbitrary command output, or raw command output.
- The metrics query does not use sudo or accept a caller-supplied command.
- Disk metrics describe /var/lib/clickhouse on the configured SSH host.
- The implementation does not add an animation dependency or continuous motion effects.

## Failure Behavior

1. IF the keyring or SSH identity is unavailable, THEN the API MUST return an actionable unavailable state without exposing credential material.
2. IF SSH fails while a sample exists, THEN the API MUST return the latest sample as stale with a sanitized recovery message.
3. IF SSH fails before the first sample, THEN the API MUST return unavailable without inventing metric values.
4. IF ClickHouse is unavailable while SSH works, THEN the API MUST retain host metrics and report ClickHouse as unavailable.
5. IF remote output is malformed or times out, THEN the API MUST reject that sample and return unavailable or the latest sample as stale.

## Conformance

- API checks cover authorization, Local and Disabled no-SSH behavior, pinned SSH identity, malformed and timed-out output, ClickHouse failure, and concurrent requests.
- Runtime checks cover Remote-only display, warming and unavailable states, five-second polling, hidden-tab cancellation, and unmount cleanup.
- Web checks cover normal and reduced-motion transitions without animating live metric values.