#!/usr/bin/env bash

choose_runtime_project_id() {
  local root_dir="$1"
  local default_project_id="$2"
  local project_ids selection project_id

  if ! project_ids="$(cd "$root_dir/api" && docker compose exec -T python-api python -c '
from telemetry.registry import default_registry
print("\n".join(sorted({item["project_id"] for item in default_registry().runtime_projects() if item.get("project_id")})))
' 2>/dev/null)"; then
    gum style --foreground 196 "Could not read Runtime project IDs from the local API. Start the stack with 'just up-dev', then retry." >&2
    return 2
  fi

  if [[ -n "$project_ids" ]]; then
    if ! selection="$(gum choose --header 'Find the Runtime project for this PHP source' \
      'Search existing project IDs' 'Enter a project ID manually')"; then
      return 1
    fi
    if [[ "$selection" == 'Search existing project IDs' ]]; then
      if ! project_id="$(printf '%s\n' "$project_ids" | gum filter --fuzzy --strict --height 12 \
        --header 'Search Runtime project IDs · type to filter, Enter to select' \
        --placeholder 'Search project IDs…' --value "$default_project_id" --select-if-one)"; then
        return 1
      fi
    else
      if ! project_id="$(gum input --header 'Runtime project ID (letters, numbers, dot, dash, underscore)' \
        --value "$default_project_id")"; then
        return 1
      fi
    fi
  else
    gum style --foreground 245 'No Runtime project IDs are listed yet. Enter the project ID selected during Runtime setup.'
    if ! project_id="$(gum input --header 'Runtime project ID (letters, numbers, dot, dash, underscore)' \
      --value "$default_project_id")"; then
      return 1
    fi
  fi

  if [[ ! "$project_id" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]]; then
    gum style --foreground 196 'Project ID must start with a letter or digit and contain only letters, digits, dot, underscore, or hyphen (maximum 128 characters).' >&2
    return 2
  fi
  printf '%s\n' "$project_id"
}
