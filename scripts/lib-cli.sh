#!/usr/bin/env bash
# Shared prompts and messages for the PHP telemetry scripts.
#
# Interactive mode (a terminal and gum are available, and --non-interactive was not passed) keeps the
# gum menus. Non-interactive mode prints plain text, never prompts, and requires every choice to come
# from a command-line option; confirmations then need --yes.

UI_INTERACTIVE=0
UI_ASSUME_YES=0

# ui_init <non_interactive 0|1> <assume_yes 0|1>
ui_init() {
  UI_ASSUME_YES="$2"
  if [[ "$1" == 1 ]]; then
    UI_INTERACTIVE=0
  elif [[ -t 0 && -t 1 ]] && command -v gum >/dev/null 2>&1; then
    UI_INTERACTIVE=1
  else
    UI_INTERACTIVE=0
  fi
}

ui_is_interactive() { [[ "$UI_INTERACTIVE" == 1 ]]; }

ui_title() {
  if ui_is_interactive; then
    gum style --border rounded --border-foreground 99 --padding '0 2' --margin '1 0' --bold --foreground 213 "$1"
  else
    printf '== %s ==\n' "$1"
  fi
}

ui_info() {
  if ui_is_interactive; then gum style --foreground 252 "$*"; else printf '%s\n' "$*"; fi
}

ui_note() {
  if ui_is_interactive; then gum style --foreground 245 "$*"; else printf '%s\n' "$*"; fi
}

ui_success() {
  if ui_is_interactive; then gum style --foreground 42 "$*"; else printf '%s\n' "$*"; fi
}

ui_error() {
  if ui_is_interactive; then gum style --foreground 196 "$*" >&2; else printf 'Error: %s\n' "$*" >&2; fi
}

ui_box() {
  if ui_is_interactive; then gum style --border rounded --border-foreground 39 --padding '0 1' "$1"; else printf '%s\n' "$1"; fi
}

# ui_spin <title> <command...>: runs the command, with a spinner when interactive.
ui_spin() {
  local title="$1"
  shift
  if ui_is_interactive; then
    gum spin --title "$title" --show-error -- "$@"
  else
    printf '%s\n' "$title"
    "$@"
  fi
}

# ui_confirm <prompt> [affirmative] [negative]: --yes accepts; non-interactive without --yes refuses.
ui_confirm() {
  if [[ "$UI_ASSUME_YES" == 1 ]]; then
    return 0
  fi
  if ! ui_is_interactive; then
    ui_error "Confirmation required: $1 Pass --yes to accept it without a prompt."
    return 1
  fi
  # Spelled out rather than building an array: bash 3.2 (macOS) rejects empty arrays under `set -u`.
  gum confirm --affirmative "${2:-Yes}" --negative "${3:-No}" "$1"
}

# ui_require <value> <option>: in non-interactive mode, a missing value names the option to pass.
ui_require() {
  if [[ -z "$1" ]] && ! ui_is_interactive; then
    ui_error "Missing $2. Pass it as an option, or run in a terminal to choose interactively."
    exit 2
  fi
}

# ui_require_tools <tool...>: gum is only required for interactive runs.
ui_require_tools() {
  local tool
  for tool in "$@"; do
    if [[ "$tool" == gum ]] && ! ui_is_interactive; then
      continue
    fi
    if ! command -v "$tool" >/dev/null 2>&1; then
      echo "Required command not found: $tool" >&2
      exit 127
    fi
  done
}

valid_project_id() { [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]]; }
