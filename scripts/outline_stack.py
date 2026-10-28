#!/usr/bin/env python3
"""Manage the opt-in local Outline Compose profile without exposing secrets."""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
import time
from pathlib import Path


WEB_DIR = Path(__file__).resolve().parents[1]
ENV_FILE = WEB_DIR / ".env"
COMPOSE = ["docker", "compose"]
OUTLINE_SERVICES = ["outline", "outline-postgres", "outline-redis"]
SECRET_KEYS = {
    "OUTLINE_SECRET_KEY",
    "OUTLINE_UTILS_SECRET",
    "OUTLINE_DATABASE_PASSWORD",
    "OUTLINE_OIDC_CLIENT_SECRET",
}
PLACEHOLDER_VALUES = {"", "change-me", "generate-a-random-secret"}


def run(
    command: list[str], *, check: bool = True, capture: bool = False
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=WEB_DIR,
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def _value(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    key, value = stripped.split("=", 1)
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return key.strip(), value


def ensure_local_secrets() -> None:
    if not ENV_FILE.exists():
        raise RuntimeError("web/.env is missing; copy web/.env.example to web/.env first.")

    original_mode = ENV_FILE.stat().st_mode & 0o777
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    found: set[str] = set()
    updated: list[str] = []

    for line in lines:
        parsed = _value(line)
        if parsed is None or parsed[0] not in SECRET_KEYS:
            updated.append(line)
            continue

        key, current = parsed
        found.add(key)
        if current.lower() in PLACEHOLDER_VALUES or current.lower().startswith(
            ("generate-", "generate_")
        ):
            current = secrets.token_hex(32)
        updated.append(f'{key}="{current}"')

    for key in sorted(SECRET_KEYS - found):
        updated.append(f'{key}="{secrets.token_hex(32)}"')

    if updated != lines:
        ENV_FILE.write_text("\n".join(updated) + "\n", encoding="utf-8")
        os.chmod(ENV_FILE, original_mode & 0o600)


def ensure_network() -> None:
    inspected = run(
        ["docker", "network", "inspect", "devassistant_net"],
        check=False,
        capture=True,
    )
    if inspected.returncode != 0:
        run(["docker", "network", "create", "devassistant_net"])


def provision_keycloak_client() -> None:
    command = COMPOSE + [
        "--profile",
        "local-auth",
        "--profile",
        "outline",
        "exec",
        "-T",
        "keycloak",
        "/bin/sh",
        "/opt/keycloak/realm-template/ensure-outline-client.sh",
    ]
    for attempt in range(60):
        result = run(command, check=False, capture=True)
        if result.returncode == 0:
            print("Local Keycloak OIDC client configured.")
            return
        if attempt < 59:
            time.sleep(2)

    raise RuntimeError(
        "Could not configure the Outline OIDC client in Keycloak. "
        "Check 'just outline-logs' and the Keycloak service, then retry."
    )


def up() -> None:
    ensure_local_secrets()
    ensure_network()
    run(
        COMPOSE
        + ["--profile", "local-auth", "--profile", "outline", "up", "-d", "keycloak"]
    )
    provision_keycloak_client()
    run(COMPOSE + ["--profile", "outline", "up", "-d", "outline"])
    print("Outline is starting at http://localhost:3020")


def down() -> None:
    existing = run(
        COMPOSE
        + ["--profile", "outline", "ps", "--all", "--quiet", *OUTLINE_SERVICES],
        capture=True,
    )
    if not existing.stdout.strip():
        print("No Outline containers are running.")
        return

    run(COMPOSE + ["--profile", "outline", "stop", *OUTLINE_SERVICES])
    run(COMPOSE + ["--profile", "outline", "rm", "--force", *OUTLINE_SERVICES])
    print(
        "Outline containers removed; PostgreSQL and uploaded-file volumes were preserved."
    )


def status() -> None:
    run(COMPOSE + ["--profile", "outline", "ps", *OUTLINE_SERVICES])


def logs() -> None:
    run(COMPOSE + ["--profile", "outline", "logs", "--follow", "outline"])


def main() -> int:
    actions = {"up": up, "down": down, "status": status, "logs": logs}
    if len(sys.argv) != 2 or sys.argv[1] not in actions:
        print("Usage: outline_stack.py {up|down|status|logs}", file=sys.stderr)
        return 2
    try:
        actions[sys.argv[1]]()
    except (OSError, subprocess.CalledProcessError, RuntimeError) as error:
        print(f"Outline command failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
