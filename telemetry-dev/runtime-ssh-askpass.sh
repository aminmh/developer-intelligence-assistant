#!/bin/sh
set -eu
if ! mkdir "${DEVASSISTANT_ASKPASS_ONCE_DIR:?}" 2>/dev/null; then
  exit 1
fi
printf '%s' "${DEVASSISTANT_ASKPASS_PASSPHRASE:-}"
