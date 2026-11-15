#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT_DIR/scripts/lib-cli.sh"

usage() {
  cat <<'USAGE'
Usage: just php-otel-index [options]

Upload and index the committed HEAD of a PHP project. Local working-tree changes are ignored.
Without options, run in a terminal to choose everything interactively.

Options:
  --project-path PATH       PHP project directory (inside a Git checkout, with composer.json)
  --project-id ID           Runtime project ID (letters, digits, dot, dash, underscore)
  --configure-credentials   Create missing local API upload credentials in api/.env and recreate the
                            Python API container (otherwise a missing credential stops the run)
  -y, --yes                 Accept confirmations without prompting
  --non-interactive         Never prompt; fail when a required option is missing
  -h, --help                Show this help

Example:
  just php-otel-index --project-path ../pizza-builder --project-id pizza --yes
USAGE
}

target_input=''
project_id=''
configure_credentials=0
assume_yes=0
non_interactive=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --project-path)
      [[ $# -ge 2 ]] || { echo 'Missing value for --project-path.' >&2; exit 2; }
      target_input="$2"; shift 2 ;;
    --project-id)
      [[ $# -ge 2 ]] || { echo 'Missing value for --project-id.' >&2; exit 2; }
      project_id="$2"; shift 2 ;;
    --configure-credentials) configure_credentials=1; shift ;;
    -y|--yes) assume_yes=1; shift ;;
    --non-interactive) non_interactive=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
done

ui_init "$non_interactive" "$assume_yes"
ui_require_tools gum docker git python3

ui_title 'Index a PHP source snapshot'
ui_note 'Register the committed HEAD revision so the PHP installer can use its ready immutable revision. Local working-tree changes are ignored.'
printf '\n'

ui_require "$target_input" '--project-path'
if [[ -z "$target_input" ]]; then
  path_choice="$(gum choose --header 'How would you like to select the PHP project?' \
    'Enter or paste a path' 'Browse folders')" || { echo 'Indexing cancelled.'; exit 0; }
  if [[ "$path_choice" == 'Browse folders' ]]; then
    if ! target_input="$(gum file --directory --header 'Browse to the PHP project folder' "$PWD")"; then
      echo 'Folder selection cancelled.'
      exit 0
    fi
  else
    source "$ROOT_DIR/scripts/read-project-path.sh"
    if target_input="$(read_project_path_with_completion 'PHP project path (absolute or relative)')"; then
      :
    else
      result=$?
      [[ $result -eq 1 ]] && { echo 'Indexing cancelled.'; exit 0; }
      exit "$result"
    fi
  fi
fi
if [[ -z "$target_input" ]]; then
  ui_error 'Enter or select a project folder to continue.'
  exit 2
fi
if ! target_dir="$(cd "$target_input" 2>/dev/null && pwd -P)"; then
  ui_error "Project directory does not exist: $target_input"
  exit 2
fi
if ! repo_root="$(git -C "$target_dir" rev-parse --show-toplevel 2>/dev/null)"; then
  ui_error 'The selected PHP project must be inside a Git checkout.'
  exit 2
fi
repo_root="$(cd "$repo_root" && pwd -P)"
if [[ ! -f "$target_dir/composer.json" ]]; then
  ui_error "No composer.json found in the selected PHP project directory: $target_dir"
  exit 2
fi

if ! commit_sha="$(git -C "$repo_root" rev-parse --verify HEAD 2>/dev/null)" || [[ ! "$commit_sha" =~ ^([0-9a-f]{40}|[0-9a-f]{64})$ ]]; then
  ui_error 'Could not identify a full source revision for this checkout.'
  exit 2
fi

default_project_id="$(basename "$target_dir" | LC_ALL=C tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9._-]+/-/g; s/^[^a-z0-9]+//; s/[-._]+$//')"
valid_project_id "$default_project_id" || default_project_id='php-project'
ui_require "$project_id" '--project-id'
if [[ -z "$project_id" ]]; then
  source "$ROOT_DIR/scripts/select-runtime-project-id.sh"
  if project_id="$(choose_runtime_project_id "$ROOT_DIR" "$default_project_id")"; then
    :
  else
    result=$?
    [[ $result -eq 1 ]] && { echo 'Indexing cancelled.'; exit 0; }
    exit "$result"
  fi
fi
if ! valid_project_id "$project_id"; then
  ui_error 'Project ID must start with a letter or digit and contain only letters, digits, dot, underscore, or hyphen (maximum 128 characters).'
  exit 2
fi

ui_info "Checkout: $repo_root"
ui_info "Commit: $commit_sha"
ui_info "Project ID: $project_id"
ui_note 'Only files tracked by the current committed revision will be uploaded.'
if ! ui_confirm 'Upload this source snapshot to the local Developer Assistant API?'; then
  echo 'Indexing cancelled.'
  exit 0
fi

if ! ui_spin 'Waiting for the local API health check…' bash "$ROOT_DIR/scripts/wait-for-local-api.sh" "$ROOT_DIR/api"; then
  exit 2
fi

if ! credentials_check="$(bash "$ROOT_DIR/scripts/ensure-local-api-credentials.sh" "$project_id" --status)"; then
  exit 2
fi
if [[ "$credentials_check" != 'ready' ]]; then
  ui_info "The local API needs a source-upload token for '$project_id' and a local download signing secret. Missing values can be created, saved privately in api/.env, and loaded by recreating only the Python API container. Secret values will not be shown."
  if [[ "$configure_credentials" != 1 ]] && ! ui_is_interactive; then
    ui_error 'Local API credentials are missing. Pass --configure-credentials to create them.'
    exit 2
  fi
  if [[ "$configure_credentials" != 1 ]] && ! ui_confirm \
    'Create or reuse the required local API credentials and recreate the Python API container?' 'Configure local API' 'Cancel'; then
    echo 'Indexing cancelled; no local API credentials were changed.'
    exit 0
  fi
  if ! ui_spin 'Configuring local API credentials…' bash "$ROOT_DIR/scripts/ensure-local-api-credentials.sh" "$project_id"; then
    ui_error 'Local API credentials could not be configured. Resolve the error above, then rerun the index action.'
    exit 2
  fi
fi

ui_note 'Uploading and indexing. A ready revision requires a live matching deployment reported by the telemetry host agent.'
if ! git -C "$repo_root" archive --format=tar "$commit_sha" \
  | python3 "$ROOT_DIR/scripts/package-git-source-zip.py" \
  | (
  cd "$ROOT_DIR/api"
  docker compose exec -T python-api python -c "$(cat "$ROOT_DIR/scripts/upload-php-source-snapshot.py")" \
    "$project_id" "$commit_sha"
); then
  ui_error 'Source indexing did not complete successfully. Review the message above and correct the reported issue.'
  exit 2
fi

ui_success 'Source snapshot is ready. Run just php-otel-install to connect the PHP project, or choose that action from the just menu.'
