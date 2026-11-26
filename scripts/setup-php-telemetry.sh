#!/usr/bin/env bash
# One-command Runtime telemetry setup for a PHP project: install, commit, index, configure, deploy, verify.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API_DIR="$ROOT_DIR/api"
COMPOSER_BIN="${COMPOSER_BIN:-composer}"
source "$ROOT_DIR/scripts/lib-cli.sh"

usage() {
  cat <<'USAGE'
Usage: just php-otel-setup PROJECT_PATH [options]

Set up Runtime telemetry for a PHP project in one step. Everything is detected; the only
question is a final confirmation of the plan, which --yes skips.

  1. Start the Runtime services (local ClickHouse, Collector, host agent) if they are not running.
  2. Install the Indexr adapter (Laravel, Symfony or core) with Composer, unless already installed.
  3. Commit composer.json and composer.lock when they are the only changes in the checkout.
  4. Upload the committed source and write the telemetry settings into the project's .env.
  5. For a Docker Compose project: label its PHP service (and its MySQL, MariaDB or PostgreSQL services) for the host agent (through a generated,
     uncommitted compose.indexr.yaml), join the Runtime network and restart it at this commit.
     Then write the method hook manifest (indexer:export --write-hooks) from the running code.
  6. Wait until the revision is indexed and ready.

Options:
  --project-id ID         Runtime project ID (default: the folder name)
  --adapter NAME          laravel | symfony | core (default: detected from composer.json)
  --library-path PATH     Local indexr-otel-library checkout (default: ../indexr-otel-library if present)
  --version CONSTRAINT    Install the published package with this constraint instead of a local checkout
  --service NAME          Compose service that runs PHP (default: detected)
  --no-compose            The app does not run in Docker Compose; send spans to http://127.0.0.1:24318
  --no-commit             Stop instead of committing the Composer changes
  --no-restart            Do not restart the Compose project (restart it yourself with the printed command)
  -y, --yes               Do not ask for confirmation
  --non-interactive       Never prompt (implies --yes is required for changes)
  -h, --help              Show this help

Example:
  just php-otel-setup ../pizza-builder --yes
USAGE
}

target_input=''
project_id=''
adapter=''
library_option=''
version_constraint=''
compose_service=''
use_compose=1
allow_commit=1
allow_restart=1
assume_yes=0
non_interactive=0
need_value() { [[ $# -ge 2 && -n "$2" ]] || { echo "Missing value for $1." >&2; exit 2; }; }
while [[ $# -gt 0 ]]; do
  case "$1" in
    --project-id) need_value "$@"; project_id="$2"; shift 2 ;;
    --adapter) need_value "$@"; adapter="$2"; shift 2 ;;
    --library-path) need_value "$@"; library_option="$2"; shift 2 ;;
    --version) need_value "$@"; version_constraint="$2"; shift 2 ;;
    --service) need_value "$@"; compose_service="$2"; shift 2 ;;
    --no-compose) use_compose=0; shift ;;
    --no-commit) allow_commit=0; shift ;;
    --no-restart) allow_restart=0; shift ;;
    -y|--yes) assume_yes=1; shift ;;
    --non-interactive) non_interactive=1; shift ;;
    -h|--help) usage; exit 0 ;;
    -*) echo "Unknown option: $1 (see --help)" >&2; exit 2 ;;
    *) [[ -z "$target_input" ]] || { echo "Only one project path is accepted." >&2; exit 2; }; target_input="$1"; shift ;;
  esac
done
if [[ -n "$adapter" && ! "$adapter" =~ ^(laravel|symfony|core)$ ]]; then
  echo '--adapter must be laravel, symfony, or core.' >&2
  exit 2
fi
if [[ -n "$library_option" && -n "$version_constraint" ]]; then
  echo 'Choose either --library-path or --version, not both.' >&2
  exit 2
fi

ui_init "$non_interactive" "$assume_yes"
ui_require_tools docker git python3 "$COMPOSER_BIN"
if [[ -z "$target_input" ]]; then
  ui_is_interactive || { usage >&2; exit 2; }
  source "$ROOT_DIR/scripts/read-project-path.sh"
  target_input="$(read_project_path_with_completion 'PHP project path (absolute or relative)')" || { echo 'Setup cancelled.'; exit 0; }
fi
in_api() { (cd "$API_DIR" && docker compose exec -T python-api "$@"); }
step() { printf '\n'; ui_info "▸ $*"; }

ui_title 'Runtime telemetry setup'

# ---- Project and defaults -------------------------------------------------------------------
if ! target_dir="$(cd "$target_input" 2>/dev/null && pwd -P)"; then
  ui_error "Project directory does not exist: $target_input"; exit 2
fi
[[ -f "$target_dir/composer.json" ]] || { ui_error "No composer.json in $target_dir"; exit 2; }
[[ ! -L "$target_dir/.env" ]] || { ui_error 'The project .env is a symlink; refusing to modify a file outside the project.'; exit 2; }
git -C "$target_dir" rev-parse --verify HEAD >/dev/null 2>&1 || { ui_error 'The project must be a Git checkout with at least one commit.'; exit 2; }

slug="$(basename "$target_dir" | LC_ALL=C tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9._-]+/-/g; s/^[^a-z0-9]+//; s/[-._]+$//')"
service_name="${slug:-php-app}"

if [[ -z "$adapter" ]]; then
  adapter="$(python3 - "$target_dir/composer.json" <<'PY'
import json, sys
package = json.load(open(sys.argv[1], encoding="utf-8"))
deps = {**package.get("require", {}), **package.get("require-dev", {})}
print("laravel" if "laravel/framework" in deps else "symfony" if "symfony/framework-bundle" in deps else "core")
PY
)"
fi
package="indexr/indexer-otel-$adapter"
# A published install (--version) uses the Packagist names; a local checkout keeps the monorepo's names.
[[ -n "$version_constraint" ]] && package="aminmh/developer-assistant-otel-$adapter"

library_dir=''
if [[ -z "$version_constraint" ]]; then
  library_input="${library_option:-$ROOT_DIR/../indexr-otel-library}"
  if library_dir="$(cd "$library_input" 2>/dev/null && pwd -P)" && [[ -f "$library_dir/packages/core/composer.json" ]]; then
    :
  else
    ui_error "No indexr-otel-library checkout at $library_input. Pass --library-path, or --version to use the published package."
    exit 2
  fi
fi

installed="$(python3 - "$target_dir/composer.json" "$adapter" <<'PY'
import json, sys
package = json.load(open(sys.argv[1], encoding="utf-8"))
names = (f"indexr/indexer-otel-{sys.argv[2]}", f"aminmh/developer-assistant-otel-{sys.argv[2]}")
print("yes" if any(name in package.get("require", {}) for name in names) else "no")
PY
)"

compose_json='{"compose": false}'
if [[ "$use_compose" == 1 ]]; then
  compose_args=(detect "$target_dir")
  [[ -n "$compose_service" ]] && compose_args+=(--service "$compose_service")
  compose_json="$(python3 "$ROOT_DIR/scripts/php-compose-telemetry.py" "${compose_args[@]}")"
fi
json_field() { python3 -c 'import json,sys; v=json.loads(sys.argv[1]).get(sys.argv[2]); print("" if v is None else str(v).lower() if isinstance(v,bool) else v)' "$compose_json" "$1"; }
is_compose="$(json_field compose)"
if [[ "$is_compose" == true ]]; then
  compose_service="$(json_field service)"
  if [[ -z "$compose_service" ]]; then
    ui_error "Could not tell which Compose service runs PHP ($(json_field reason)). Pass --service NAME, or --no-compose."
    exit 2
  fi
  labelled="$(json_field labelled)"
  # MySQL, MariaDB and PostgreSQL services must report too, or Database Schema cannot load them.
  pending_databases=()
  while IFS= read -r name; do
    [[ -n "$name" ]] && pending_databases+=("$name")
  done < <(python3 -c 'import json,sys; [print(i["service"]) for i in json.loads(sys.argv[1]).get("databases", []) if not i["labelled"]]' "$compose_json")
  label_project="$(json_field label_project)"
  if [[ -n "$label_project" ]]; then
    # Existing labels already name the Runtime project; spans must use the same ID.
    if [[ -n "$project_id" && "$project_id" != "$label_project" ]]; then
      ui_error "Compose service '$compose_service' is labelled for project '$label_project', not '$project_id'. Drop --project-id or change the label."
      exit 2
    fi
    project_id="$label_project"
  fi
  otlp_endpoint='http://telemetry-collector-runtime:4318'
else
  labelled=false
  environment=development
  otlp_endpoint='http://127.0.0.1:24318'
fi
[[ -n "$project_id" ]] || project_id="$slug"
valid_project_id "$project_id" || { ui_error "Invalid project ID '$project_id'; pass --project-id."; exit 2; }

# Changes other than the ones this setup makes would make the indexed source differ from what runs.
# The method hook manifest and its refresh stamp are generated by the running app, not source.
generated=(':(exclude,glob)**/indexer-otel-hooks.json' ':(exclude,glob)**/indexer-otel-hooks.json.*')
configured_hooks="$(sed -nE 's/^[[:space:]]*INDEXER_OTEL_HOOKS_FILE[[:space:]]*=[[:space:]]*["'"'"']?([^"'"'"']*)["'"'"']?[[:space:]]*$/\1/p' "$target_dir/.env" 2>/dev/null | tail -1)"
if [[ -n "$configured_hooks" && "$configured_hooks" != /* && "$configured_hooks" != *..* ]]; then
  generated+=(":(exclude,literal)$configured_hooks" ":(exclude,glob)$configured_hooks.*")
fi
other_changes="$(git -C "$target_dir" status --porcelain --untracked-files=normal -- . ':!.env' ':!composer.json' ':!composer.lock' ':!compose.indexr.yaml' "${generated[@]}")"
if [[ -n "$other_changes" ]]; then
  ui_error 'The checkout has uncommitted changes. Commit or stash them first, so the indexed source matches what runs:'
  printf '%s\n' "$other_changes" >&2
  exit 2
fi

plan="Project: $target_dir
Runtime project ID: $project_id
Adapter: $package ($([[ "$installed" == yes ]] && echo 'already installed' || { [[ -n "$library_dir" ]] && echo "install from $library_dir" || echo "install $version_constraint"; }))
Collector endpoint: $otlp_endpoint"
if [[ "$is_compose" == true ]]; then
  plan+="
Compose service: $compose_service ($([[ "$labelled" == true ]] && echo 'already labelled' || echo 'labels and network added in compose.indexr.yaml'))
Database services: $([[ ${#pending_databases[@]} -gt 0 ]] && echo "${pending_databases[*]} (labelled in compose.indexr.yaml; recreated once)" || echo 'none to add')
Restart: $([[ "$allow_restart" == 1 ]] && echo 'docker compose up -d at the committed revision' || echo 'skipped (--no-restart)')"
fi
plan+="
Commits: composer.json and composer.lock only, if Composer changes them
Files written: .env, the method hook manifest$([[ "$is_compose" == true && ( "$labelled" != true || ${#pending_databases[@]} -gt 0 ) ]] && echo ', compose.indexr.yaml (not committed)' || true)"
ui_box "$plan"
if ! ui_confirm 'Set up Runtime telemetry with this plan?' 'Set up' 'Cancel'; then
  echo 'Setup cancelled; nothing was changed.'; exit 0
fi

# ---- 1. Runtime services --------------------------------------------------------------------
step 'Checking the Developer Assistant services'
if ! (cd "$API_DIR" && docker compose ps --status running --services 2>/dev/null | grep -qx python-api); then
  ui_note 'The Developer Assistant API is not running; starting it (just api-up).'
  just --justfile "$ROOT_DIR/justfile" api-up >/dev/null \
    || { ui_error 'Could not start the API services. Run `just up` and check its output, then rerun.'; exit 2; }
fi
if ! bash "$ROOT_DIR/scripts/wait-for-local-api.sh" "$API_DIR" >/dev/null 2>&1; then
  ui_error 'The Developer Assistant API did not become healthy. Check `just api-logs`, then rerun.'
  exit 2
fi
mode="$(in_api python -c 'from telemetry.runtime_storage import get_storage_target; print(get_storage_target()["mode"])')"
if [[ "$mode" == disabled ]]; then
  ui_note 'Runtime storage is disabled; switching it to Local.'
  in_api python -c 'from telemetry.runtime_storage import set_storage_mode; set_storage_mode("local")' >/dev/null
fi
python3 "$API_DIR/telemetry-dev/runtime_stack.py" start >/dev/null
if [[ "$is_compose" == true ]]; then
  # The API accepts deployment reports only for projects and environments the host agent is allowed to report.
  authorization="$(python3 "$ROOT_DIR/scripts/authorize-host-project.py" "$project_id" "$environment")"
  if [[ "$authorization" == no-agent ]]; then
    ui_error 'No local Runtime host agent is configured yet. Run `just runtime-setup` once (it creates the agent), then rerun this command.'
    exit 2
  fi
  if [[ "$authorization" == added ]]; then
    ui_note "Allowed the host agent to report '$project_id' ($environment); reloading the API."
    env -u TELEMETRY_CI_PROJECT_TOKENS -u DOWNLOAD_TOKEN_SECRET -u TELEMETRY_HOST_SCOPES \
      docker compose -f "$API_DIR/docker-compose.yml" --project-directory "$API_DIR" up -d --no-deps --force-recreate python-api >/dev/null
    bash "$ROOT_DIR/scripts/wait-for-local-api.sh" "$API_DIR" >/dev/null
  fi
  python3 "$API_DIR/telemetry-dev/runtime_setup.py" ensure-host-services >/dev/null \
    || ui_note 'The host agent did not start; run `just runtime-status`. Indexing waits for it to report the deployment.'
fi
bash "$ROOT_DIR/scripts/ensure-local-api-credentials.sh" "$project_id"

# ---- 2. Composer ----------------------------------------------------------------------------
if [[ "$installed" != yes ]]; then
  step "Installing $package"
  if [[ -n "$library_dir" ]]; then
    repo_json="$(python3 -c 'import json,sys; v="0.1.0"; print(json.dumps({"type":"path","url":sys.argv[1]+"/packages/*","options":{"symlink":True,"versions":{f"indexr/indexer-otel-{n}":v for n in ("core","laravel","symfony")}}},separators=(",",":")))' "$library_dir")"
    "$COMPOSER_BIN" --working-dir="$target_dir" repo add indexr-otel-local "$repo_json"
    "$COMPOSER_BIN" --working-dir="$target_dir" require --no-interaction "$package:0.1.0" open-telemetry/exporter-otlp
  else
    "$COMPOSER_BIN" --working-dir="$target_dir" require --no-interaction "$package:$version_constraint" open-telemetry/exporter-otlp
  fi
fi

# ---- 3. Commit the dependency change --------------------------------------------------------
if [[ -n "$(git -C "$target_dir" status --porcelain -- composer.json composer.lock)" ]]; then
  if [[ "$allow_commit" != 1 ]]; then
    ui_error 'Composer changed composer.json/composer.lock. Commit them, then rerun (or drop --no-commit).'
    exit 2
  fi
  step 'Committing composer.json and composer.lock'
  git -C "$target_dir" add -- composer.json composer.lock
  git -C "$target_dir" commit -q -m 'chore: add Indexr runtime telemetry' -- composer.json composer.lock
  ui_note "Committed on $(git -C "$target_dir" branch --show-current || echo 'detached HEAD'): $(git -C "$target_dir" log -1 --format='%h %s')"
fi
commit_sha="$(git -C "$target_dir" rev-parse --verify HEAD)"

# ---- 4. Upload the source and configure the app ---------------------------------------------
step "Uploading source at ${commit_sha:0:12}"
# Starting the host services or loading new credentials can recreate the API container.
bash "$ROOT_DIR/scripts/wait-for-local-api.sh" "$API_DIR" >/dev/null
repo_root="$(git -C "$target_dir" rev-parse --show-toplevel)"
upload="$(git -C "$repo_root" archive --format=tar "$commit_sha" \
  | python3 "$ROOT_DIR/scripts/package-git-source-zip.py" \
  | (cd "$API_DIR" && docker compose exec -T python-api python -c "$(cat "$ROOT_DIR/scripts/upload-php-source-snapshot.py")" "$project_id" "$commit_sha" --no-wait))"
revision_id="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("revision_id") or "")' "$upload")"
state="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("state") or "")' "$upload")"
[[ "$revision_id" =~ ^rev_[a-f0-9]{32}$ ]] || { ui_error "The API returned no revision ID: $upload"; exit 2; }
ui_note "Revision $revision_id ($state)"

# The method hook manifest lists the app methods whose calls are counted per request (Method activity).
case "$adapter" in
  laravel) hooks_default='storage/indexer-otel-hooks.json'; console=(php artisan) ;;
  symfony) hooks_default='var/indexer-otel-hooks.json'; console=(php bin/console) ;;
  *) hooks_default=''; console=() ;;
esac
env_args=("$target_dir/.env" "$service_name" "$project_id" "$revision_id" "$otlp_endpoint")
[[ -n "$hooks_default" ]] && env_args+=(--hooks-file "$hooks_default")
python3 "$ROOT_DIR/scripts/write-php-otel-env.py" "${env_args[@]}"

# Regenerates the manifest from the running code, so methods added since the last run are counted.
write_hook_manifest() {
  [[ ${#console[@]} -gt 0 ]] || return 0
  local result
  if [[ "$is_compose" == true ]]; then
    result="$(cd "$target_dir" && docker compose exec -T "$compose_service" "${console[@]}" indexer:export --write-hooks 2>&1)" || result="failed: $result"
  elif command -v php >/dev/null 2>&1; then
    result="$(cd "$target_dir" && "${console[@]}" indexer:export --write-hooks 2>&1)" || result="failed: $result"
  else
    result='failed: php is not on PATH'
  fi
  if [[ "$result" == failed:* ]]; then
    ui_note "Could not write the method hook manifest, so Method activity stays empty (${result:8:200}). Run \`${console[*]} indexer:export --write-hooks\` in the app."
  else
    ui_note "$(printf '%s' "$result" | tail -1)"
  fi
}

# ---- 5. Deploy ------------------------------------------------------------------------------
restart_command="GIT_COMMIT_SHA=\$(git rev-parse HEAD) docker compose up -d"
if [[ "$is_compose" == true ]]; then
  if [[ "$labelled" != true || ${#pending_databases[@]} -gt 0 ]]; then
    step "Labelling Compose service '$compose_service' and databases (${pending_databases[*]:-none}) for the host agent"
    write_args=(write "$target_dir" --service "$compose_service" --project-id "$project_id")
    [[ -n "$library_dir" ]] && write_args+=(--library-path "$library_dir")
    if [[ ${#pending_databases[@]} -gt 0 ]]; then
      database_list="$(IFS=,; echo "${pending_databases[*]}")"
      write_args+=(--databases "$database_list")
    fi
    python3 "$ROOT_DIR/scripts/php-compose-telemetry.py" "${write_args[@]}" >/dev/null
  fi
  if [[ "$allow_restart" == 1 ]]; then
    step "Restarting the Compose project at ${commit_sha:0:12}"
    (cd "$target_dir" && GIT_COMMIT_SHA="$commit_sha" docker compose up -d)
    if [[ "$adapter" == laravel && -f "$target_dir/bootstrap/cache/config.php" ]]; then
      # A cached config would ignore the new .env values.
      (cd "$target_dir" && docker compose exec -T "$compose_service" php artisan config:clear >/dev/null) \
        || ui_note 'Could not clear the Laravel config cache; run `php artisan config:clear` in the container.'
    fi
    step 'Writing the method hook manifest'
    write_hook_manifest
  fi
fi

# ---- 6. Wait for the indexed revision -------------------------------------------------------
if [[ "$is_compose" == true && "$allow_restart" == 1 ]] || [[ "$state" == ready ]]; then
  step 'Waiting for the host agent to report the deployment and for indexing to finish'
  if ! in_api python - "$project_id" "$commit_sha" <<'PY'
import sys, time
from telemetry.registry import default_registry
project_id, commit_sha = sys.argv[1:3]
registry, last, deadline = default_registry(), None, time.monotonic() + 600
while time.monotonic() < deadline:
    snapshot = registry.get(project_id, commit_sha) or {}
    state = snapshot.get("state")
    if state != last:
        print(f"  state: {state}", flush=True)
        last = state
    if state == "ready":
        sys.exit(0)
    if state == "failed":
        print(f"  indexing failed: {snapshot.get('error_code')}", flush=True)
        sys.exit(1)
    time.sleep(3)
print("  still not ready after 10 minutes", flush=True)
sys.exit(1)
PY
  then
    ui_error "The revision is not ready yet. Check that the app's container runs with the telemetry labels (\`just runtime-status\`), then rerun this command; it is safe to repeat."
    exit 3
  fi
  printf '\n'
  ui_success "✓ Runtime telemetry is set up for '$project_id' at revision $revision_id."
  ui_note 'Open Developer Assistant → Runtime and send a few requests to the app to see them.'
else
  printf '\n'
  ui_success "✓ Installed and configured for revision $revision_id."
  if [[ "$is_compose" == true ]]; then
    ui_note "Start the app at this commit so the host agent can report it: $restart_command"
    [[ ${#console[@]} -gt 0 ]] && ui_note "Then write the method hook manifest in the container: ${console[*]} indexer:export --write-hooks" || true
  else
    write_hook_manifest
    ui_note "Restart the PHP process so it loads .env. Indexing finishes once a deployment of this commit is reported."
  fi
fi
ui_note 'After new commits, rerun this command to index the new revision and restart at it.'
