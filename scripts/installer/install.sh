#!/usr/bin/env bash
# Stage owner. Compatible with the Bash 3.2 shipped by macOS.
set -Eeuo pipefail
umask 077
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
export INSTALL_ROOT="$ROOT"
export PATH="$ROOT/.install/bin:$ROOT/.install/venv/bin:$PATH"
export PIP_CACHE_DIR="$ROOT/.install/cache/pip"
CLI="$ROOT/scripts/installer/cli.py"
PYTHON=$(command -v python3 || true)
if [[ -z "$PYTHON" ]] || ! "$PYTHON" -c 'import sys; raise SystemExit(sys.version_info < (3,10))'; then
  for candidate in python3.14 python3.13 python3.12 python3.11 python3.10; do
    if command -v "$candidate" >/dev/null 2>&1; then PYTHON=$(command -v "$candidate"); break; fi
  done
fi
[[ -n "$PYTHON" ]] || {
  printf '%s\n' 'Host Python is missing. Install Python >=3.10 with venv support, then bash install.sh.' >&2
  exit 1
}
export INSTALL_HOST_PYTHON="$PYTHON"
if [[ "${1:-}" != --locked ]]; then
  exec "$PYTHON" "$CLI" lock "$0" --locked "$@"
fi
shift
action=${1:-resume}
ctl() { "$PYTHON" "$CLI" "$@"; }
case "$action" in
  status|info) ctl "$action"; exit ;;
  check) ctl report strict; if [[ -f "$ROOT/.install/choices.json" ]]; then ctl ports; fi; exit ;;
  install|resume) ;;
  *) printf '%s\n' 'Usage: install.sh [install|resume|check|status|info]' >&2; exit 2 ;;
esac

paused() {
  printf '\nInstallation paused. Completed work and caches are retained.\nResume from this app folder: bash install.sh\n' >&2
}
trap 'paused' ERR
trap 'printf "\nCancelled.\n" >&2; paused; exit 130' INT
trap 'paused; exit 143' TERM

run() { "$@" 2>&1 | ctl logs; }
retry() {
  local attempt delay
  for attempt in 1 2 3; do
    if run "$@"; then return 0; fi
    if [[ "$attempt" != 3 ]]; then
      delay=$((attempt * attempt * 2))
      printf 'Network/build operation failed; retry %s/3 in %ss.\n' "$((attempt + 1))" "$delay"
      sleep "$delay"
    fi
  done
  return 1
}
progress() {
  local done=$1 title=$2 bar='' i
  for ((i=0; i<8; i++)); do
    if ((i < done)); then bar="${bar}#"; else bar="${bar}-"; fi
  done
  printf '\n[%s] %d%% %s\n' "$bar" "$((done * 100 / 8))" "$title"
}
stage() {
  local name=$1 index=$2; shift 2
  progress "$index" "$name"
  if [[ "$name" == images || "$name" == build || "$name" == models ]]; then
    if ctl valid "$name" && ctl probe "$name"; then
      printf 'Revalidated cached %s.\n' "$name"
      return 0
    fi
  fi
  "$@"
  ctl checkpoint "$name"
}

ctl report
if [[ "$action" == install ]]; then ctl choose edit; else ctl choose; fi
ctl ports
ctl review
printf '\nApprove this review, including dependency/system changes? Type yes: '
read -r approved
[[ "$approved" == yes ]] || { paused; exit 1; }

dependencies() {
  if ! ctl report strict; then retry ctl dependencies; fi
  "$PYTHON" -c 'import sys; raise SystemExit(sys.version_info < (3,10))' || {
    printf '%s\n' 'Select Python >=3.10 on PATH and rerun bash install.sh.' >&2; return 1;
  }
  if [[ ! -x "$ROOT/.install/venv/bin/python3" ]]; then
    "$PYTHON" -m venv "$ROOT/.install/venv"
  fi
  PYTHON="$ROOT/.install/venv/bin/python3"
  hash -r
  retry "$ROOT/.install/venv/bin/python3" -m pip install --disable-pip-version-check -r "$ROOT/scripts/installer/requirements.txt"
  ctl report strict
}
images() { retry ctl compose api pull --ignore-buildable; retry ctl compose web pull --ignore-buildable; }
build() { retry ctl compose api build; retry ctl compose web build web; }
services() {
  run ctl prepare
  docker network inspect devassistant_net >/dev/null 2>&1 || run docker network create devassistant_net
  # Existing Runtime/Make owners pick up the generated automatic overrides.
  run just --justfile "$ROOT/justfile" _ensure-runtime-helper
  if [[ "$(uname -s)" == Darwin ]]; then
    export RUNTIME_SSH_AGENT_SOURCE=/run/host-services/ssh-auth.sock
  else
    export RUNTIME_SSH_AGENT_SOURCE="$HOME/.local/state/devassistant/runtime-host/ssh-agent.sock"
    mkdir -p "$(dirname "$RUNTIME_SSH_AGENT_SOURCE")"
    if [[ ! -S "$RUNTIME_SSH_AGENT_SOURCE" ]]; then
      ssh-agent -a "$RUNTIME_SSH_AGENT_SOURCE" >/dev/null
    fi
  fi
  run ctl compose api up -d --wait --wait-timeout 300
  run just --justfile "$ROOT/justfile" _ensure-host-services
  run ctl compose web up -d --wait --wait-timeout 300
  run just --justfile "$ROOT/justfile" _ensure-sysmon
  run python3 "$ROOT/api/telemetry-dev/runtime_stack.py" start
}

stage dependencies 0 dependencies
stage configuration 1 run ctl configuration
stage certificates 2 run ctl certificates
stage images 3 images
stage build 4 build
stage models 5 retry ctl models
stage services 6 services
stage verification 7 run ctl verify
progress 8 'Installation verified'
ctl info
