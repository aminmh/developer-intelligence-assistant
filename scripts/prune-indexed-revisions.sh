#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT_DIR/scripts/lib-cli.sh"

usage() {
  cat <<'USAGE'
Usage: just php-otel-prune --project-id ID [options]

Remove indexed revisions of one Runtime project: their source archives, graph and vector data,
assessment reports, and revision-scoped telemetry. One revision is kept. A revision that a
running deployment still uses, or that a job is working on, is refused and left in place.

Options:
  --project-id ID     Runtime project whose revisions are pruned (exact, case-sensitive)
  --keep REVISION     Revision to keep (default: the most recently updated ready revision)
  --dry-run           List what would be removed without removing anything
  -y, --yes           Remove without a confirmation prompt
  --non-interactive   Never prompt; removal then requires --yes
  -h, --help          Show this help

Example:
  just php-otel-prune --project-id pizza --dry-run
USAGE
}

project_id=''
keep_revision=''
dry_run=0
assume_yes=0
non_interactive=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --project-id) [[ $# -ge 2 ]] || { echo 'Missing value for --project-id.' >&2; exit 2; }; project_id="$2"; shift 2 ;;
    --keep) [[ $# -ge 2 ]] || { echo 'Missing value for --keep.' >&2; exit 2; }; keep_revision="$2"; shift 2 ;;
    --dry-run) dry_run=1; shift ;;
    -y|--yes) assume_yes=1; shift ;;
    --non-interactive) non_interactive=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
done
if [[ -z "$project_id" ]] || ! valid_project_id "$project_id"; then
  echo 'Pass a valid --project-id (see --help).' >&2
  exit 2
fi
if [[ -n "$keep_revision" && ! "$keep_revision" =~ ^rev_[a-f0-9]{32}$ ]]; then
  echo '--keep must be a revision ID (rev_ followed by 32 hex characters).' >&2
  exit 2
fi

ui_init "$non_interactive" "$assume_yes"
ui_require_tools docker

if ! bash "$ROOT_DIR/scripts/wait-for-local-api.sh" "$ROOT_DIR/api"; then
  exit 2
fi

# Lists "<revision_id>\t<state>\t<source>\t<updated_at>" for the project, newest first.
if ! revisions="$(cd "$ROOT_DIR/api" && docker compose exec -T python-api python -c '
import sys
from telemetry.registry import default_registry
with default_registry()._connect() as db:
    rows = db.execute(
        "SELECT revision_id, state, commit_sha, updated_at FROM snapshots WHERE project_id=? AND revision_id IS NOT NULL ORDER BY updated_at DESC",
        (sys.argv[1],),
    ).fetchall()
for revision_id, state, source, updated_at in rows:
    print("\t".join(str(value) for value in (revision_id, state, source, updated_at)))
' "$project_id" </dev/null)"; then
  ui_error 'Could not read indexed revisions from the local API.'
  exit 2
fi
if [[ -z "$revisions" ]]; then
  ui_note "Project $project_id has no indexed revisions."
  exit 0
fi

if [[ -z "$keep_revision" ]]; then
  keep_revision="$(printf '%s\n' "$revisions" | awk -F'\t' '$2 == "ready" { print $1; exit }')"
  if [[ -z "$keep_revision" ]]; then
    ui_error "Project $project_id has no ready revision to keep. Pass --keep explicitly."
    exit 2
  fi
elif ! printf '%s\n' "$revisions" | awk -F'\t' -v keep="$keep_revision" '$1 == keep { found = 1 } END { exit !found }'; then
  ui_error "Revision $keep_revision does not belong to project $project_id."
  exit 2
fi

removals="$(printf '%s\n' "$revisions" | awk -F'\t' -v keep="$keep_revision" '$1 != keep')"
ui_info "Project: $project_id"
ui_info "Keep:    $keep_revision"
if [[ -z "$removals" ]]; then
  ui_success 'Nothing to remove: this is the only indexed revision.'
  exit 0
fi
ui_info 'Remove:'
printf '%s\n' "$removals" | awk -F'\t' '{ printf "  %s  %-9s source %.16s  updated %s\n", $1, $2, $3, $4 }'
if [[ "$dry_run" == 1 ]]; then
  ui_note 'Dry run: nothing was removed.'
  exit 0
fi
if ! ui_confirm "Permanently remove $(printf '%s\n' "$removals" | wc -l | tr -d ' ') revision(s) and their indexed data?" 'Remove' 'Cancel'; then
  echo 'Nothing was removed.'
  exit 0
fi

failed=0
while IFS=$'\t' read -r revision_id _state _source _updated; do
  if result="$(cd "$ROOT_DIR/api" && docker compose exec -T python-api python -c '
import sys
from code_quality.revision_deletion import RevisionBusy, RevisionStorageUnavailable, remove_revision
try:
    print("removed" if remove_revision(sys.argv[1], sys.argv[2]) else "not found")
except (RevisionBusy, RevisionStorageUnavailable) as exc:
    print(f"refused: {exc}")
    raise SystemExit(3)
' "$project_id" "$revision_id" </dev/null 2>&1)"; then
    ui_success "  $revision_id: $result"
  else
    ui_error "  $revision_id: ${result:-removal failed}"
    failed=1
  fi
done <<< "$removals"
exit "$failed"
