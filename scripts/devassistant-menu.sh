#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -t 0 || ! -t 1 ]]; then
  echo "The interactive menu needs a terminal. Run a named recipe such as 'just up-dev' instead." >&2
  exit 2
fi

if ! command -v gum >/dev/null 2>&1; then
  echo "Gum is required for the interactive menu. Install Just and Gum, then rerun 'just'." >&2
  echo "With Homebrew: brew install just gum" >&2
  exit 127
fi

if ! command -v just >/dev/null 2>&1; then
  echo "Just is required to run project actions. Install it, then rerun this menu." >&2
  exit 127
fi

draw_brand() {
  gum style \
    --border rounded \
    --border-foreground 99 \
    --padding '0 2' \
    --margin '1 0' \
    --bold \
    --foreground 213 \
    'Developer Assistant'
  gum style --foreground 245 'Local services · Runtime tools · Developer actions'
  printf '\n'
}

choose_one() {
  local header="$1"
  shift
  gum choose \
    --height 12 \
    --header "$header" \
    --cursor '› ' \
    --cursor.foreground 213 \
    --selected.foreground 255 \
    "$@"
}

run_recipe() {
  local recipe="$1"
  printf '\n'
  if just "$recipe"; then
    printf '\n'
    gum style --foreground 42 "✓ Completed: just $recipe"
  else
    local result=$?
    printf '\n' >&2
    gum style --foreground 196 "✗ Command failed (exit $result): just $recipe" >&2
  fi
  printf '\n'
  read -r -p 'Press Enter to return to the menu… ' _ || true
}

run_action_menu() {
  if select_action "$@"; then
    return 0
  else
    local result=$?
    [[ $result -eq 1 ]] && return 0
    return "$result"
  fi
}

select_action() {
  local category="$1"
  local selection
  local recipe=''
  shift

  if selection="$(choose_one "$category · Select an action" "$@")"; then
    :
  else
    local result=$?
    case $result in
      1) return 1 ;;
      130) exit 130 ;;
      *) return "$result" ;;
    esac
  fi

  case "$category:$selection" in
    'Full stack:Start development stack') recipe=up-dev ;;
    'Full stack:Start production stack') recipe=up ;;
    'Full stack:Start low-memory stack') recipe=up-lite ;;
    'Full stack:Restart development stack') recipe=restart-dev ;;
    'Full stack:Restart production stack') recipe=restart ;;
    'Full stack:Stop services') recipe=stop ;;
    'Full stack:Stop and remove containers') recipe=down ;;
    'Full stack:Show status') recipe=status ;;
    'Full stack:Follow logs') recipe=logs ;;

    'API:Start API') recipe=api-up ;;
    'API:Stop API and keep containers') recipe=api-stop ;;
    'API:Restart API') recipe=api-restart ;;
    'API:Stop and remove API containers') recipe=api-down ;;
    'API:Show API status') recipe=api-status ;;
    'API:Follow API logs') recipe=api-logs ;;

    'Web:Start Web development service') recipe=web-up-dev ;;
    'Web:Start production Web service') recipe=web-up ;;
    'Web:Restart Web development service') recipe=web-restart-dev ;;
    'Web:Restart production Web service') recipe=web-restart ;;
    'Web:Stop production Web service') recipe=web-stop ;;
    'Web:Stop and remove production Web container') recipe=web-down ;;
    'Web:Stop and remove Web development containers') recipe=web-dev-down ;;
    'Web:Show Web status') recipe=web-status ;;
    'Web:Follow Web logs') recipe=web-logs ;;

    'Runtime:Set up Runtime') recipe=runtime-setup ;;
    'Runtime:Show Runtime setup status') recipe=runtime-status ;;
    'Runtime:Clean up Runtime') recipe=runtime-cleanup ;;
    'PHP telemetry:Set up a PHP project (one step)') recipe=php-otel-setup ;;
    'PHP telemetry:Index a source snapshot') recipe=php-otel-index ;;
    'PHP telemetry:Install in a PHP project') recipe=php-otel-install ;;
    *:Back) return 1 ;;
    *)
      gum style --foreground 196 "Unknown menu action: $selection" >&2
      return 2
      ;;
  esac

  run_recipe "$recipe"
  return 0
}

while true; do
  clear
  draw_brand

  if category="$(choose_one 'Choose a service or action' \
    'Full stack' 'API' 'Web' 'Runtime' 'PHP telemetry' 'Exit')"; then
    :
  else
    result=$?
    case $result in
      1) exit 0 ;;
      130) exit 130 ;;
      *) exit "$result" ;;
    esac
  fi

  case "$category" in
    'Full stack')
      run_action_menu 'Full stack' \
        'Start development stack' \
        'Start production stack' \
        'Start low-memory stack' \
        'Restart development stack' \
        'Restart production stack' \
        'Stop services' \
        'Stop and remove containers' \
        'Show status' \
        'Follow logs' \
        'Back'
      ;;
    'API')
      run_action_menu API \
        'Start API' \
        'Stop API and keep containers' \
        'Restart API' \
        'Stop and remove API containers' \
        'Show API status' \
        'Follow API logs' \
        'Back'
      ;;
    'Web')
      run_action_menu Web \
        'Start Web development service' \
        'Start production Web service' \
        'Restart Web development service' \
        'Restart production Web service' \
        'Stop production Web service' \
        'Stop and remove production Web container' \
        'Stop and remove Web development containers' \
        'Show Web status' \
        'Follow Web logs' \
        'Back'
      ;;
    'Runtime')
      run_action_menu Runtime \
        'Set up Runtime' \
        'Show Runtime setup status' \
        'Clean up Runtime' \
        'Back'
      ;;
    'PHP telemetry')
      run_action_menu 'PHP telemetry' \
        'Set up a PHP project (one step)' \
        'Index a source snapshot' \
        'Install in a PHP project' \
        'Back'
      ;;
    Exit) exit 0 ;;
  esac
done
