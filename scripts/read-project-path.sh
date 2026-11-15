#!/usr/bin/env bash

read_project_path_with_completion() {
  local prompt_text="$1"
  local raw_path parsed_path

  gum style --foreground 252 "$prompt_text" >&2
  gum style --foreground 245 'Type or paste a path, then press Tab for filesystem completion.' >&2
  if ! IFS= read -e -r -p 'Path> ' raw_path; then
    return 1
  fi

  if ! parsed_path="$(python3 -c '
import os, shlex, sys
try:
    parts = shlex.split(sys.argv[1])
except ValueError as error:
    print("Could not parse the path: {}".format(error), file=sys.stderr)
    raise SystemExit(2)
print(os.path.expanduser(" ".join(parts)))
' "$raw_path")"; then
    return 2
  fi

  printf '%s\n' "$parsed_path"
}
