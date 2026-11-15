"""Private, atomic installer state. Persist data, never executable shell text."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

ROOT = Path(os.environ.get("INSTALL_ROOT", Path(__file__).resolve().parents[2])).resolve()
STATE = ROOT / ".install"
STAGES = ("dependencies", "configuration", "certificates", "images", "build", "models", "services", "verification")


def private_dir(path: Path) -> None:
    if path.is_symlink():
        raise ValueError(f"Refusing symbolic-link directory: {path.name}")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def atomic(path: Path, content: str, mode: int = 0o600) -> None:
    if path.is_symlink():
        raise ValueError(f"Refusing symbolic-link file: {path.name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".installer-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load(path: Path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def save(path: Path, value) -> None:
    atomic(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def env_read(path: Path) -> dict[str, str]:
    values = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        if not match:
            continue
        key, value = match.groups()
        if key in values:
            raise ValueError(f"Duplicate {key} in {path.parent.name}/.env; resolve before installing")
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError(f"Multiline or malformed {key} is unsupported by the installer")
            if value[0] == '"':
                value = value[1:-1].replace('\\"', '"').replace('\\\\', '\\')
            else:
                value = value[1:-1]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        if "\n" in value or "\r" in value:
            raise ValueError(f"Multiline {key} is unsupported")
        # Compose expands dollars even inside double quotes. Refuse ambiguous
        # existing expressions rather than silently changing their meaning.
        if "$" in value:
            raise ValueError(f"Resolve Compose interpolation in {key} before installing")
        values[key] = value
    return values


def env_update(path: Path, updates: dict[str, str]) -> dict[str, str]:
    values = env_read(path)
    original = path.read_text() if path.exists() else ""
    remaining = dict(updates)
    lines = []
    for line in original.splitlines():
        match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if match and match[1] in remaining:
            key = match[1]
            line = key + "=" + quote_env(remaining.pop(key))
        lines.append(line)
    lines += [key + "=" + quote_env(value) for key, value in remaining.items()]
    content = "\n".join(lines) + "\n"
    if content != original:
        backup = STATE / "backups" / (path.parent.name + ".env")
        private_dir(backup.parent)
        if original and not backup.exists():
            atomic(backup, original)
        atomic(path, content)
    else:
        path.chmod(0o600)
    values.update(updates)
    return values


def quote_env(value: str) -> str:
    if any(char in value for char in "\r\n$"):
        raise ValueError("Unsafe environment value")
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def digest(paths: list[Path]) -> str:
    result = hashlib.sha256()
    for path in sorted(paths):
        result.update(str(path.relative_to(ROOT)).encode())
        result.update(path.read_bytes() if path.is_file() else b"<missing>")
    return result.hexdigest()


def fingerprint() -> str:
    files = [STATE / "choices.json", ROOT / "justfile"]
    for folder in ("api", "web"):
        files += [ROOT / folder / name for name in (".env", ".dockerignore", "docker-compose.yml", "docker-compose.override.yml")]
    files += list((ROOT / "scripts/installer").glob("*.py"))
    files += list((ROOT / "scripts/installer").glob("*.sh"))
    files += [ROOT / "scripts/installer/requirements.txt", STATE / "proxy.env", STATE / "Caddyfile"]
    files += list((ROOT / "api").rglob("Dockerfile*")) + list((ROOT / "web").glob("Dockerfile*"))
    files += [ROOT / "web/package-lock.json", ROOT / "web/package.json", ROOT / "api/Python-Service/.dockerignore"]
    files += list((ROOT / "api/Python-Service").glob("requirements*.txt"))
    # Source edits also invalidate build results; exclude mutable data, logs,
    # caches and secrets, which are tracked separately above.
    for directory in ("web/src", "api/Python-Service/app", "api/Python-Service/php", "api/telemetry-dev", "api/analyzer-worker"):
        files += [p for p in (ROOT / directory).rglob("*") if p.is_file()
                  and not any(part in {"__pycache__", ".pytest_cache", "node_modules", "vendor", "logs", "certs"} for part in p.parts)
                  and p.suffix not in {".pyc", ".log", ".db", ".sqlite", ".sqlite3"}]
    return digest(files)


def checkpoint(stage: str, valid: bool = False) -> bool:
    if stage not in STAGES:
        raise ValueError("Unknown stage")
    path = STATE / "checkpoint.json"
    data = load(path, {"stages": {}})
    current = fingerprint()
    if data.get("fingerprint") != current:
        data = {"fingerprint": current, "stages": {}}
    if valid:
        return bool(data["stages"].get(stage))
    data["stages"][stage] = True
    save(path, data)
    return True


def redact(line: str) -> str:
    secrets = []
    for path in (ROOT / "api/.env", ROOT / "web/.env", STATE / "proxy.env"):
        try:
            values = env_read(path)
        except ValueError:
            return "[output suppressed: invalid secret configuration]\n"
        secrets += [value for key, value in values.items()
                    if re.search("SECRET|PASSWORD|TOKEN|API_KEY", key) and value]
    for value in sorted(secrets, key=len, reverse=True):
        line = line.replace(value, "[REDACTED]")
    return re.sub(r"(?i)(authorization|password|secret|api[-_]key)([=: ]+)[^\s,;]+", r"\1\2[REDACTED]", line)
