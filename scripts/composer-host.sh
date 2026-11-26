#!/usr/bin/env bash
# Composer for the Runtime library install, run on the host.
#
# The app runs in a container with its own PHP version and extensions, but this runs on the developer's machine,
# where PHP is often newer (for example 8.4 for an app on 8.2) and extensions such as ext-redis may be missing.
# Composer would then refuse to resolve packages the container can run. Platform checks are skipped for the
# commands that resolve packages; the lock file records the project's own platform settings, not the host's.
# The app's own Composer scripts (cache:clear, assets:install...) are meant for its container and can fail or run out
# of memory on the host, so they are skipped here.
set -euo pipefail

composer_bin="${INDEXR_COMPOSER:-composer}"
subcommand=''
for argument in "$@"; do
  case "$argument" in
    -*) ;;
    *) subcommand="$argument"; break ;;
  esac
done

case "$subcommand" in
  require|update|install|remove) exec "$composer_bin" "$@" --ignore-platform-reqs --no-scripts ;;
  *) exec "$composer_bin" "$@" ;;
esac
