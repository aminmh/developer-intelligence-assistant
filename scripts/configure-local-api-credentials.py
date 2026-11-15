#!/usr/bin/env python3
"""Create missing local API secrets without printing their values."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile


PROJECT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


def assignment(lines: list[str], name: str) -> tuple[list[int], str | None]:
    matches = [index for index, line in enumerate(lines) if re.match(rf"^\s*{re.escape(name)}\s*=", line)]
    if len(matches) > 1:
        raise ValueError(f"api/.env has multiple {name} entries; resolve the duplicates first.")
    if not matches:
        return matches, None
    return matches, lines[matches[0]].split("=", maxsplit=1)[1].strip()


def decode_tokens(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    value: str | None = raw
    if value.startswith("'") and value.endswith("'"):
        value = value[1:-1]
    elif value.startswith('"') and value.endswith('"'):
        parsed = json.loads(value)
        if isinstance(parsed, str):
            value = parsed
        else:
            value = None
            decoded = parsed
    if value is not None:
        decoded = json.loads(value.split("#", maxsplit=1)[0].strip())
    if not isinstance(decoded, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in decoded.items()
    ):
        raise ValueError("TELEMETRY_CI_PROJECT_TOKENS in api/.env must be a JSON object of string tokens.")
    return decoded


def decode_secret(raw: str | None) -> str:
    if not raw:
        return ""
    if raw.startswith("'") and raw.endswith("'"):
        return raw[1:-1]
    if raw.startswith('"') and raw.endswith('"'):
        value = json.loads(raw)
        return value if isinstance(value, str) else ""
    return raw.split("#", maxsplit=1)[0].strip()


def write_private_env(path: Path, lines: list[str]) -> None:
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=".env.", dir=str(path.parent))
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write("".join(lines))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError as error:
        raise ValueError(f"Could not securely update api/.env: {error}") from error
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def replace_assignment(lines: list[str], matches: list[int], name: str, value: str) -> None:
    replacement = f"{name}={value}\n"
    if matches:
        lines[matches[0]] = replacement
    else:
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines[-1] += "\n"
        lines.append(replacement)


def configure(path: Path, project_id: str) -> str:
    if not PROJECT_ID.fullmatch(project_id):
        raise ValueError("Invalid project ID.")
    if path.is_symlink():
        raise ValueError("Refusing to update api/.env because it is a symbolic link.")
    try:
        content = path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError as error:
        raise ValueError(f"Could not read api/.env: {error}") from error

    lines = content.splitlines(keepends=True)
    token_matches, token_raw = assignment(lines, "TELEMETRY_CI_PROJECT_TOKENS")
    secret_matches, secret_raw = assignment(lines, "DOWNLOAD_TOKEN_SECRET")
    tokens = decode_tokens(token_raw)

    changes = []
    project_token = tokens.get(project_id)
    if not isinstance(project_token, str) or len(project_token) < 32:
        tokens[project_id] = secrets.token_urlsafe(48)
        replace_assignment(
            lines,
            token_matches,
            "TELEMETRY_CI_PROJECT_TOKENS",
            "'{}'".format(json.dumps(tokens, ensure_ascii=True, separators=(",", ":"))),
        )
        changes.append("project upload token created")

    download_secret = decode_secret(secret_raw)
    if len(download_secret) < 32:
        replace_assignment(lines, secret_matches, "DOWNLOAD_TOKEN_SECRET", secrets.token_urlsafe(48))
        changes.append("download signing secret created")

    if changes:
        write_private_env(path, lines)
        return " and ".join(changes)
    return "credentials already configured"


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: configure-local-api-credentials.py API_ENV_PATH PROJECT_ID", file=sys.stderr)
        return 2
    try:
        message = configure(Path(sys.argv[1]), sys.argv[2])
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(error, file=sys.stderr)
        return 2
    print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
