---
title: "Runtime Setup Runs in the Browser with Desktop Approval"
status: draft
tags:
  - "api"
  - "frontend"
  - "runtime-telemetry"
---

## Context

Local Runtime setup labels a developer's Compose services, updates the API host scope, starts the ingress and host agent, and recreates the chosen services. The browser cannot reach Docker or local files, so a desktop helper (`api/telemetry-dev/runtime_setup.py helper`, started by `just up` on `127.0.0.1:47831`) does that work. Until now every choice and the final approval were Gum prompts in the terminal where `just up` ran, even when setup was started from the Runtime page. The browser only started the flow and showed progress, and the helper's dispatch function had been lost in an earlier refactor, so browser-started setup, cleanup and local container removal always failed.

Approval in the terminal had one real security property: a web page alone, whether through a script injected into it or a stolen session, could not approve edits to Compose files or container restarts. A GUI has to keep that.

## Decision

Runtime setup runs as a browser wizard, with the final approval in a native dialog on the developer's computer.

1. `setup()` is split into `compose_project_details`, `build_setup_plan` (computes every change without making any) and `apply_setup_plan` (approval, recovery record, file edits, agent start, recreate). The terminal flow and the browser use the same functions.
2. The helper adds read-only wizard endpoints: `POST /compose-projects`, `POST /compose-project` and `POST /setup/plan`. They require the existing short-lived, origin-bound token (`/api/runtime/setup-authorization`, operators and administrators only), refuse while another operation runs or a setup record exists, and change nothing. The plan response shows only the label lines that will be added, never other lines of the Compose files.
3. `POST /setup/apply {plan_id}` consumes the token and the single-use plan (kept in memory for ten minutes, only the latest). The helper refuses if any Compose file changed since the preview, both before asking and again just before editing.
4. Every browser-started change (setup, cleanup, local container removal) is approved through `helper_approval`: a native dialog (`osascript` on macOS; `zenity` or `kdialog` on Linux) whose default button is Cancel and which gives up after five minutes. The dialog text is passed as an argument, never interpolated into the script. Without a desktop session the helper falls back to Gum in its own terminal, and otherwise reports that the terminal command must be used.
5. The helper's API version moves to 28 so `just up` restarts an older helper, and its `/status` reports when it is waiting for approval.

## Alternatives Considered

- **Approve in the browser only.** Simplest, but a compromised page or session could then edit Compose files and recreate containers.
- **Have the helper open a terminal window.** Still a terminal flow, and platform-specific.
- **A separate desktop app (Electron or Tauri).** Strong isolation, but a new application to build, sign and update for one flow.
- **A one-time code shown in the terminal and typed into the browser.** Keeps approval out of the browser, but still requires the terminal.

## Consequences

- Developers can set up, clean up and remove local Runtime containers without touching the terminal after `just up`.
- An approval needs a person at the desktop; a remote browser session on the same account can prepare a plan but cannot apply it unattended.
- Compose project names, directories and file paths reach the browser from the local helper. Other content of the Compose files does not.
- The terminal commands remain for machines without a desktop session and for scripts.
- The helper still has to be started by `just up`; running it as a login service (launchd or a systemd user unit) would remove the last terminal step.