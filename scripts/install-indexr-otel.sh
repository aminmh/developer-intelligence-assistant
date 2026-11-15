#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSER_BIN="${COMPOSER_BIN:-composer}"
source "$ROOT_DIR/scripts/lib-cli.sh"

usage() {
  cat <<'USAGE'
Usage: just php-otel-install [options]

Install the Indexr PHP adapter in a project and connect it to a ready indexed revision.
Without options, run in a terminal to choose everything interactively.

Options:
  --project-path PATH          PHP project directory (Git checkout with composer.json)
  --adapter NAME               laravel | symfony | core (detected from composer.json when omitted)
  --package-source SOURCE      local (editable path repository) | published
  --library-path PATH          indexr-otel-library checkout for --package-source local
                               (default: ../indexr-otel-library next to Developer Assistant)
  --version CONSTRAINT         Composer constraint for --package-source published, e.g. ^1.0
  --revision ID                Connect to this ready revision (rev_ followed by 32 hex characters)
  --index                      Index the project's committed HEAD now and connect to that revision
  --project-id ID              Runtime project ID for --index (also narrows --revision)
  --configure-credentials      With --index: create missing local API upload credentials
  --service-name NAME          OpenTelemetry service.name (default: the project folder name)
  --endpoint WHERE             host | docker-desktop | docker-network, where the PHP project runs:
                               host            http://127.0.0.1:24318
                               docker-desktop  http://host.docker.internal:24318
                               docker-network  http://telemetry-collector-runtime:4318
  -y, --yes                    Accept confirmations without prompting
  --non-interactive            Never prompt; fail when a required option is missing
  -h, --help                   Show this help

Example:
  just php-otel-install --project-path ../pizza-builder --package-source local \
    --revision rev_0123456789abcdef0123456789abcdef --endpoint docker-network --yes
USAGE
}

target_input=''
adapter_option=''
package_option=''
library_option=''
version_constraint=''
revision_option=''
index_now=0
project_option=''
configure_credentials=0
service_option=''
endpoint_option=''
assume_yes=0
non_interactive=0
need_value() { [[ $# -ge 2 ]] || { echo "Missing value for $1." >&2; exit 2; }; }
while [[ $# -gt 0 ]]; do
  case "$1" in
    --project-path) need_value "$@"; target_input="$2"; shift 2 ;;
    --adapter) need_value "$@"; adapter_option="$2"; shift 2 ;;
    --package-source) need_value "$@"; package_option="$2"; shift 2 ;;
    --library-path) need_value "$@"; library_option="$2"; shift 2 ;;
    --version) need_value "$@"; version_constraint="$2"; shift 2 ;;
    --revision) need_value "$@"; revision_option="$2"; shift 2 ;;
    --index) index_now=1; shift ;;
    --project-id) need_value "$@"; project_option="$2"; shift 2 ;;
    --configure-credentials) configure_credentials=1; shift ;;
    --service-name) need_value "$@"; service_option="$2"; shift 2 ;;
    --endpoint) need_value "$@"; endpoint_option="$2"; shift 2 ;;
    -y|--yes) assume_yes=1; shift ;;
    --non-interactive) non_interactive=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
done
if [[ -n "$revision_option" && "$index_now" == 1 ]]; then
  echo 'Choose either --revision or --index, not both.' >&2
  exit 2
fi
if [[ -n "$adapter_option" && ! "$adapter_option" =~ ^(laravel|symfony|core)$ ]]; then
  echo '--adapter must be laravel, symfony, or core.' >&2
  exit 2
fi
if [[ -n "$package_option" && ! "$package_option" =~ ^(local|published)$ ]]; then
  echo '--package-source must be local or published.' >&2
  exit 2
fi
if [[ -n "$endpoint_option" && ! "$endpoint_option" =~ ^(host|docker-desktop|docker-network)$ ]]; then
  echo '--endpoint must be host, docker-desktop, or docker-network.' >&2
  exit 2
fi

ui_init "$non_interactive" "$assume_yes"
ui_require_tools gum just docker git python3 "$COMPOSER_BIN"

ui_title 'Indexr PHP OpenTelemetry installer'
ui_note 'Install the PHP adapter and connect it to an indexed Developer Assistant revision.'
printf '\n'

ui_require "$target_input" '--project-path'
if [[ -z "$target_input" ]]; then
  path_choice="$(gum choose --header 'How would you like to select the target PHP project?' \
    'Enter or paste a path' 'Browse folders')" || { echo 'Installation cancelled.'; exit 0; }
  if [[ "$path_choice" == 'Browse folders' ]]; then
    if ! target_input="$(gum file --directory --header 'Browse to the target PHP project folder' "$PWD")"; then
      echo 'Folder selection cancelled.'
      exit 0
    fi
  else
    source "$ROOT_DIR/scripts/read-project-path.sh"
    if target_input="$(read_project_path_with_completion 'Target PHP project path (absolute or relative)')"; then
      :
    else
      result=$?
      [[ $result -eq 1 ]] && { echo 'Installation cancelled.'; exit 0; }
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
if [[ ! -f "$target_dir/composer.json" ]]; then
  ui_error "No composer.json found in $target_dir"
  exit 2
fi
if [[ -L "$target_dir/.env" ]]; then
  ui_error 'The project .env is a symlink. Refusing to modify a file outside the project directory.'
  exit 2
fi

framework="$(python3 - "$target_dir/composer.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding='utf-8') as stream:
    package = json.load(stream)
dependencies = {**package.get('require', {}), **package.get('require-dev', {})}
if 'laravel/framework' in dependencies:
    print('laravel')
elif 'symfony/framework-bundle' in dependencies:
    print('symfony')
else:
    print('generic')
PY
)"

[[ -n "$adapter_option" ]] || { [[ "$framework" == generic ]] || adapter_option="$framework"; }
ui_require "$adapter_option" '--adapter (no Laravel or Symfony dependency was detected)'
if [[ -z "$adapter_option" ]]; then
  adapter_choice="$(gum choose --header 'Choose the PHP integration package' \
    'Core only (plain PHP)' 'Laravel' 'Symfony')" || { echo 'Installation cancelled.'; exit 0; }
  case "$adapter_choice" in
    'Laravel') adapter_option='laravel' ;;
    'Symfony') adapter_option='symfony' ;;
    *) adapter_option='core' ;;
  esac
fi
adapter="indexr/indexer-otel-$adapter_option"

ui_require "$package_option" '--package-source (local or published)'
if [[ -z "$package_option" ]]; then
  package_choice="$(gum choose --header 'Choose how Composer installs the Indexr package' \
    'Local checkout (editable path repository)' 'Published Composer package')" || { echo 'Installation cancelled.'; exit 0; }
  [[ "$package_choice" == 'Published Composer package' ]] && package_option='published' || package_option='local'
fi

local_repo_name='indexr-otel-local'
local_version='0.1.0'
repo_json=''
existing_repo=''
if [[ "$package_option" == 'local' ]]; then
  package_source='Local checkout (editable path repository)'
  default_library="$ROOT_DIR/../indexr-otel-library"
  library_input="$library_option"
  if [[ -z "$library_input" ]]; then
    if ui_is_interactive; then
      if ! library_input="$(gum input --header 'Path to the indexr-otel-library checkout' --value "$default_library")"; then
        echo 'Installation cancelled.'
        exit 0
      fi
    else
      library_input="$default_library"
    fi
  fi
  if ! library_dir="$(cd "$library_input" 2>/dev/null && pwd -P)" || [[ ! -f "$library_dir/packages/core/composer.json" ]]; then
    ui_error "No Indexr PHP library checkout found at: $library_input"
    exit 2
  fi
  if [[ ! -f "$library_dir/packages/laravel/composer.json" || ! -f "$library_dir/packages/symfony/composer.json" ]]; then
    ui_error 'The local checkout must contain core, Laravel, and Symfony packages.'
    exit 2
  fi
  repo_json="$(python3 - "$library_dir/packages/*" <<'PY'
import json
import sys

print(json.dumps({
    'type': 'path',
    'url': sys.argv[1],
    'options': {
        'symlink': True,
        'versions': {
            'indexr/indexer-otel-core': '0.1.0',
            'indexr/indexer-otel-laravel': '0.1.0',
            'indexr/indexer-otel-symfony': '0.1.0',
        },
    },
}, separators=(',', ':')))
PY
)"
  existing_repo="$(python3 - "$target_dir/composer.json" "$local_repo_name" "$repo_json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding='utf-8') as stream:
    package = json.load(stream)
repositories = package.get('repositories', [])
if isinstance(repositories, dict):
    existing = repositories.get(sys.argv[2])
else:
    existing = next((item for item in repositories if isinstance(item, dict) and item.get('name') == sys.argv[2]), None)
if existing is None:
    print('absent')
else:
    expected = json.loads(sys.argv[3])
    keys = ('type', 'url', 'options')
    print('matching' if all(existing.get(key) == expected.get(key) for key in keys) else 'conflicting')
PY
)"
  if [[ "$existing_repo" == 'conflicting' ]]; then
    ui_error "Composer repository '$local_repo_name' already points to a different configuration. Resolve that entry before installing."
    exit 2
  fi
else
  package_source='Published Composer package'
  ui_require "$version_constraint" '--version (Composer constraint for the published package)'
  if [[ -z "$version_constraint" ]]; then
    if ! version_constraint="$(gum input --header 'Published Composer version constraint (for example ^1.0 or dev-main)' --placeholder '^1.0')"; then
      echo 'Installation cancelled.'
      exit 0
    fi
  fi
  if [[ -z "$version_constraint" || "$version_constraint" == *$'\n'* ]]; then
    ui_error 'Enter one Composer version constraint.'
    exit 2
  fi
fi

if ! bash "$ROOT_DIR/scripts/wait-for-local-api.sh" "$ROOT_DIR/api"; then
  ui_error 'Could not read indexed revisions from the local Developer Assistant API. Start the stack with `just up-dev`, wait for the API, then rerun.'
  exit 2
fi
ready_revisions=''
ready_revisions_read=0
for attempt in {1..5}; do
  if ready_revisions="$(cd "$ROOT_DIR/api" && docker compose exec -T python-api python -c 'from telemetry.registry import default_registry; print("\n".join("\t".join((p["project_id"], p["revision_id"])) for p in default_registry().runtime_projects() if p.get("state") == "ready" and p.get("revision_id")))' </dev/null 2>/dev/null)"; then
    ready_revisions_read=1
    break
  fi
  sleep 1
done
if [[ "$ready_revisions_read" != 1 ]]; then
  ui_error 'The API is healthy, but its indexed revisions could not be read. Check `just api-logs`, then retry.'
  exit 2
fi

if [[ "$index_now" == 1 ]]; then
  revision_action='Index this project now'
elif [[ -n "$revision_option" ]]; then
  revision_action='Choose an existing ready revision'
elif ! ui_is_interactive; then
  ui_error 'Missing --revision ID or --index. Pass one, or run in a terminal to choose interactively.'
  exit 2
elif [[ -n "$ready_revisions" ]]; then
  revision_action="$(gum choose --header 'Choose how to connect this PHP project' \
    'Choose an existing ready revision' 'Index this project now')" || { echo 'Installation cancelled.'; exit 0; }
else
  ui_note 'No ready indexed revisions are available yet. You can index this project from here.'
  revision_action='Index this project now'
fi

if [[ "$revision_action" == 'Index this project now' ]]; then
  if ! target_commit="$(git -C "$target_dir" rev-parse --verify HEAD 2>/dev/null)"; then
    ui_error 'The target project must be a Git checkout before its source can be indexed.'
    exit 2
  fi
  default_project_id="$(basename "$target_dir" | LC_ALL=C tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9._-]+/-/g; s/^[^a-z0-9]+//; s/[-._]+$//')"
  valid_project_id "$default_project_id" || default_project_id='php-project'
  project_id="$project_option"
  ui_require "$project_id" '--project-id'
  if [[ -z "$project_id" ]]; then
    source "$ROOT_DIR/scripts/select-runtime-project-id.sh"
    if project_id="$(choose_runtime_project_id "$ROOT_DIR" "$default_project_id")"; then
      :
    else
      result=$?
      [[ $result -eq 1 ]] && { echo 'Installation cancelled.'; exit 0; }
      exit "$result"
    fi
  fi
  index_args=(--project-path "$target_dir" --project-id "$project_id")
  [[ "$assume_yes" == 1 ]] && index_args+=(--yes)
  [[ "$configure_credentials" == 1 ]] && index_args+=(--configure-credentials)
  ui_is_interactive || index_args+=(--non-interactive)
  if ! bash "$ROOT_DIR/scripts/index-php-source-snapshot.sh" "${index_args[@]}"; then
    ui_error 'Source indexing did not finish. Fix the reported issue and rerun the installer.'
    exit 2
  fi
  if ! revision_id="$(cd "$ROOT_DIR/api" && docker compose exec -T python-api python -c 'import sys; from telemetry.registry import default_registry; snapshot = default_registry().get(sys.argv[1], sys.argv[2]); print(snapshot.get("revision_id", "") if snapshot and snapshot.get("state") == "ready" else "")' "$project_id" "$target_commit" 2>/dev/null)"; then
    ui_error 'The API could not resolve the indexed revision after upload. Retry the index step.'
    exit 2
  fi
  selected_revision="${project_id}"$'\t'"${revision_id}"
elif [[ -n "$revision_option" ]]; then
  selected_revision="$(printf '%s\n' "$ready_revisions" | awk -F'\t' -v revision="$revision_option" -v project="$project_option" \
    '$2 == revision && (project == "" || $1 == project) { print; exit }')"
  if [[ -z "$selected_revision" ]]; then
    ui_error "Revision $revision_option is not a ready indexed revision${project_option:+ of project $project_option}."
    exit 2
  fi
else
  if ! selected_revision="$(printf '%s\n' "$ready_revisions" | gum choose --height 12 --header 'Choose a ready indexed project revision')"; then
    echo 'Installation cancelled.'
    exit 0
  fi
fi
IFS=$'\t' read -r project_id revision_id <<< "$selected_revision"
if [[ ! "$revision_id" =~ ^rev_[a-f0-9]{32}$ ]]; then
  ui_error 'The selected revision ID is malformed; refresh the Developer Assistant API and retry.'
  exit 2
fi

if ! snapshot_commit="$(cd "$ROOT_DIR/api" && docker compose exec -T python-api python -c 'import sys; from telemetry.registry import default_registry; snapshot = default_registry().get_by_revision_id(sys.argv[1], sys.argv[2]); print(snapshot["commit_sha"] if snapshot else "")' "$project_id" "$revision_id" 2>/dev/null)"; then
  ui_error 'Could not resolve the selected revision in the local Developer Assistant API. Refresh and retry.'
  exit 2
fi
if [[ ! "$snapshot_commit" =~ ^([0-9a-f]{40}|[0-9a-f]{64})$ ]]; then
  ui_error 'The selected indexed revision has no valid source provenance. Refresh and retry.'
  exit 2
fi
if ! target_commit="$(git -C "$target_dir" rev-parse --verify HEAD 2>/dev/null)"; then
  ui_error 'The target project must be a Git checkout so the installer can verify it matches the selected indexed revision.'
  exit 2
fi
if [[ "$target_commit" != "$snapshot_commit" ]] || [[ -n "$(git -C "$target_dir" status --porcelain --untracked-files=normal -- . ':!.env')" ]]; then
  ui_error 'The target checkout does not exactly match the selected indexed revision or has local changes. Index a clean checkout, then choose its ready revision.'
  exit 2
fi

service_default="$(basename "$target_dir" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9._-' '-' | sed -E 's/-+$//')"
service_name="$service_option"
if [[ -z "$service_name" ]]; then
  if ui_is_interactive; then
    if ! service_name="$(gum input --header 'OpenTelemetry service.name' --value "$service_default")"; then
      echo 'Installation cancelled.'
      exit 0
    fi
  else
    service_name="$service_default"
  fi
fi
if [[ -z "$service_name" || "$service_name" == *','* || "$service_name" == *'='* ]]; then
  ui_error 'service.name must be non-empty and cannot contain commas or equals signs.'
  exit 2
fi

ui_require "$endpoint_option" '--endpoint (host, docker-desktop, or docker-network)'
if [[ -z "$endpoint_option" ]]; then
  endpoint_choice="$(gum choose --header 'Where does the PHP project run?' \
    'On this host (http://127.0.0.1:24318)' \
    'In Docker Desktop (http://host.docker.internal:24318)' \
    'On the Developer Assistant Docker network (http://telemetry-collector-runtime:4318)')" || { echo 'Installation cancelled.'; exit 0; }
  case "$endpoint_choice" in
    'In Docker Desktop (http://host.docker.internal:24318)') endpoint_option='docker-desktop' ;;
    'On the Developer Assistant Docker network (http://telemetry-collector-runtime:4318)') endpoint_option='docker-network' ;;
    *) endpoint_option='host' ;;
  esac
fi
case "$endpoint_option" in
  docker-desktop) otlp_endpoint='http://host.docker.internal:24318' ;;
  docker-network) otlp_endpoint='http://telemetry-collector-runtime:4318' ;;
  *) otlp_endpoint='http://127.0.0.1:24318' ;;
esac

env_path="$target_dir/.env"
if [[ -e "$env_path" && ! -f "$env_path" ]]; then
  ui_error 'The target .env path exists and is not a regular file.'
  exit 2
fi
if [[ -e "$env_path" && ! -w "$env_path" ]] || [[ ! -e "$env_path" && ! -w "$target_dir" ]]; then
  ui_error 'The target project .env file or directory is not writable.'
  exit 2
fi

if ! python3 "$ROOT_DIR/scripts/write-php-otel-env.py" "$env_path" "$service_name" "$project_id" "$revision_id" "$otlp_endpoint" --check; then
  exit 2
fi

summary="$(printf 'Project: %s\nAdapter: %s\nPackage source: %s\nIndexed project: %s\nImmutable revision: %s\nOTLP endpoint: %s\nFiles: composer.json, composer.lock (Composer-managed), .env' \
  "$target_dir" "$adapter" "$package_source" "$project_id" "$revision_id" "$otlp_endpoint")"
ui_box "$summary"
printf '\n'
if ! ui_confirm 'Continue with Composer installation and update the target .env?' 'Install and configure' 'Cancel'; then
  echo 'Installation cancelled; no project files were changed.'
  exit 0
fi

if [[ "$package_option" == 'local' ]]; then
  if [[ "$existing_repo" != 'matching' ]]; then
    "$COMPOSER_BIN" --working-dir="$target_dir" repo add "$local_repo_name" "$repo_json"
  fi
  "$COMPOSER_BIN" --working-dir="$target_dir" require "$adapter:$local_version" open-telemetry/exporter-otlp
else
  "$COMPOSER_BIN" --working-dir="$target_dir" require "$adapter:$version_constraint" open-telemetry/exporter-otlp
fi

python3 "$ROOT_DIR/scripts/write-php-otel-env.py" "$env_path" "$service_name" "$project_id" "$revision_id" "$otlp_endpoint"

printf '\n'
ui_success '✓ PHP project is configured for Developer Assistant Runtime telemetry.'
printf 'Revision: %s · Project: %s · Endpoint: %s\n' "$revision_id" "$project_id" "$otlp_endpoint"
ui_note 'Start the Runtime Collector with `just` → API → runtime services, or run: (cd api && docker compose --profile runtime up -d telemetry-clickhouse telemetry-collector-runtime)'
ui_note 'If this PHP runtime uses containers, rebuild/restart its service so Composer packages and .env values load.'
