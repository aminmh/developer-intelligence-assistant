#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo 'Usage: wait-for-local-api.sh API_DIRECTORY' >&2
  exit 2
fi

api_dir="$1"
for ((attempt = 1; attempt <= 60; attempt++)); do
  if health="$(cd "$api_dir" && docker compose exec -T python-api python -c '
import urllib.request
response = urllib.request.urlopen("http://127.0.0.1:9002/health", timeout=3)
print("ready" if response.status == 200 else "not-ready")
' 2>/dev/null)" && [[ "$health" == 'ready' ]]; then
    exit 0
  fi
  sleep 1
done

echo 'The local Python API did not answer its HTTP health check within 60 seconds. Check `just api-logs`, then retry indexing.' >&2
exit 1
