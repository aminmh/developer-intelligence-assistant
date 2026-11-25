---
title: "Support several local Runtime setups at once"
status: draft
tags:
  - "api"
  - "frontend"
  - "runtime-telemetry"
---

## Goal

A developer can set up Runtime for a second project (or environment) without cleaning up the first, and can clean up each setup on its own. Today the helper keeps one recovery record, `local-setup.json`, and refuses a new setup until it is cleaned up (@api/telemetry-dev/runtime_setup.py `setup_blocker`). The record holds the Compose originals that cleanup restores, so it must never be overwritten; that is the reason for the limit. Decision context: @.archcore/runtime-telemetry/runtime-setup-in-browser-with-desktop-approval.adr.md.

## Design

[assumption] Decisions the user has not confirmed yet are marked so.

1. One recovery record per setup, keyed by project ID and environment, in a `setups/` folder beside the old file. A record keeps its Compose originals, services, app `.env` revision, scope entry and phase.
2. A setup is refused when it overlaps an existing record: the same project and environment, or the same Compose file or service already labeled by another record. Their originals would conflict.
3. [assumption] Host-wide parts are shared and recorded once in a `host.json` file: certificates, host agent, ingress and the host identity fingerprint. Each record adds only its own `TELEMETRY_HOST_SCOPES` entry; `update_env_scope` already supports several projects and environments per host.
4. Cleanup of one setup restores its Compose files, reverts its `.env` revision, recreates its services and removes only its scope entry. The agent, ingress and certificates are stopped and removed only when the last record is cleaned up (a record count in `host.json`).
5. `ensure_host_services`, `stop_host_services` and `managed_agent_environment` start the agent when any record is active.
6. [assumption] An old single `local-setup.json` is read as one record and moved to `setups/` on first use, with no change to its contents.
7. The helper's `/setup-state` returns a list; `/cleanup` takes a project ID and environment; the helper API version moves to 30 so `just up` restarts an older helper.
8. The Runtime page lists every setup with its own Clean up button, and "Set up Runtime" stays enabled. Cleanup waits for the API to restart before reloading the inventory (already fixed).

## Tasks

### Phase 1: Per-setup records in the helper

1. [ ] T1 Store one recovery record per project and environment, and migrate the old single file

   - files: @api/telemetry-dev/runtime_setup.py
   - covers: none
   - done when: a new helper test sets up two records and an old `local-setup.json`; it lists both and keeps the old one unchanged
   - proof: —

2. [ ] T2 Replace the single blocker with an overlap check per project, environment, Compose file and service

   - files: @api/telemetry-dev/runtime_setup.py
   - covers: none
   - done when: a helper test refuses an overlapping setup and accepts a different project
   - proof: —

3. [ ] T3 Record shared host state once, and tear it down only with the last record

   - files: @api/telemetry-dev/runtime_setup.py
   - covers: none
   - done when: a helper test cleans two records in turn; the agent, ingress and certificates stay until the second
   - proof: —

4. [ ] T4 Make cleanup, status and host-service start and stop work per record

   - files: @api/telemetry-dev/runtime_setup.py, @justfile
   - covers: none
   - done when: a helper test cleans one record and leaves the other's Compose files and scope entry intact
   - proof: —

### Phase 2: Helper endpoints and Runtime page

Covers: helper `/setup-state` list, `/cleanup` with project ID and environment, API version 30, one Clean up button per setup, "Set up Runtime" always enabled; expand when Phase 1 is proven.

### Phase 3: Verification on real projects

Covers: set up two Compose projects, see both containers lists, clean up one and keep the other reporting; expand when Phase 2 is proven.

## Acceptance Criteria

- A second setup starts while the first is active, and both projects' containers appear on the Runtime page.
- Cleaning up one setup leaves the other reporting spans.
- Cleaning up the last setup removes the agent, ingress and generated certificates, as it does today.
- A failed or cancelled setup or cleanup never overwrites another setup's record.

## Declared Delta

- Changes the helper's recovery storage and its HTTP endpoints: `local-setup.json` becomes `setups/<project>@<environment>.json` plus `host.json`.
- Changes the single-setup assumption in @web/src/components/RuntimeView.vue (`savedSetup`).
- Amends the browser-setup decision: its "refuse while a setup record exists" clause becomes "refuse an overlapping setup".

## Dependencies

- Real Compose projects to test two setups on, and a person at the desktop for each approval dialog.
- Tests for the helper live in @api/telemetry-dev/test_runtime_setup_flows.py; Phase 1 added six to it. Run them with `python3 -m unittest test_runtime_setup_flows` in `api/telemetry-dev`.

## Waiting on

- A decision on the two [assumption] items above (shared host state; migration of the old file) (owner: user).
