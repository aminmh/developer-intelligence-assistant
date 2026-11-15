#!/usr/bin/env bash
# Run from a complete downloaded Developer Assistant folder.
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
exec bash "$ROOT/scripts/installer/bootstrap.sh" "$@"
