# Developer Assistant local task runner.
# Run `just` for the interactive service/action menu.

set shell := ["bash", "-cu"]

root_dir := justfile_directory()
api_dir := root_dir + "/api"
web_dir := root_dir + "/web"
runtime_script := api_dir + "/telemetry-dev/runtime_setup.py"
runtime_stack_script := api_dir + "/telemetry-dev/runtime_stack.py"
sysmon_script := root_dir + "/scripts/sysmon-agent.mjs"

default: menu

# Review choices and install the local HTTPS stack.
install:
    @bash "{{root_dir}}/scripts/installer/install.sh" install

# Report host dependencies without changing system configuration.
install-check:
    @bash "{{root_dir}}/scripts/installer/install.sh" check

# Revalidate and resume a previous installation.
install-resume:
    @bash "{{root_dir}}/scripts/installer/install.sh" resume

# Show recorded installation stages.
install-status:
    @bash "{{root_dir}}/scripts/installer/install.sh" status

# Display the private service and credential report (contains secrets).
install-info:
    @bash "{{root_dir}}/scripts/installer/install.sh" info

# Run deterministic offline API and Web regression suites.
test:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{api_dir}}"
    docker compose run --no-deps --rm \
        --volume .:/workspace \
        --workdir /workspace/Python-Service \
        python-api env \
        PYTHONPATH=/workspace/Python-Service/app \
        TELEMETRY_SNAPSHOT_ROOT=/tmp/pytest-snapshot-root \
        python -m pytest -q -o addopts= -m 'not integration' app/tests
    cd "{{web_dir}}"
    npm test

# Run the Neo4j integration checks against the local Docker service.
test-integration:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{api_dir}}"
    docker compose up -d neo4j qdrant
    docker compose run --no-deps --rm python-api python -m pytest -q -o markers="integration: requires local Docker-backed Neo4j" -m integration tests/test_neo4j_integration.py

# Open the interactive service and action menu.
menu:
    @bash "{{root_dir}}/scripts/devassistant-menu.sh"

# Start the full stack with the production Web service.
up: _ensure-host-services _ensure-runtime-helper api-up _api-runtime-up _ensure-sysmon web-up

# Start a lower-memory stack with production Web and externally configured OIDC.
up-lite: _api-up-lite web-up-lite

# Start the full stack with the hot-reload Web development service.
up-dev: _ensure-host-services _ensure-runtime-helper api-up _api-runtime-up _ensure-sysmon web-up-dev

# Stop services while keeping their containers.
stop: _stop-runtime-helper _stop-sysmon _stop-host-services web-stop api-stop

# Stop and remove the full stack's containers.
down: _stop-runtime-helper _stop-sysmon _stop-host-services web-dev-down web-down api-down

# Restart the full stack with the production Web service.
restart: down up

# Restart the full stack with the hot-reload Web development service.
restart-dev: down up-dev

# Show API and Web container status.
status:
    #!/usr/bin/env bash
    set -euo pipefail
    echo "==> API stack"
    make -C "{{api_dir}}" ps
    echo
    echo "==> Web stack (all profiles)"
    cd "{{web_dir}}"
    docker compose --profile '*' ps -a

# Follow API and Web development logs together.
logs:
    #!/usr/bin/env bash
    set -euo pipefail
    make -C "{{api_dir}}" logs &
    api_pid=$!
    make -C "{{web_dir}}" dev-logs 2>/dev/null &
    web_pid=$!
    wait "$api_pid" "$web_pid"

# Configure local Runtime telemetry interactively.
runtime-setup:
    @python3 "{{runtime_script}}" setup

# Show local Runtime telemetry setup status.
runtime-status:
    @python3 "{{runtime_script}}" status

# Clean up local Runtime telemetry setup.
runtime-cleanup:
    @python3 "{{runtime_script}}" cleanup

# Start the optional local Outline knowledge base.
outline-up:
    @make -C "{{web_dir}}" outline-up

# Stop Outline containers while preserving its database and uploaded files.
outline-down:
    @make -C "{{web_dir}}" outline-down

# Show Outline stack status.
outline-status:
    @make -C "{{web_dir}}" outline-status

# Follow Outline application logs.
outline-logs:
    @make -C "{{web_dir}}" outline-logs

# Start, verify, and select a Runtime ClickHouse storage mode.
runtime-storage mode: _ensure-host-services api-up
    @python3 "{{runtime_stack_script}}" select "{{mode}}"

# Set up Runtime telemetry for a PHP project in one step: install, commit, index, label, restart, verify.
[positional-arguments]
php-otel-setup *args:
    @bash "{{root_dir}}/scripts/setup-php-telemetry.sh" "$@"

# Install and configure Indexr OpenTelemetry in a PHP project. Pass --help for non-interactive options.
[positional-arguments]
php-otel-install *args:
    @bash "{{root_dir}}/scripts/install-indexr-otel.sh" "$@"

# Upload and index the committed PHP source revision; local changes are ignored. Pass --help for options.
[positional-arguments]
php-otel-index *args:
    @bash "{{root_dir}}/scripts/index-php-source-snapshot.sh" "$@"

# Remove indexed revisions of a Runtime project, keeping the newest ready one. Pass --help for options.
[positional-arguments]
php-otel-prune *args:
    @bash "{{root_dir}}/scripts/prune-indexed-revisions.sh" "$@"

# Start the API services.
api-up: _ensure-runtime-helper
    #!/usr/bin/env bash
    set -euo pipefail
    agent_socket="$HOME/.local/state/devassistant/runtime-host/ssh-agent.sock"
    export SSH_AUTH_SOCK="$agent_socket"
    if [[ "$(uname -s)" == "Darwin" ]]; then
      export RUNTIME_SSH_AGENT_SOURCE=/run/host-services/ssh-auth.sock
    else
      export RUNTIME_SSH_AGENT_SOURCE="$agent_socket"
    fi
    make -C "{{api_dir}}" up

[private]
_api-runtime-up:
    #!/usr/bin/env bash
    set -euo pipefail
    python3 "{{runtime_stack_script}}" start

# Stop API services while keeping their containers.
api-stop:
    @make -C "{{api_dir}}" stop

# Stop and remove API containers.
api-down:
    @make -C "{{api_dir}}" down

# Restart API services.
api-restart:
    @make -C "{{api_dir}}" restart

# Show API container status.
api-status:
    @make -C "{{api_dir}}" ps

# Follow API logs.
api-logs:
    @make -C "{{api_dir}}" logs

# Start the production Web service.
web-up:
    @make -C "{{web_dir}}" up

# Start production Web without embedded Keycloak; configure external OIDC first.
web-up-lite:
    @make -C "{{web_dir}}" up-lite

# Start the Web development service with hot reload.
web-up-dev:
    @make -C "{{web_dir}}" dev

# Stop the production Web service while keeping its container.
web-stop:
    @make -C "{{web_dir}}" stop

# Stop and remove the production Web container.
web-down:
    @make -C "{{web_dir}}" down

# Stop and remove Web development-profile containers.
web-dev-down:
    @make -C "{{web_dir}}" dev-down

# Restart the production Web service.
web-restart:
    @make -C "{{web_dir}}" restart

# Restart the Web development service.
web-restart-dev:
    @cd "{{web_dir}}" && docker compose --profile dev restart web-dev

# Show Web container status.
web-status:
    @cd "{{web_dir}}" && docker compose --profile '*' ps -a

# Follow Web development logs.
web-logs:
    @make -C "{{web_dir}}" dev-logs

[private]
_ensure-host-services:
    #!/usr/bin/env bash
    set -euo pipefail
    if ! python3 "{{runtime_script}}" ensure-host-services; then
      echo "==> WARNING: local Runtime host agent did not start; run 'just runtime-status' for setup details"
    fi

[private]
_api-up-lite:
    #!/usr/bin/env bash
    set -euo pipefail
    OLLAMA_KEEP_ALIVE="${OLLAMA_KEEP_ALIVE:-30s}" make -C "{{api_dir}}" up

[private]
_stop-host-services:
    #!/usr/bin/env bash
    set -euo pipefail
    if ! python3 "{{runtime_script}}" stop-host-services; then
      echo "==> WARNING: local Runtime host agent did not stop cleanly"
    fi

[private]
_ensure-sysmon:
    #!/usr/bin/env bash
    set -euo pipefail
    sysmon_running() {
      curl -s -m 2 http://localhost:8765/stats >/dev/null 2>&1
    }
    if [[ "${SYSMON_AUTO:-1}" != "1" ]]; then
      exit 0
    fi
    if sysmon_running; then
      echo "sysmon-agent already running"
      exit 0
    fi
    if [[ ! -f "{{sysmon_script}}" ]]; then
      echo "sysmon-agent script not found: {{sysmon_script}} — start it manually with Node.js"
      exit 0
    fi
    nohup node "{{sysmon_script}}" >/tmp/sysmon-agent.log 2>&1 &
    pid=$!
    if ! printf '%s\n' "$pid" >/tmp/sysmon-agent.pid 2>/dev/null; then
      echo "==> WARNING: could not write /tmp/sysmon-agent.pid — down will not stop the agent"
    fi
    for _ in 1 2 3 4; do
      sysmon_running && break
      sleep 0.5
    done
    if kill -0 "$pid" 2>/dev/null && sysmon_running; then
      echo "==> sysmon-agent started (pid $pid)"
    else
      echo "==> WARNING: sysmon-agent may have failed to start — check /tmp/sysmon-agent.log"
    fi

[private]
_stop-sysmon:
    #!/usr/bin/env bash
    set -euo pipefail
    pid_file=/tmp/sysmon-agent.pid
    [[ -f "$pid_file" ]] || exit 0
    pid="$(cat "$pid_file" 2>/dev/null)" || exit 0
    if ps -p "$pid" -o command= 2>/dev/null | grep -Fq "{{sysmon_script}}"; then
      kill -INT "$pid" 2>/dev/null || true
      sleep 1
    fi
    rm -f "$pid_file"

[private]
_ensure-runtime-helper:
    #!/usr/bin/env bash
    set -euo pipefail
    helper_pid_file=/tmp/devassistant-runtime-helper.pid
    helper_log_file=/tmp/devassistant-runtime-helper.log
    helper_url=http://127.0.0.1:47831/health
    if [[ "$(uname -s)" == "Darwin" ]]; then
      runtime_agent_socket="${SSH_AUTH_SOCK:-$(launchctl getenv SSH_AUTH_SOCK 2>/dev/null || true)}"
      if [[ -n "$runtime_agent_socket" ]]; then
        export RUNTIME_SSH_AGENT_SOCKET="$runtime_agent_socket"
      fi
    fi
    stop_helper() {
      [[ -f "$helper_pid_file" ]] || return 0
      local pid
      pid="$(cat "$helper_pid_file" 2>/dev/null)" || return 0
      if ps -p "$pid" -o command= 2>/dev/null | grep -Fq "{{runtime_script}} helper"; then
        kill -TERM "$pid" 2>/dev/null || true
      fi
      rm -f "$helper_pid_file"
    }
    helper_responding() {
      curl -fsS -m 1 -o /dev/null "$helper_url" 2>/dev/null
    }
    helper_state="$(curl -fsS -m 1 "$helper_url" 2>/dev/null || true)"
    api_version="$(printf '%s' "$helper_state" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("api_version", 0))' 2>/dev/null || true)"
    helper_bind="$(printf '%s' "$helper_state" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("helper_bind", ""))' 2>/dev/null || true)"
    if [[ "$api_version" == "29" && "$helper_bind" == "0.0.0.0" ]]; then
      echo "runtime setup helper already running (API v${api_version})"
      exit 0
    fi
    if [[ -n "$api_version" ]]; then
      echo "==> Replacing stale Runtime setup helper (API v${api_version})"
      stop_helper
      for _ in 1 2 3 4 5; do
        ! helper_responding && break
        sleep 0.2
      done
      if helper_responding; then
        echo "==> ERROR: the stale Runtime setup helper still owns port 47831. Stop that process, then run 'just up-dev' again." >&2
        exit 1
      fi
    fi
    nohup env RUNTIME_HELPER_BIND=0.0.0.0 python3 "{{runtime_script}}" helper >"$helper_log_file" 2>&1 &
    pid=$!
    printf '%s\n' "$pid" >"$helper_pid_file" 2>/dev/null || true
    for _ in 1 2 3 4; do
      helper_responding && break
      sleep 0.5
    done
    if kill -0 "$pid" 2>/dev/null && helper_responding; then
      echo "==> Runtime setup helper started (pid $pid)"
    else
      echo "==> WARNING: Runtime setup helper did not start; see $helper_log_file"
    fi

[private]
_stop-runtime-helper:
    #!/usr/bin/env bash
    set -euo pipefail
    helper_pid_file=/tmp/devassistant-runtime-helper.pid
    [[ -f "$helper_pid_file" ]] || exit 0
    pid="$(cat "$helper_pid_file" 2>/dev/null)" || exit 0
    if ps -p "$pid" -o command= 2>/dev/null | grep -Fq "{{runtime_script}} helper"; then
      kill -TERM "$pid" 2>/dev/null || true
    fi
    rm -f "$helper_pid_file"
