#!/usr/bin/env python3
"""Small command boundary used by Bash recipes; output never includes secrets."""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys

from config import choose, generate, validate
from state import ROOT, STATE, STAGES, checkpoint, load, private_dir, redact, save
import system


def compose(folder: str, *args: str, capture: bool = False):
    c = validate(load(STATE / "choices.json"))
    profiles = ["local-auth"] if folder == "web" else ([] if args and args[0] == "up" else ["runtime", "runtime-local"])
    profiles += [n for n in ("outline", "drawio") if n in c["optional"]] if folder == "web" else (["analyzer"] if "sonarqube" in c["optional"] else [])
    command = ["docker", "compose", "--project-directory", str(ROOT / folder)]
    for profile in profiles:
        command += ["--profile", profile]
    return subprocess.run(command + list(args), check=True, text=True, capture_output=capture)


def image_probe(build: bool) -> bool:
    for folder in ("api", "web"):
        config = json.loads(compose(folder, "config", "--format", "json", capture=True).stdout)
        for name, service in config["services"].items():
            if bool(service.get("build")) != build:
                continue
            image = service.get("image", config["name"] + "-" + name)
            if subprocess.run(["docker", "image", "inspect", image], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
                return False
    return True


def main() -> int:
    private_dir(STATE)
    action, *args = sys.argv[1:]
    if action == "lock":
        path = STATE / "installer.lock"
        if path.is_symlink():
            raise ValueError("Refusing symbolic-link installer lock")
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another installer is running. Wait for it to finish before resuming.") from None
        os.set_inheritable(fd, True)
        os.execvp("bash", ["bash", *args])
    elif action == "report":
        return 0 if system.report(strict="strict" in args) else 1
    elif action == "choose":
        choose(edit="edit" in args)
    elif action == "review":
        system.review()
    elif action == "dependencies":
        system.install_dependencies()
    elif action == "ports":
        system.check_ports()
    elif action == "configuration":
        generate()
        from verify import report
        report({}, "Installation in progress; verification pending")
    elif action == "certificates":
        system.certificates()
        from config import make_overrides
        make_overrides(validate(load(STATE / "choices.json")))
    elif action == "prepare":
        c = load(STATE / "choices.json")
        path = STATE / "sonarqube-bootstrap.json"
        if "sonarqube" in c["optional"] and not path.exists():
            document = json.loads(compose("api", "config", "--format", "json", capture=True).stdout)
            volumes = {v["name"] for k, v in document["volumes"].items() if k.startswith(("sonarqube-", "analyzer-postgres-"))}
            existing = set(subprocess.run(["docker", "volume", "ls", "--format", "{{.Name}}"], check=True, text=True, capture_output=True).stdout.splitlines())
            save(path, {"fresh": not bool(volumes & existing)})
    elif action == "checkpoint":
        checkpoint(args[0])
    elif action == "valid":
        return 0 if checkpoint(args[0], valid=True) else 1
    elif action == "probe":
        if args[0] in ("images", "build"):
            return 0 if image_probe(args[0] == "build") else 1
        if args[0] == "models":
            c = load(STATE / "choices.json")
            models = set(c["models"])
            from state import env_read
            models.update(v[k] for v in (env_read(ROOT / "api/.env"), env_read(ROOT / "web/.env"))
                          for k in ("OLLAMA_EMBED_MODEL", "OLLAMA_MODEL", "OLLAMA_EVAL_MODEL") if v.get(k))
            for model in models:
                if subprocess.run(["ollama", "show", model], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
                    return 1
    elif action == "models":
        if not system.run("curl", "--noproxy", "*", "-fsS", "--max-time", "5", "http://127.0.0.1:11434/api/tags", check=False):
            raise RuntimeError("Start Ollama before downloading models (macOS: open Ollama or brew services start ollama; Linux: sudo systemctl start ollama, or ollama serve). For Docker access, Ollama must listen on the host gateway, usually OLLAMA_HOST=0.0.0.0:11434; restrict it with the host firewall. Resume: bash install.sh")
        from state import env_read
        c = load(STATE / "choices.json")
        values = env_read(ROOT / "api/.env"), env_read(ROOT / "web/.env")
        for model in sorted(set(c["models"]) | {v[k] for v in values for k in ("OLLAMA_EMBED_MODEL", "OLLAMA_MODEL", "OLLAMA_EVAL_MODEL") if v.get(k)}):
            subprocess.run(["ollama", "pull", model], check=True)
    elif action == "compose":
        compose(args[0], *args[1:])
    elif action == "verify":
        from verify import verify
        verify()
    elif action == "info":
        path = STATE / "services.txt"
        if not path.exists():
            raise RuntimeError("No installation report yet. Run bash install.sh")
        print(path.read_text(), end="")
    elif action == "status":
        for stage in STAGES:
            current = checkpoint(stage, valid=True)
            print(f"{stage}: {'recorded (will revalidate)' if current else 'pending or inputs changed'}")
        print("Resume: bash install.sh\nCredentials: just install-info")
    elif action == "logs":
        path = STATE / "progress.log"
        if path.is_symlink():
            raise ValueError("Refusing symbolic-link log")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as stream:
            for line in sys.stdin:
                value = redact(line)
                print(value, end="", flush=True)
                stream.write(value)
                stream.flush()
    else:
        raise ValueError("Unknown installer action")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError, KeyError, subprocess.CalledProcessError) as error:
        # Subprocess errors include full commands; never print them or response bodies.
        message = "External command failed (details suppressed to protect credentials)." if isinstance(error, subprocess.CalledProcessError) else str(error)
        print("Installer: " + redact(message), file=sys.stderr)
        raise SystemExit(1)
