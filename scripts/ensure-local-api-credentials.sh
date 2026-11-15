#!/usr/bin/env bash
# Make sure the local API has a source-upload token for PROJECT_ID and a download signing secret.
# Missing values are created in api/.env (never printed) and the Python API container is recreated.
#
# Usage: ensure-local-api-credentials.sh PROJECT_ID [--status]
#   --status  only print "ready" or "missing"
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API_DIR="$ROOT_DIR/api"
project_id="${1:?Usage: ensure-local-api-credentials.sh PROJECT_ID [--status]}"

credentials_status() {
  (cd "$API_DIR" && docker compose exec -T python-api python -c '
import json, os, sys
try:
    tokens = json.loads(os.environ.get("TELEMETRY_CI_PROJECT_TOKENS", "{}"))
except (ValueError, TypeError):
    tokens = {}
token = tokens.get(sys.argv[1]) if isinstance(tokens, dict) else None
secret = os.environ.get("DOWNLOAD_TOKEN_SECRET", "")
print("ready" if isinstance(token, str) and len(token) >= 32 and len(secret) >= 32 else "missing")
' "$project_id" 2>/dev/null)
}

if ! status="$(credentials_status)"; then
  echo "The local API container is unavailable. Start it with 'just up-dev', then retry." >&2
  exit 2
fi
if [[ "${2:-}" == --status ]]; then
  echo "$status"
  exit 0
fi
[[ "$status" == ready ]] && exit 0

if ! update="$(python3 "$ROOT_DIR/scripts/configure-local-api-credentials.py" "$API_DIR/.env" "$project_id" 2>&1)"; then
  echo "$update" >&2
  exit 2
fi
echo "$update; secret values remain stored in api/.env. Recreating the Python API container…"
env -u TELEMETRY_CI_PROJECT_TOKENS -u DOWNLOAD_TOKEN_SECRET \
  docker compose -f "$API_DIR/docker-compose.yml" --project-directory "$API_DIR" up -d --no-deps --force-recreate python-api >/dev/null
bash "$ROOT_DIR/scripts/wait-for-local-api.sh" "$API_DIR"
if [[ "$(credentials_status || echo missing)" != ready ]]; then
  echo 'The API container still has missing or invalid local API credentials. Check the Docker Compose environment configuration.' >&2
  exit 2
fi
