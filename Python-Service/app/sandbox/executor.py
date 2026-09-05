"""Sandbox executor — two-tier project runtime analysis against the nested
DinD sandbox daemon (runsc).

Tier 1 (dependency resolution): a NETWORKED container runs the framework's
dependency installer (e.g. ``composer install``) on a writable copy of the
project. Network egress is required here (packagist). This tier only runs the
package-manager binary plus project root scripts — the documented boundary.

Tier 2 (runtime probes): the project (now with vendor/) is re-mounted
READ-ONLY into a ``runsc`` container with ``--network=none``, ALL caps dropped,
pids/mem limited — the same hardening compose_generator encodes. Artisan
commands run here produce runtime truth (route:list == Router::getRoutes()).

Image policy: prefer images ALREADY pulled on the daemon (``composer:2`` is
cached in dind-storage); pull only when the daemon has egress; otherwise the
probe reports an honest "image unavailable" failure.

Transport is plain HTTP against the Docker Engine API (urllib) — no docker
Python SDK, so backend changes stay live via the /app bind-mount.
"""

from __future__ import annotations

import io
import json
import os
import tarfile
import urllib.error as error
import urllib.request as request
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from infra.exception_logger import get_exception_logger

CONFIG_PATH = os.getenv("SANDBOX_RUNTIMES_CONFIG", "/app/config/sandbox_runtimes.yml")

# Runtime hardening shared with compose_generator / ReproduceSpec defaults.
_RUNSC_CAPS = ["ALL"]
_RUNSC_PIDS = 256
_RUNSC_MEM_BYTES = 1 * 1024 * 1024 * 1024  # 1g
_RUNSC_TMPFS = {"/tmp": "rw,size=536870912,mode=1777"}  # 512m — composer cache fits

# Env injected into every probe: identical to laravel.py _runtime_routes so a
# misbehaving artisan command can only touch an empty in-memory sqlite + array
# drivers — never real project config or a network.
_PROBE_ENV = {
    "APP_ENV": "testing",
    "APP_DEBUG": "false",
    "APP_KEY": "base64:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    "DB_CONNECTION": "sqlite",
    "DB_DATABASE": ":memory:",
    "CACHE_DRIVER": "array",
    "SESSION_DRIVER": "array",
    "QUEUE_CONNECTION": "sync",
    "MAIL_MAILER": "array",
    "COMPOSER_ALLOW_SUPERUSER": "1",
    "HOME": "/tmp",
}

_INSTALL_ENV = {
    "COMPOSER_ALLOW_SUPERUSER": "1",
    "COMPOSER_NO_INTERACTION": "1",
    "HOME": "/tmp",
    "COMPOSER_CACHE_DIR": "/tmp/composer-cache",
}

# Noise dirs never staged (macOS zip cruft etc.).
_NOISE_DIRS = {"__MACOSX"}


def jobs_root() -> str:
    return os.environ.get("JOBS_ROOT", "/jobs")


def _normalize_host(docker_host: str | None) -> str:
    host = docker_host or os.environ.get("DOCKER_HOST", "")
    if not host or host.startswith("unix://"):
        raise ValueError(
            "sandbox executor requires a TCP Docker host (DOCKER_HOST); "
            "unix sockets are not supported for container orchestration"
        )
    return host.replace("tcp://", "http://").rstrip("/")


class DockerApi:
    """Minimal Docker Engine API client over HTTP (no SDK)."""

    def __init__(self, docker_host: str | None = None, timeout: float = 10.0) -> None:
        self.base = _normalize_host(docker_host)
        self.timeout = timeout

    # -- transport ----------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> tuple[int, bytes]:
        req = urllib.request.Request(
            self.base + path, data=body, method=method, headers=headers or {}
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def _json(self, method: str, path: str, payload: dict | None = None,
              timeout: float | None = None) -> Any:
        body = None
        headers: dict[str, str] = {}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        status, raw = self._request(method, path, body=body, headers=headers,
                                    timeout=timeout)
        if status >= 400:
            raise RuntimeError(
                f"docker API {method} {path} -> {status}: "
                f"{raw[:400].decode('utf-8', 'replace')}"
            )
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return raw.decode("utf-8", "replace")

    # -- engine ops ---------------------------------------------------------

    def ping(self) -> bool:
        try:
            status, _ = self._request("GET", "/_ping", timeout=3.0)
            return status < 400
        except Exception:  # noqa: BLE001
            return False

    def list_images(self) -> list[dict[str, Any]]:
        """Images present on the daemon: [{Id, RepoTags, Labels}]."""
        out = self._json("GET", "/images/json")
        return out if isinstance(out, list) else []

    def image_exists(self, reference: str) -> bool:
        """Search the daemon's LOCAL images for repo:tag (exact) or repo."""
        repo, _, tag = reference.partition(":")
        tag = tag or "latest"
        for img in self.list_images():
            for rt in img.get("RepoTags") or []:
                r, _, t = rt.rpartition(":")
                if r == repo and (t == tag or t == "latest"):
                    return True
        return False

    def pull_image(self, reference: str, timeout: float = 300.0) -> bool:
        """Best-effort pull (needs daemon egress). Streams ignored; result is
        re-checked via image_exists."""
        repo, _, tag = reference.partition(":")
        status, _ = self._request(
            "POST", f"/images/create?fromImage={repo}&tag={tag or 'latest'}",
            timeout=timeout,
        )
        return status < 400

    def create_container(self, spec: dict[str, Any]) -> str:
        out = self._json("POST", "/containers/create", payload=spec)
        return str(out.get("Id", out.get("id", "")))

    def start(self, container_id: str) -> None:
        self._json("POST", f"/containers/{container_id}/start")

    def wait(self, container_id: str) -> int:
        out = self._json("POST", f"/containers/{container_id}/wait", timeout=900.0)
        if isinstance(out, dict):
            code = out.get("StatusCode", out.get("status_code"))
            if code is not None:
                return int(code)
        return -1

    def logs(self, container_id: str) -> str:
        """Raw logs (Tty containers emit plain text). Logs stream until the
        container exits — dependency installs can take minutes, so keep a long
        timeout here (not the default per-request 10s)."""
        raw = self._request(
            "GET",
            f"/containers/{container_id}/logs?stdout=1&stderr=1",
            timeout=600.0,
        )[1]
        return raw.decode("utf-8", "replace")

    def remove(self, container_id: str, force: bool = True) -> None:
        try:
            self._json("DELETE", f"/containers/{container_id}?force={int(force)}")
        except RuntimeError:
            pass  # already gone

    def put_archive(self, container_id: str, path: str, tar_bytes: bytes) -> None:
        status, raw = self._request(
            "PUT",
            f"/containers/{container_id}/archive?path={path}",
            body=tar_bytes,
            headers={"Content-Type": "application/x-tar"},
            timeout=120.0,
        )
        if status >= 400:
            raise RuntimeError(
                f"put_archive -> {status}: {raw[:300].decode('utf-8', 'replace')}"
            )

    # -- lifecycle helper ---------------------------------------------------

    def run_once(
        self,
        *,
        image: str,
        argv: list[str],
        working_dir: str,
        host_config: dict[str, Any],
        env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Create → start → wait → collect logs → remove. Returns the run
        record {exit_code, stdout} or {status:'failed', error}."""
        spec: dict[str, Any] = {
            "Image": image,
            "Cmd": argv,
            "WorkingDir": working_dir,
            "Tty": True,
            "Env": [f"{k}={v}" for k, v in (env or {}).items()],
            "HostConfig": host_config,
        }
        try:
            cid = self.create_container(spec)
        except Exception as exc:  # noqa: BLE001
            return {"status": "failed", "error": str(exc)[:400]}
        try:
            try:
                self.start(cid)
            except Exception as exc:  # noqa: BLE001
                return {
                    "status": "failed",
                    "exit_code": None,
                    "error": f"start failed: {exc}"[:400],
                }
            exit_code = self.wait(cid)
            stdout = self.logs(cid)
            return {"status": "done", "exit_code": exit_code, "stdout": stdout}
        finally:
            self.remove(cid)


def _tar_directory(workspace_path: str, prefix: str) -> bytes:
    """Tar the workspace contents with each entry prefixed by ``prefix``.

    prefix is ``<project_id>/src`` relative to the put_archive destination
    (the daemon JOBS_ROOT mount), so one put at /jobs lays out the whole tree.
    """
    root = Path(workspace_path)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for child in sorted(root.rglob("*")):
            if not child.is_file():
                continue
            rel = child.relative_to(root)
            if any(part in _NOISE_DIRS for part in rel.parts):
                continue
            info = tarfile.TarInfo(f"{prefix}/{rel.as_posix()}")
            try:
                data = child.read_bytes()
            except OSError:
                continue
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _find_manifest_rel(workspace_path: str, manifest: str) -> str | None:
    """Locate the real app dir holding ``manifest`` (root or one child deep).

    Returns the RELATIVE dir ('' for root, or a child name) or None when the
    marker is absent — callers degrade honestly instead of guessing.
    """
    root = Path(workspace_path)
    if not root.is_dir():
        return None
    if (root / manifest).is_file():
        return ""
    for child in sorted(p for p in root.iterdir() if p.is_dir()):
        if child.name.startswith(".") or child.name in _NOISE_DIRS:
            continue
        if (child / manifest).is_file():
            return child.name
    return None


class SandboxExecutor:
    """Two-tier project analysis driver (stage → install → probes)."""

    def __init__(
        self,
        docker_host: str | None = None,
        runtime_config_path: str = CONFIG_PATH,
    ) -> None:
        self.api = DockerApi(docker_host)
        self.config_path = runtime_config_path
        self._config: dict[str, Any] | None = None
        # project_id -> relative app dir inside the staged source ('' = root).
        self._rel: dict[str, str] = {}
        self._stage_probe_detail: dict[str, Any] | None = None

    # -- config -------------------------------------------------------------

    def load_runtime_config(self) -> dict[str, Any]:
        if self._config is None:
            import yaml

            path = Path(self.config_path)
            self._config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return self._config

    def runtime_for(self, framework: str) -> dict[str, Any] | None:
        conf = self.load_runtime_config()
        return conf.get("runtimes", {}).get(framework or "php")

    # -- staging ------------------------------------------------------------

    def stage_project(self, workspace_path: str, project_id: str) -> str:
        """Upload the workspace into the daemon JOBS_ROOT via a throwaway
        container on the jobs volume. Returns the daemon-side source path."""
        if not project_id or "/" in project_id or ".." in Path(project_id).parts:
            raise ValueError(f"unsafe project_id: {project_id!r}")
        # Nested-DinD put_archive silently drops trees under brand-new bind
        # dirs — pre-create the project dir from a container first (mkdir
        # through the bind persists; tar merges into the existing dir).
        self.api.run_once(
            image="alpine:3.20.3",
            argv=["sh", "-c", f"mkdir -p '{jobs_root()}/{project_id}'"],
            working_dir="/",
            host_config={"Binds": [f"{jobs_root()}:/jobs"]},
        )
        tar_bytes = _tar_directory(workspace_path, f"{project_id}/src")
        holder = self.api.create_container(
            {
                "Image": "alpine:3.20.3",
                "Cmd": ["sleep", "infinity"],
                "HostConfig": {
                    "Binds": [f"{jobs_root()}:/jobs"],
                    "ReadonlyRootfs": False,
                },
            }
        )
        try:
            self.api.put_archive(holder, "/jobs", tar_bytes)
        finally:
            self.api.remove(holder, force=True)
        return f"{jobs_root()}/{project_id}/src"

    # -- tier 1: dependency install -----------------------------------------

    def install_dependencies(
        self, project_id: str, framework: str = "php"
    ) -> dict[str, Any]:
        """Networked (non-runsc) dependency install on a writable project copy.
        Uses an image the daemon already has; pulls only when egress exists."""
        rt = self.runtime_for(framework)
        if not rt:
            return {"status": "skipped", "reason": f"no runtime config for {framework!r}"}
        image = rt.get("image", "")
        if not self.api.image_exists(image) and not self.api.pull_image(image):
            return {
                "status": "unavailable",
                "reason": f"image {image} not pulled and daemon has no egress",
                "image": image,
            }
        installer = rt.get("installer") or [
            "composer", "install", "--no-interaction", "--no-progress",
            "--prefer-dist",
        ]
        # Nested-DinD DNS drops out under composer's parallel dist downloads
        # (curl 'Resolving timed out'). Warm the composer cache with retried
        # --dry-run passes (each accumulates cached dists inside the same
        # container), then materialize vendor fully offline — no network
        # needed once the cache is complete.
        import shlex

        cmd_name = installer[0]
        verb = installer[1] if len(installer) > 1 and installer[1] in (
            "install", "update", "require", "dump-autoload",
        ) else "install"
        rest = installer[2:] if len(installer) > 1 and installer[1] == verb else installer[1:]
        opts = " ".join(shlex.quote(a) for a in rest)
        script = (
            "i=0; while [ $i -lt 6 ]; do "
            f"{cmd_name} {verb} {opts} --dry-run >/dev/null 2>&1 && break; "
            "i=$((i+1)); done; "
            "export COMPOSER_DISABLE_NETWORK=1; "
            f"exec {cmd_name} {verb} {opts}"
        )
        src = f"{jobs_root()}/{project_id}/src"
        rel = self._rel.get(project_id, "")
        working_dir = "/app" if not rel else f"/app/{rel}"
        result = self.api.run_once(
            image=image,
            argv=["sh", "-c", script],
            working_dir=working_dir,
            env={**_INSTALL_ENV},
            host_config={
                "Binds": [f"{src}:/app"],
                "ReadonlyRootfs": False,
                "CapDrop": _RUNSC_CAPS,
                "PidsLimit": _RUNSC_PIDS,
                "Memory": _RUNSC_MEM_BYTES,
                "Tmpfs": dict(_RUNSC_TMPFS),
                # Empirical (nested DinD): composer's curl resolver flakes on
                # the embedded forwarder under cold-cache load but is reliable
                # pinned to 8.8.8.8. The wrapper warms the cache with --dry-run
                # passes, so the final install runs fully offline.
                "Dns": ["8.8.8.8"],
            },
        )
        result.setdefault("image", image)
        return result

    # -- tier 2: runtime probes ---------------------------------------------

    def run_probe(
        self,
        project_id: str,
        probe_name: str,
        framework: str = "php",
        argv: list[str] | None = None,
    ) -> dict[str, Any]:
        """Run an artisan probe under runsc, network none, read-only mount."""
        rt = self.runtime_for(framework)
        if not rt:
            return {"name": probe_name, "status": "skipped",
                    "reason": f"no runtime config for {framework!r}"}
        image = rt.get("image", "")
        if not self.api.image_exists(image):
            return {
                "name": probe_name,
                "status": "unavailable",
                "error": f"image {image} not pulled",
                "image": image,
            }
        command = argv or (rt.get("probes", {}).get(probe_name))
        if not command:
            return {"name": probe_name, "status": "skipped",
                    "reason": f"no probe template {probe_name!r}"}
        src = f"{jobs_root()}/{project_id}/src"
        rel = self._rel.get(project_id, "")
        working_dir = "/app" if not rel else f"/app/{rel}"
        record = self.api.run_once(
            image=image,
            argv=command,
            working_dir=working_dir,
            env={**_PROBE_ENV},
            host_config={
                "Runtime": "runsc",
                "Binds": [f"{src}:/app:ro"],
                "NetworkMode": "none",
                "ReadonlyRootfs": True,
                "CapDrop": _RUNSC_CAPS,
                "PidsLimit": _RUNSC_PIDS,
                "Memory": _RUNSC_MEM_BYTES,
                "Tmpfs": dict(_RUNSC_TMPFS),
            },
        )
        record["name"] = probe_name
        record["command"] = command
        record["image"] = image
        return record

    # -- interactive shell (docker-run feel, state via writable /app) -------

    def project_staged(self, project_id: str) -> bool:
        """True when the daemon still carries ``/jobs/<pid>/src`` (an earlier
        analyze of this project). Uses a throwaway alpine ls over the read-only
        /jobs bind — never raises. Retried because nested-DinD container
        creation right after a job can transiently fail."""
        import time

        last: dict[str, Any] = {}
        for attempt in range(3):
            try:
                rec = self.api.run_once(
                    image="alpine:3.20.3",
                    argv=["sh", "-c", f"test -d /jobs/{project_id}/src && echo yes"],
                    working_dir="/",
                    env={"HOME": "/tmp"},
                    host_config={
                        "Binds": [f"{jobs_root()}:/jobs:ro"],
                        "CapDrop": _RUNSC_CAPS,
                        "PidsLimit": 64,
                        "Memory": 128 * 1024 * 1024,
                    },
                )
                last = rec
                if rec.get("exit_code") == 0:
                    # Exit code is the truth — tiny containers can exit before
                    # their logs are readable, so never require the echo text.
                    self._stage_probe_detail = None
                    return True
                if rec.get("status") == "failed":
                    time.sleep(2.0)
                    continue
                self._stage_probe_detail = {
                    "exit_code": rec.get("exit_code"),
                    "stdout": (rec.get("stdout") or "")[:120],
                }
                return False
            except Exception as exc:  # noqa: BLE001 — liveness check is best-effort
                last = {"error": str(exc)[:200]}
                time.sleep(2.0)
        self._stage_probe_detail = {"error": (last.get("error") or str(last))[:200]}
        return False

    def run_command(
        self,
        project_id: str,
        command: str,
        *,
        framework: str = "php",
        cwd: str | None = None,
    ) -> dict[str, Any]:
        """Run an arbitrary shell command against the staged project — the
        "terminal like docker" path.

        Executes ``sh -c <command>`` in a NETWORKED, writable container (same
        hardened caps/pids/mem as tier 1) with the project mounted rw at /app,
        so files the command writes (composer require, artisan make:*,
        migrations, logs) PERSIST on the daemon for later commands. Each call
        is a fresh container; state lives in the bind mount, not the process
        (no TTY, so no long-running interactive apps like ``artisan serve``).
        """
        rt = self.runtime_for(framework)
        if not rt:
            return {
                "status": "skipped",
                "reason": f"no runtime config for {framework!r}",
            }
        image = rt.get("image", "")
        if not self.api.image_exists(image):
            return {
                "status": "unavailable",
                "error": f"image {image} not pulled",
                "image": image,
            }
        if not self.project_staged(project_id):
            detail = self._stage_probe_detail
            reason = (
                "project not staged on the sandbox daemon — run an analysis "
                "first (or re-run it after the daemon restarted)"
            )
            if detail:
                import json as _json
                reason += f" [liveness: {_json.dumps(detail)[:240]}]"
            return {"status": "skipped", "reason": reason}
        rel = self._rel.get(project_id, "")
        src = f"{jobs_root()}/{project_id}/src"
        if cwd:
            script = command
            working_dir = cwd
        else:
            working_dir = "/app" if not rel else f"/app/{rel}"
            if not rel:
                # App may sit under one nested dir (uploads wrap: src/Archive/).
                # Discover it inside the SAME container before running.
                script = (
                    "cd /app || exit 9; if [ ! -f composer.json ]; then "
                    "for x in /app/*/; do [ -f \"$x/composer.json\" ] && "
                    "cd \"$x\" && break; done; fi; " + command
                )
            else:
                script = command
        record = self.api.run_once(
            image=image,
            argv=["sh", "-c", script],
            working_dir=working_dir,
            env={**_PROBE_ENV, "COMPOSER_CACHE_DIR": "/tmp/composer-cache"},
            host_config={
                "Binds": [f"{src}:/app"],
                "ReadonlyRootfs": False,
                "CapDrop": _RUNSC_CAPS,
                "PidsLimit": _RUNSC_PIDS,
                "Memory": _RUNSC_MEM_BYTES,
                "Tmpfs": dict(_RUNSC_TMPFS),
                "Dns": ["8.8.8.8"],
            },
        )
        record["command"] = command
        record["image"] = image
        record["working_dir"] = working_dir
        return record

    # -- full driver --------------------------------------------------------

    def analyze(
        self,
        workspace_path: str,
        project_id: str,
        framework: str = "php",
        probes: list[str] | None = None,
    ) -> dict[str, Any]:
        """stage → install → probes. One call, partial-failure tolerant.

        Returns {staged, source, install, probes:[...], runtime_image}.
        """
        out: dict[str, Any] = {
            "staged": False,
            "source": None,
            "install": None,
            "probes": [],
            "runtime_image": None,
        }
        rt = self.runtime_for(framework)
        manifest = (rt or {}).get("manifest")
        if manifest:
            rel = _find_manifest_rel(workspace_path, manifest)
            if rel is None:
                out["install"] = {
                    "status": "unavailable",
                    "reason": f"no {manifest} marker under the workspace (app root not found)",
                }
                return out
            self._rel[project_id] = rel
        else:
            self._rel[project_id] = ""
        try:
            src = self.stage_project(workspace_path, project_id)
            out["staged"] = True
            out["source"] = src
        except Exception as exc:  # noqa: BLE001
            get_exception_logger().error("sandbox stage failed: %s", exc)
            out["install"] = {"status": "failed", "error": f"stage: {exc}"[:400]}
            return out

        install = self.install_dependencies(project_id, framework)
        out["install"] = install
        rt = self.runtime_for(framework) or {}
        out["runtime_image"] = rt.get("image")

        if install.get("status") == "done" and install.get("exit_code") == 0:
            for name in probes or list((rt.get("probes") or {}).keys()):
                out["probes"].append(self.run_probe(project_id, name, framework))
        else:
            reason = install.get("reason") or install.get("error") or (
                f"install exit {install.get('exit_code')}"
            )
            out["probes"] = [
                {"name": n, "status": "skipped", "reason": reason}
                for n in (probes or list((rt.get("probes") or {}).keys()))
            ]
        return out


def load_runtime_yml(path: str = CONFIG_PATH) -> dict[str, Any]:
    """Parse sandbox_runtimes.yml (used by tests)."""
    import yaml

    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


_executor: SandboxExecutor | None = None


def default_executor() -> SandboxExecutor:
    """Lazy singleton — constructing DockerApi validates DOCKER_HOST, so
    importing this module must not require a configured daemon."""
    global _executor
    if _executor is None:
        _executor = SandboxExecutor()
    return _executor
