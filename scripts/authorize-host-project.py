#!/usr/bin/env python3
"""Allow the local Runtime host agent to report deployments of PROJECT_ID in ENVIRONMENT.

Usage: authorize-host-project.py PROJECT_ID ENVIRONMENT
Prints "added" (api/.env changed; recreate the Python API container), "present", or "no-agent"
(run `just runtime-setup` once to create the local host agent).
"""
from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api" / "telemetry-dev"))
import runtime_setup  # noqa: E402  (reuses the scope format `just runtime-setup` writes)


def main(argv: list[str]) -> int:
    if len(argv) != 3 or not runtime_setup.SLUG.fullmatch(argv[1]) or not runtime_setup.SLUG.fullmatch(argv[2]):
        print(__doc__, file=sys.stderr)
        return 2
    if not (runtime_setup.CERT_DIR / "agent" / "host.crt").is_file():
        print("no-agent")
        return 0
    before, after = runtime_setup.update_env_scope(runtime_setup.cert_fingerprint(), argv[1], argv[2])
    if before == after:
        print("present")
        return 0
    runtime_setup.write_text_atomic(runtime_setup.ENV_FILE, after)
    print("added")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
