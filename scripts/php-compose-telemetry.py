#!/usr/bin/env python3
"""Find a Compose project's PHP service and wire it for Runtime telemetry without editing its files.

Usage:
  php-compose-telemetry.py detect PROJECT_DIR [--service NAME]
  php-compose-telemetry.py write PROJECT_DIR --service NAME --project-id ID [--library-path PATH]
  php-compose-telemetry.py extension-mounts PROJECT_DIR --service NAME --so FILE --ini FILE --extension-dir DIR --ini-dir DIR
                                 [--databases NAME,NAME]

``detect`` prints JSON: {"compose": bool, "services": [...], "service": name|null,
"labelled": bool, "reason": str}. ``write`` creates ``compose.indexr.yaml`` with the host-agent
labels, the Runtime network and (for a local library checkout) a read-only mount of that checkout
at the same path, then points ``COMPOSE_FILE`` in the project's ``.env`` at it. The project's own
Compose files and Git history stay untouched; the override is excluded through ``.git/info/exclude``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys

OVERRIDE = "compose.indexr.yaml"
BASE_FILES = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
OVERRIDE_FILES = ("compose.override.yaml", "compose.override.yml", "docker-compose.override.yaml", "docker-compose.override.yml")
RUNTIME_NETWORK = "api_telemetry_runtime"
LABEL_PREFIX = "com.devassistant.telemetry."
PHP_NAMES = {"php", "app", "php-fpm", "fpm", "laravel", "symfony", "web", "api", "backend"}
# Labelled only so the host agent reports the container; the schema loader needs it in Runtime.
# A fixed commit keeps Compose from recreating the database each time the app is deployed.
DATABASE_COMMIT = "0" * 40
DATABASE_IMAGE = re.compile(r"(?:^|[/:_-])(mysql|mariadb|percona|postgres(?:ql)?)(?:[:@/_-]|$)", re.IGNORECASE)
DATABASE_TOOL = re.compile(r"admin|exporter|proxy|pgbouncer|backup|migrat", re.IGNORECASE)
NOT_PHP = re.compile(r"phpmyadmin|phppgadmin|adminer|mysql|mariadb|postgres|redis|nginx|caddy|httpd|traefik|mailhog|mailpit")


def compose_config(project: Path, files: list[str] | None = None) -> dict | None:
    if not any((project / name).is_file() for name in BASE_FILES):
        return None
    command = ["docker", "compose"]
    for name in files or []:
        command += ["-f", name]
    env = dict(os.environ)
    if files:
        env.pop("COMPOSE_FILE", None)
    result = subprocess.run(command + ["config", "--format", "json"], cwd=project, env=env,
                            capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise SystemExit("docker compose config failed: " + result.stderr.strip()[:500])
    return json.loads(result.stdout)


def php_score(project: Path, name: str, service: dict) -> int:
    image = str(service.get("image", ""))
    if NOT_PHP.search(name.lower()) or NOT_PHP.search(image.lower()):
        return 0
    score = 0
    if name.lower() in PHP_NAMES:
        score += 2
    if re.search(r"(^|/)php[:@-]|php-fpm|laravel|symfony|frankenphp|roadrunner", image.lower()):
        score += 3
    build = service.get("build")
    if isinstance(build, dict):
        context = Path(str(build.get("context", project)))
        dockerfile = context / str(build.get("dockerfile", "Dockerfile"))
        try:
            text = dockerfile.read_text(encoding="utf-8", errors="replace")[:20000]
        except OSError:
            text = ""
        if re.search(r"^\s*FROM\s+\S*(php|frankenphp)\S*", text, re.IGNORECASE | re.MULTILINE):
            score += 4
    for volume in service.get("volumes", []) or []:
        if isinstance(volume, dict) and volume.get("type") == "bind":
            try:
                if Path(str(volume.get("source", ""))).resolve() == project.resolve():
                    score += 2
            except OSError:
                pass
    return score


def database_engine(name: str, service: dict) -> str | None:
    """Engine of a Compose database server, from its image; admin UIs and sidecars are not databases."""
    image = str(service.get("image", ""))
    if DATABASE_TOOL.search(name) or DATABASE_TOOL.search(image):
        return None
    match = DATABASE_IMAGE.search(image)
    if not match:
        return None
    engine = match.group(1).lower()
    return "postgresql" if engine.startswith("postgres") else "mariadb" if engine == "mariadb" else "mysql"


def is_labelled(spec: dict) -> bool:
    return str((spec.get("labels") or {}).get(LABEL_PREFIX + "enabled", "")).lower() == "true"


def detect(project: Path, requested: str | None) -> dict:
    config = compose_config(project)
    if config is None:
        return {"compose": False, "services": [], "service": None, "labelled": False, "reason": "no Compose file"}
    services = config.get("services", {})
    names = sorted(services)
    if requested:
        if requested not in services:
            raise SystemExit(f"Compose service '{requested}' does not exist. Services: {', '.join(names)}")
        chosen, reason = requested, "chosen with --service"
    else:
        scored = sorted(((php_score(project, name, spec), name) for name, spec in services.items()), reverse=True)
        best = [name for score, name in scored if score and score == scored[0][0]]
        chosen = best[0] if len(best) == 1 else None
        reason = "detected" if chosen else ("several PHP candidates: " + ", ".join(best) if best else "no PHP service found")
    labels = (services.get(chosen, {}).get("labels") or {}) if chosen else {}
    labelled = str(labels.get(LABEL_PREFIX + "enabled", "")).lower() == "true"
    # The host agent reports this environment; the API authorizes it per project and environment.
    environment = (labels.get(LABEL_PREFIX + "environment") if labelled else None) or label_environment(project)
    databases = [{"service": name, "engine": engine, "labelled": is_labelled(spec)}
                 for name, spec in sorted(services.items()) if (engine := database_engine(name, spec))]
    return {"compose": True, "services": names, "service": chosen, "labelled": labelled, "reason": reason,
            "environment": environment, "label_project": labels.get(LABEL_PREFIX + "project") if labelled else None,
            "databases": databases}


def label_environment(project: Path) -> str:
    return env_value(project / ".env", "APP_ENV") or "development"


def compose_files(project: Path) -> list[str]:
    files = [name for name in BASE_FILES if (project / name).is_file()][:1]
    files += [name for name in OVERRIDE_FILES if (project / name).is_file()][:1]
    return files


def env_value(env_path: Path, key: str) -> str | None:
    if not env_path.is_file():
        return None
    for line in env_path.read_text(encoding="utf-8").splitlines():
        match = re.match(rf"^\s*{re.escape(key)}\s*=\s*(.*?)\s*$", line)
        if match:
            return match.group(1).strip().strip("\"'")
    return None


def set_env(env_path: Path, key: str, value: str) -> None:
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.is_file() else []
    written = False
    updated = []
    for line in lines:
        if re.match(rf"^\s*{re.escape(key)}\s*=", line):
            if not written:
                updated.append(f"{key}={value}")
                written = True
        else:
            updated.append(line)
    if not written:
        updated.append(f"{key}={value}")
    env_path.write_text("\n".join(updated).rstrip() + "\n", encoding="utf-8")


def exclude_from_git(project: Path, name: str) -> None:
    result = subprocess.run(["git", "rev-parse", "--git-path", "info/exclude"], cwd=project,
                            capture_output=True, text=True)
    if result.returncode != 0:
        return
    exclude = (project / result.stdout.strip()).resolve()
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text(encoding="utf-8").splitlines() if exclude.is_file() else []
    prefix = subprocess.run(["git", "rev-parse", "--show-prefix"], cwd=project, capture_output=True, text=True).stdout.strip()
    entry = "/" + prefix + name
    if entry not in existing:
        exclude.write_text("\n".join(existing + [entry]).strip() + "\n", encoding="utf-8")


def quote(value: str) -> str:
    return json.dumps(value)


def write(project: Path, service: str, project_id: str, library: str | None, databases: list[str] | None = None) -> dict:
    env_path = project / ".env"
    base = compose_files(project)
    config = compose_config(project, base)
    if config is None or service not in config.get("services", {}):
        raise SystemExit(f"Compose service '{service}' was not found.")
    spec = config["services"][service]
    # Database containers report in the same environment as the application.
    environment = (spec.get("labels") or {}).get(LABEL_PREFIX + "environment") if is_labelled(spec) else None
    environment = environment or label_environment(project)
    lines = [
        "# Generated by Developer Assistant (just php-otel-setup). Not committed: listed in .git/info/exclude.",
        "# Loaded through COMPOSE_FILE in .env. Delete this file and that entry to remove the wiring.",
        "services:",
    ]
    if not is_labelled(spec):
        lines += [
            f"  {service}:",
            "    labels:",
            f'      {LABEL_PREFIX}enabled: "true"',
            f"      {LABEL_PREFIX}project: {quote(project_id)}",
            f"      {LABEL_PREFIX}environment: {quote(environment)}",
            f'      {LABEL_PREFIX}commit: "${{GIT_COMMIT_SHA:-unknown}}"',
            f"      {LABEL_PREFIX}service: {quote(service)}",
        ]
        # Listing networks replaces the implicit default, so keep every network the service already uses.
        networks = list((spec.get("networks") or {"default": None}).keys())
        lines.append("    networks:")
        for name in networks + ["devassistant-telemetry-runtime"]:
            lines.append(f"      {name}: {{}}")
        if library:
            # Composer's path repository symlinks vendor/indexr/* to this host path; mount it at the same path.
            lines += ["    volumes:", f"      - {quote(library + ':' + library + ':ro')}"]
    for database in databases or []:
        db_spec = config["services"].get(database)
        if db_spec is None or not database_engine(database, db_spec):
            raise SystemExit(f"Compose service '{database}' is not a MySQL, MariaDB or PostgreSQL database.")
        if is_labelled(db_spec):
            continue
        lines += [
            f"  {database}:",
            "    labels:",
            f'      {LABEL_PREFIX}enabled: "true"',
            f"      {LABEL_PREFIX}project: {quote(project_id)}",
            f"      {LABEL_PREFIX}environment: {quote(environment)}",
            f'      {LABEL_PREFIX}commit: "{DATABASE_COMMIT}"',
            f"      {LABEL_PREFIX}service: {quote(database)}",
        ]
    if not is_labelled(spec):
        lines += [
            "networks:",
            "  devassistant-telemetry-runtime:",
            "    external: true",
            f"    name: {RUNTIME_NETWORK}",
        ]
    (project / OVERRIDE).write_text("\n".join(lines) + "\n", encoding="utf-8")
    exclude_from_git(project, OVERRIDE)

    current = env_value(env_path, "COMPOSE_FILE")
    files = current.split(os.pathsep) if current else base
    if OVERRIDE not in files:
        files.append(OVERRIDE)
    set_env(env_path, "COMPOSE_FILE", os.pathsep.join(files))
    compose_config(project, files)  # fail now, not at deploy time, if the merged config is invalid
    return {"override": OVERRIDE, "compose_file": os.pathsep.join(files), "environment": environment}


EXTENSION_OVERRIDE = "compose.indexr-ext.yaml"


def write_extension_mounts(project: Path, service: str, so_path: str, ini_path: str, extension_dir: str, ini_dir: str) -> dict:
    """Mount a PHP extension built elsewhere into a service, so the image does not have to be rebuilt.

    Written to its own file, loaded through COMPOSE_FILE like compose.indexr.yaml and kept out of Git, so rerunning
    the telemetry setup (which rewrites compose.indexr.yaml) never drops it. Delete the file and its COMPOSE_FILE
    entry to remove the mounts.
    """
    for value in (so_path, ini_path):
        if not os.path.isabs(value) or any(ch in value for ch in "\n\r\0"):
            raise SystemExit(f"Extension files must be given as absolute paths: {value}")
    for value in (extension_dir, ini_dir):
        if not value.startswith("/") or any(ch in value for ch in "\n\r\0"):
            raise SystemExit(f"Container directories must be absolute paths: {value}")
    lines = [
        "# Generated by Developer Assistant (Runtime setup, PHP extension without rebuilding the image).",
        "# Not committed: listed in .git/info/exclude. Loaded through COMPOSE_FILE in .env.",
        "# Delete this file and its COMPOSE_FILE entry to remove the mounts.",
        "services:",
        f"  {service}:",
        "    volumes:",
        f"      - {quote(so_path + ':' + extension_dir.rstrip('/') + '/opentelemetry.so:ro')}",
        f"      - {quote(ini_path + ':' + ini_dir.rstrip('/') + '/docker-php-ext-opentelemetry.ini:ro')}",
    ]
    (project / EXTENSION_OVERRIDE).write_text("\n".join(lines) + "\n", encoding="utf-8")
    exclude_from_git(project, EXTENSION_OVERRIDE)
    env_path = project / ".env"
    current = env_value(env_path, "COMPOSE_FILE")
    files = current.split(os.pathsep) if current else compose_files(project)
    if EXTENSION_OVERRIDE not in files:
        files.append(EXTENSION_OVERRIDE)
    set_env(env_path, "COMPOSE_FILE", os.pathsep.join(files))
    compose_config(project, files)  # fail now, not at deploy time, if the merged configuration is invalid
    return {"override": EXTENSION_OVERRIDE, "compose_file": os.pathsep.join(files)}


def main(argv: list[str]) -> int:
    if len(argv) >= 3 and argv[1] == "extension-mounts":
        options = dict(zip(argv[3::2], argv[4::2]))
        needed = ("--service", "--so", "--ini", "--extension-dir", "--ini-dir")
        if any(key not in options for key in needed):
            print("extension-mounts needs " + ", ".join(needed), file=sys.stderr)
            return 2
        print(json.dumps(write_extension_mounts(Path(argv[2]).resolve(), options["--service"], options["--so"], options["--ini"],
                                                options["--extension-dir"], options["--ini-dir"])))
        return 0
    if len(argv) < 3 or argv[1] not in {"detect", "write"}:
        print(__doc__, file=sys.stderr)
        return 2
    project = Path(argv[2]).resolve()
    options = dict(zip(argv[3::2], argv[4::2]))
    if argv[1] == "detect":
        print(json.dumps(detect(project, options.get("--service"))))
    else:
        if "--service" not in options or "--project-id" not in options:
            print("write needs --service and --project-id", file=sys.stderr)
            return 2
        databases = [name for name in options.get("--databases", "").split(",") if name]
        print(json.dumps(write(project, options["--service"], options["--project-id"], options.get("--library-path"), databases)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
