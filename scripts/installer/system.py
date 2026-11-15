"""Host preflight, reviewed dependency actions and local certificate provisioning."""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

from config import hosts_block, selected, url, validate
from state import ROOT, STATE, atomic, load, private_dir, env_read, env_update


def run(*args: str, check: bool = True) -> str:
    result = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and result.returncode:
        raise RuntimeError(f"{args[0]} failed; check dependency availability and permissions (output suppressed)")
    return result.stdout.strip() if not result.returncode else ""


def version(text: str) -> tuple[int, ...]:
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text)
    return tuple(int(v or 0) for v in match.groups()) if match else (0, 0, 0)


DEPENDENCIES = (
    ("docker", (24, 0, 0), ("docker", "--version"), "container engine", "Install/start Docker Desktop (macOS) or Docker Engine (Linux)"),
    ("compose", (2, 24, 4), ("docker", "compose", "version", "--short"), "safe port overrides and orchestration", "Update Docker Compose plugin to >=2.24.4"),
    ("buildx", (0, 12, 0), ("docker", "buildx", "version"), "BuildKit builder management", "Install/update Docker Buildx plugin"),
    ("python3", (3, 10, 0), (os.environ.get("INSTALL_HOST_PYTHON", "python3"), "--version"), "isolated Runtime host helpers", "Install Python >=3.10 and venv support"),
    ("node", (22, 0, 0), ("node", "--version"), "host system monitor", "Install Node.js >=22"),
    ("ollama", (0, 11, 0), ("ollama", "--version"), "host inference and model storage", "Install/start Ollama >=0.11"),
    ("mkcert", (1, 4, 4), ("mkcert", "-version"), "local HTTPS identity and OS trust", "Install mkcert >=1.4.4"),
)


def report(strict: bool = False) -> bool:
    system = platform.system()
    machine = platform.machine()
    supported = system in {"Darwin", "Linux"} and machine in {"arm64", "aarch64", "x86_64", "amd64"}
    if system == "Linux":
        release = Path("/etc/os-release").read_text()
        supported = supported and bool(re.search(r"(?m)^(?:ID|ID_LIKE)=.*(?:ubuntu|debian)", release))
    print(f"Host: {system} {machine}; {'supported' if supported else 'UNSUPPORTED'}; {'desktop' if desktop() else 'headless'}")
    good = supported
    print("Dependency | Required | Detected | Status | Purpose | Proposed action")
    for name, minimum, command, purpose, action in DEPENDENCIES:
        detected = run(*command, check=False) if shutil.which(command[0]) else ""
        ok = version(detected) >= minimum
        print(f"{name} | {'.'.join(map(str, minimum))}+ | {detected or 'missing'} | {'OK' if ok else 'ACTION'} | {purpose} | {'none' if ok else action}")
        good &= ok
    for utility in ("curl", "tar", "openssl", "make", "ssh", "ssh-agent", "sudo"):
        found = shutil.which(utility)
        print(f"{utility} | OS utility | {'present' if found else 'missing'} | {'OK' if found else 'ACTION'} | bootstrap/Runtime | {'none' if found else 'Install OS package'}")
        good &= bool(found)
    engine = run("docker", "info", "--format", "{{.OSType}}", check=False) if shutil.which("docker") else ""
    print(f"Docker daemon/permission: {'OK' if engine == 'linux' else 'ACTION: start a local Linux engine and allow this user access; log out/in after group changes'}")
    good &= engine == "linux"
    if engine:
        builder = run("docker", "buildx", "inspect", check=False)
        match = re.search(r"BuildKit(?: version)?:\s*v?([\d.]+)", builder, re.I)
        buildkit = version(match[1]) if match else (0, 0, 0)
        good &= buildkit >= (0, 12, 0)
        print(f"BuildKit | 0.12.0+ | {match[1] if match else 'unavailable'} | {'OK' if buildkit >= (0, 12, 0) else 'ACTION: initialize/start a local BuildKit builder with docker buildx inspect --bootstrap'} | persistent build caches")
        context = run("docker", "context", "inspect", check=False)
        try:
            endpoint = json.loads(context)[0]["Endpoints"]["docker"]["Host"]
        except (ValueError, KeyError, IndexError):
            endpoint = ""
        local = endpoint.startswith("unix://") and not os.environ.get("DOCKER_HOST", "").startswith(("tcp:", "ssh:"))
        good &= local
        print(f"Docker context: {'local' if local else 'ACTION: select a local unix-socket Docker context'}")
        resources = run("docker", "info", "--format", "{{.MemTotal}} {{.NCPU}}", check=False).split()
        if len(resources) == 2:
            memory, cpus = map(int, resources)
            minimum_memory = 16 if "sonarqube" in load(STATE / "choices.json", {}).get("optional", []) else 8
            sufficient = memory >= minimum_memory * 1024**3 and cpus >= 4
            print(f"Docker resources: {memory // 1024**3} GiB / {cpus} CPUs; {'OK' if sufficient else 'ACTION: allocate at least 8 GiB and 4 CPUs (16 GiB recommended for analyzers)'}")
            good &= sufficient
    disk = shutil.disk_usage(ROOT).free // 1024**3
    writable = os.access(ROOT, os.W_OK)
    print(f"Disk: {disk} GiB free (40 GiB required); app directory writable: {writable}")
    good &= disk >= 40 and writable
    print("Containers provide: Node 22 web, Python 3.12 API, PHP/Composer, Go/analyzer toolchains, databases, Caddy. No host pip/npm application install.")
    if not desktop():
        print("Headless: browser launch, native approval dialogs and desktop credential-store integrations are unavailable. Terminal Runtime operations remain available; visit HTTPS locally via a browser on this host.")
    return bool(supported and (good or not strict))


def desktop() -> bool:
    return platform.system() == "Darwin" or bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def install_dependencies() -> None:
    if platform.system() == "Darwin":
        if not shutil.which("brew"):
            raise RuntimeError("Install Homebrew, Docker Desktop, Python >=3.10, Node >=22, Ollama and mkcert; then bash install.sh")
        for binary, formula in (("mkcert", "mkcert"), ("node", "node@22"), ("ollama", "ollama")):
            minimum = next(d[1] for d in DEPENDENCIES if d[0] == binary)
            command = next(d[2] for d in DEPENDENCIES if d[0] == binary)
            detected = run(*command, check=False) if shutil.which(binary) else ""
            if version(detected) < minimum:
                subprocess.run(["brew", "install", formula], check=True)
        subprocess.run(["brew", "install", "nss"], check=True)
    elif platform.system() == "Linux":
        subprocess.run(["sudo", "apt-get", "update"], check=True)
        subprocess.run(["sudo", "apt-get", "install", "-y", "ca-certificates", "curl", "openssl", "make", "openssh-client", "python3", "python3-venv", "mkcert", "libnss3-tools"], check=True)
    else:
        raise RuntimeError("Unsupported host OS")
    if not report(strict=True):
        raise RuntimeError("Finish the ACTION items above (Docker/Node/Ollama may require manual OS steps), then bash install.sh")


def check_ports() -> None:
    c = validate(load(STATE / "choices.json"))
    desired = {c["hosts"][n]["port"] for n in selected(c)} | {c["bolt_port"], c["grpc_port"]}
    owned = set()
    # Existing installer containers may already own these exact ports on resume.
    if shutil.which("docker"):
        for folder in ("api", "web"):
            result = subprocess.run(["docker", "compose", "--project-directory", str(ROOT / folder), "ps", "--format", "json"], capture_output=True, text=True)
            if result.returncode == 0:
                raw = result.stdout.strip()
                rows = json.loads(raw) if raw.startswith("[") else [json.loads(line) for line in raw.splitlines() if line]
                if isinstance(rows, dict):
                    rows = [rows]
                for row in rows:
                    if row.get("Service") in {"local-https", "neo4j"}:
                        owned.update(int(p["PublishedPort"]) for p in row.get("Publishers") or [] if p.get("PublishedPort"))
    for port in sorted(desired):
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except PermissionError:
                # Rootless Docker may also need explicit low-port OS setup.
                print(f"Port {port}: low-port binding needs Docker permission; verify Docker can publish it")
            except OSError:
                if port not in owned:
                    raise RuntimeError(f"Port {port} is occupied by another process; edit .install/choices.json and resume")
    print("Published ports checked (loopback only).")


def review() -> None:
    c = validate(load(STATE / "choices.json"))
    print("\nINSTALLATION REVIEW")
    print("Packages: macOS Homebrew mkcert/nss, Node 22 and Ollama if needed; Ubuntu/Debian apt utilities, Python/venv, mkcert/NSS. Docker and unsupported versions may need manual setup.")
    print("Privileged changes: sudo apt on Linux; mkcert OS/NSS root trust; one marked /etc/hosts block. Existing hosts outside the block are preserved.")
    print("Application changes: back up existing env files privately; fill missing credentials; update public URLs and persisted OIDC client redirects/origins, preserving passwords and data.")
    print("Containers/images/models will be downloaded and built; package caches and volumes persist.")
    for name in selected(c):
        print(f"  127.0.0.1 {c['hosts'][name]['hostname']} -> {url(c, name)}")
    print(f"  Neo4j encrypted Bolt: {c['bolt_port']}; Qdrant TLS gRPC: {c['grpc_port']}")
    print("The local CA private key stays on this host. Runtime ingress/mTLS is separate.")


def certificates() -> None:
    c = validate(load(STATE / "choices.json"))
    subprocess.run(["mkcert", "-install"], check=True)
    caroot = run("mkcert", "-CAROOT")
    for name, names in (("proxy", sorted({c['hosts'][n]['hostname'] for n in selected(c)})),
                        ("neo4j", [c["hosts"]["neo4j"]["hostname"]])):
        directory = STATE / "certs" / name
        private_dir(directory)
        cert, key = directory / "cert.pem", directory / "key.pem"
        valid = cert.exists() and key.exists() and bool(run("openssl", "x509", "-checkend", "2592000", "-noout", "-in", str(cert), check=False))
        if valid:
            valid = bool(run("openssl", "verify", "-CAfile", caroot + "/rootCA.pem", str(cert), check=False))
        if valid:
            public = run("openssl", "x509", "-in", str(cert), "-pubkey", "-noout", check=False)
            valid = bool(public) and public == run("openssl", "pkey", "-in", str(key), "-pubout", check=False)
        if valid:
            for hostname in names:
                if not run("openssl", "x509", "-in", str(cert), "-noout", "-checkhost", hostname, check=False):
                    valid = False
        if not valid:
            subprocess.run(["mkcert", "-cert-file", str(cert), "-key-file", str(key), *names], check=True)
        cert.chmod(0o644)
        key.chmod(0o644 if name == "neo4j" else 0o600)
        directory.chmod(0o755 if name == "neo4j" else 0o700)
    hosts = Path("/etc/hosts")
    proposal = STATE / "hosts.proposed"
    current = hosts.read_text()
    proposed = hosts_block(current, c)
    if proposed != current:
        atomic(proposal, proposed)
        if not (STATE / "backups/hosts").exists():
            private_dir(STATE / "backups")
            atomic(STATE / "backups/hosts", current)
        # Preserve /etc/hosts inode, permissions and ownership.
        with proposal.open("rb") as stream:
            subprocess.run(["sudo", "tee", "/etc/hosts"], stdin=stream, stdout=subprocess.DEVNULL, check=True)
    if "sonarqube" in c["optional"]:
        analyzer_certificates()
    print("Local service certificates and hosts block ready.")


def analyzer_certificates() -> None:
    """Keep analyzer mTLS separate; never replace an existing partial identity."""
    values = env_read(ROOT / "api/.env")
    directory = STATE / "analyzer-tls"
    api = Path(values.get("ANALYZER_API_TLS_DIR", str(directory / "api")))
    worker = Path(values.get("ANALYZER_WORKER_TLS_DIR", str(directory / "worker")))
    if not api.is_absolute():
        api = ROOT / "api" / api
    if not worker.is_absolute():
        worker = ROOT / "api" / worker
    required = [api / name for name in ("api.crt", "api.key", "worker-ca.crt")]
    required += [worker / name for name in ("worker.crt", "worker.key", "api-ca.crt")]
    if not any(path.exists() for path in required) and not directory.exists() and not any(
            values.get(key) for key in ("ANALYZER_API_TLS_DIR", "ANALYZER_WORKER_TLS_DIR")):
        subprocess.run(["bash", str(ROOT / "api/analyzer-worker/provision-certs.sh"), str(directory), "analyzer-worker"], check=True)
    if not all(path.is_file() for path in required):
        raise RuntimeError("Existing analyzer mTLS bundle is incomplete. Restore its matching API/worker certificates; no identity was overwritten. Resume: bash install.sh")
    fingerprint = run("openssl", "x509", "-in", str(worker / "worker.crt"), "-noout", "-fingerprint", "-sha256").split("=", 1)[-1].replace(":", "").lower()
    if values.get("ANALYZER_WORKER_CERT_SHA256") and values["ANALYZER_WORKER_CERT_SHA256"] != fingerprint:
        raise RuntimeError("Stored analyzer fingerprint differs from its certificate. Reconcile the existing identity before resuming.")
    env_update(ROOT / "api/.env", {"ANALYZER_API_TLS_DIR": str(api), "ANALYZER_WORKER_TLS_DIR": str(worker),
                                   "ANALYZER_CERT_GID": values.get("ANALYZER_CERT_GID", str(os.getgid())),
                                   "ANALYZER_WORKER_CERT_SHA256": fingerprint})
