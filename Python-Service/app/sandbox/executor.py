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
import re
import tarfile
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
_RUNSC_TMPFS = {"/tmp": "rw,size=536870912,mode=1777"}  # 512m scratch

# Durable composer cache. Tier-1 installs warm this NAMED VOLUME (dind-storage,
# survives across containers + jobs) with retried --dry-run passes, then
# materialize vendor fully offline. run_command containers mount it too, so
# interactive `composer require` retries reuse the same cache. tmpfs /tmp above
# is NOT where the cache lives anymore (it outgrew 512m on real projects).
_COMPOSER_CACHE_VOLUME = "phpcomposer-cache"
_COMPOSER_CACHE_DIR = "/composer-cache"

# Env injected into every probe: identical to laravel.py _runtime_routes so a
# misbehaving artisan command can only touch an empty in-memory sqlite + array
# drivers — never real project config or a network. LOG_CHANNEL=stderr keeps
# Monolog off the read-only /app mount (runsc probes crash on a locked
# storage/logs file otherwise).
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
    "LOG_CHANNEL": "stderr",
    "COMPOSER_ALLOW_SUPERUSER": "1",
    "HOME": "/tmp",
}

_INSTALL_ENV = {
    "COMPOSER_ALLOW_SUPERUSER": "1",
    "COMPOSER_NO_INTERACTION": "1",
    "HOME": "/tmp",
    "COMPOSER_CACHE_DIR": _COMPOSER_CACHE_DIR,
}

# Noise dirs never staged (macOS zip cruft etc.).
_NOISE_DIRS = {"__MACOSX"}


def jobs_root() -> str:
    return os.environ.get("JOBS_ROOT", "/jobs")


# -- runtime image + installer helpers ---------------------------------------


def _php_constraint_floor(require_php: str | None) -> str | None:
    """First major.minor mentioned by a composer.json ``require.php``.

    ``^8.3`` → ``8.3``, ``>=8.2.5`` → ``8.2``, ``<8.5`` → ``8.5``(first token).
    Used to pick a runtime image matching the project's PHP floor — composer:2
    currently ships PHP 8.5, which real-world Laravel projects (``^8.3`` +
    bcmath/gd) cannot boot on.
    """
    if not require_php:
        return None
    m = re.search(r"\b(\d{1,2})\.(\d{1,2})\b", require_php)
    return f"{m.group(1)}.{m.group(2)}" if m else None


def _detect_php_floor(project_root: Path) -> str | None:
    """Read composer.json require.php from a local project dir (best-effort)."""
    try:
        data = json.loads((project_root / "composer.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return _php_constraint_floor((data.get("require") or {}).get("php"))


def _installer_flags(
    installer: list[str], dev: bool
) -> tuple[str, str, list[str]]:
    """Split an installer argv into (cmd_name, verb, flags).

    ``dev=True`` drops ``--no-dev`` (require-dev packages such as Telescope are
    referenced by real projects' own config/providers and by the tests probe —
    a prod-only install then crashes at boot).
    """
    cmd_name = installer[0] if installer else "composer"
    verb = "install"
    rest: list[str] = list(installer[1:])
    if len(rest) > 0 and rest[0] in (
        "install", "update", "require", "dump-autoload",
    ):
        verb, rest = rest[0], rest[1:]
    if dev:
        rest = [a for a in rest if a != "--no-dev"]
    return cmd_name, verb, rest


def _composer_retry_script(cmd_name: str, verb: str, opts: str) -> str:
    """Nested-DinD DNS drops out under composer's parallel dist downloads (curl
    'Resolving timed out'); composer's ``--dry-run`` never downloads dists, so
    no warm/offline trick helps. What manual remediation proved reliable:

    - HTTP strictly serial (COMPOSER_MAX_PARALLEL_HTTP=1) — single-flight
      requests never hit the resolver storm.
    - Plain networked install, retried up to 4x. The durable cache VOLUME
      (phpcomposer-cache) accumulates downloaded dists across passes AND jobs,
      so pass N+1 resumes where pass N failed — big cold sets converge.

    Shared by tier-1 installs and any composer shell the executor runs.
    """
    return (
        "export COMPOSER_MAX_PARALLEL_HTTP=1; "
        "i=0; rc=1; "
        f"while [ $i -lt 4 ]; do {cmd_name} {verb} {opts}; rc=$?; "
        "[ $rc -eq 0 ] && break; i=$((i+1)); sleep 2; done; "
        "exit $rc"
    )


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
        # project_id -> resolved runtime image (PHP-floor aware) chosen at
        # analyze() time; run_command/run_probe reuse it for staged projects.
        self._runtime_images: dict[str, str] = {}
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

    def resolve_runtime_image(
        self,
        framework: str,
        workspace_path: str | None = None,
        rel: str = "",
    ) -> tuple[str, str | None]:
        """Pick the daemon image for a framework + project.

        Static order (no daemon): runtime ``php_versions`` map keyed by PHP
        floor, matched against the local composer.json require.php → exact
        floor, else nearest lower floor, else the runtime default image.

        Daemon-aware refinement: when the Docker backend answers, the chosen
        image must actually be pulled. Exact-floor images commonly do not exist
        on a fresh DinD (only a subset is pre-loaded) — we then fall back to
        the next available floor image, then the runtime default, and say so
        in ``detail``. ``detail`` explains the choice for honest job reporting.
        """
        rt = self.runtime_for(framework)
        if not rt:
            return "", None
        image = rt.get("image", "")
        versions = rt.get("php_versions") or {}
        if not versions or not workspace_path:
            return image, None
        root = Path(workspace_path)
        if rel:
            root = root / rel
        floor = _detect_php_floor(root)
        if not floor:
            return image, None

        # Preference order: static choice first, remaining floors (desc), then
        # the runtime default as the last resort.
        keys = sorted(
            ((tuple(int(x) for x in v.split("."))), v)
            for v in versions
            if v.count(".") == 1
        )
        target = tuple(int(x) for x in floor.split("."))
        if floor in versions:
            chosen = versions[floor]
            base_detail = f"php {floor}"
        else:
            best = next(
                (v for t, v in reversed(keys) if t < target), None
            )
            if best:
                chosen = versions[best]
                base_detail = f"php {floor} required, {best} image"
            else:
                chosen = image
                base_detail = f"php {floor} required, no matching image — default {image}"

        preference: list[str] = []
        for cand in [chosen, *(versions[k] for _, k in reversed(keys)), image]:
            if cand and cand not in preference:
                preference.append(cand)

        # If the backend answers, honor reality: the picked image must exist.
        try:
            if self.api.image_exists(preference[0]):
                return preference[0], base_detail
            for cand in preference[1:]:
                if self.api.image_exists(cand):
                    return cand, (
                        f"{base_detail} — {preference[0]} unavailable, "
                        f"using {cand} (loaded on daemon)"
                    )
            return preference[0], base_detail
        except Exception:  # noqa: BLE001 — daemon unreachable → static order
            return preference[0], base_detail

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
        self,
        project_id: str,
        framework: str = "php",
        *,
        dev: bool | None = None,
        image: str | None = None,
    ) -> dict[str, Any]:
        """Networked (non-runsc) dependency install on a writable project copy.
        Uses an image the daemon already has; pulls only when egress exists."""
        rt = self.runtime_for(framework)
        if not rt:
            return {"status": "skipped", "reason": f"no runtime config for {framework!r}"}
        image = image or self._runtime_images.get(project_id) or rt.get("image", "")
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
        # dev=None → runtime config knob (real projects need require-dev
        # packages: Telescope etc. are referenced by their own config).
        dev = rt.get("dev", False) if dev is None else dev
        import shlex

        cmd_name, verb, flags = _installer_flags(installer, bool(dev))
        opts = " ".join(shlex.quote(a) for a in flags)
        script = _composer_retry_script(cmd_name, verb, opts)
        src = f"{jobs_root()}/{project_id}/src"
        rel = self._rel.get(project_id, "")
        working_dir = "/app" if not rel else f"/app/{rel}"
        result = self.api.run_once(
            image=image,
            argv=["sh", "-c", script],
            working_dir=working_dir,
            env={**_INSTALL_ENV},
            host_config={
                "Binds": [
                    f"{src}:/app",
                    # Shared cache volume — dry-run warm passes + the offline
                    # final install (and later run_command composer calls) all
                    # accumulate into the same durable named volume.
                    f"{_COMPOSER_CACHE_VOLUME}:{_COMPOSER_CACHE_DIR}",
                ],
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
        image: str | None = None,
    ) -> dict[str, Any]:
        """Run an artisan probe under runsc, network none, read-only mount."""
        rt = self.runtime_for(framework)
        if not rt:
            return {"name": probe_name, "status": "skipped",
                    "reason": f"no runtime config for {framework!r}"}
        image = image or self._runtime_images.get(project_id) or rt.get("image", "")
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
                # Laravel boots into a read-only /app and still writes package
                # manifests (bootstrap/cache/packages.php + services.php) —
                # overlay a writable tmpfs there or every probe dies.
                "Tmpfs": {
                    "/tmp": "rw,size=536870912,mode=1777",
                    "/app/bootstrap/cache": "rw,size=67108864,mode=1777",
                },
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
        image: str | None = None,
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
        image = image or self._runtime_images.get(project_id) or rt.get("image", "")
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
            env={**_PROBE_ENV, "COMPOSER_CACHE_DIR": _COMPOSER_CACHE_DIR},
            host_config={
                "Binds": [
                    f"{src}:/app",
                    f"{_COMPOSER_CACHE_VOLUME}:{_COMPOSER_CACHE_DIR}",
                ],
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
        *,
        install_dev: bool | None = None,
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
            rel = ""
            self._rel[project_id] = rel
        # Choose the daemon image up-front (composer.json require.php floor →
        # runtime php_versions map) so install + probes + later shell commands
        # all run the SAME PHP.
        image, _detail = self.resolve_runtime_image(framework, workspace_path, rel)
        if image:
            self._runtime_images[project_id] = image
        try:
            src = self.stage_project(workspace_path, project_id)
            out["staged"] = True
            out["source"] = src
        except Exception as exc:  # noqa: BLE001
            get_exception_logger().error("sandbox stage failed: %s", exc)
            out["install"] = {"status": "failed", "error": f"stage: {exc}"[:400]}
            return out

        install = self.install_dependencies(
            project_id, framework, dev=install_dev, image=image
        )
        out["install"] = install
        out["runtime_image"] = image or (rt or {}).get("image")

        if install.get("status") == "done" and install.get("exit_code") == 0:
            for name in probes or list((rt.get("probes") or {}).keys()):
                # C6: one crashing probe never cancels the remaining probes.
                try:
                    out["probes"].append(
                        self.run_probe(project_id, name, framework, image=image)
                    )
                except Exception as exc:  # noqa: BLE001 — probe isolation
                    out["probes"].append(
                        {
                            "name": name,
                            "status": "failed",
                            "error": str(exc)[:300],
                        }
                    )
        else:
            reason = install.get("reason") or install.get("error") or (
                f"install exit {install.get('exit_code')}"
            )
            out["probes"] = [
                {"name": n, "status": "skipped", "reason": reason}
                for n in (probes or list((rt.get("probes") or {}).keys()))
            ]
        return out

    def cleanup_staging(self, project_id: str) -> dict[str, Any]:
        """Delete the daemon-side staged project dir (cons F17).

        The nested DinD volume accumulates one ``/jobs/<pid>/src`` tree per
        analysis; the orchestrator no longer auto-removes it so shell sessions
        stay usable — an explicit cleanup (UI button / endpoint) reclaims it.
        """
        if not project_id or "/" in project_id or ".." in Path(project_id).parts:
            return {"status": "skipped", "reason": f"unsafe project_id: {project_id!r}"}
        try:
            self.api.run_once(
                image="alpine:3.20.3",
                argv=["sh", "-c", f"rm -rf '{jobs_root()}/{project_id}'"],
                working_dir="/",
                host_config={"Binds": [f"{jobs_root()}:/jobs"]},
            )
            self._rel.pop(project_id, None)
            self._runtime_images.pop(project_id, None)
            return {"status": "done", "staging": f"{jobs_root()}/{project_id}"}
        except Exception as exc:  # noqa: BLE001 — cleanup is best-effort
            return {"status": "failed", "error": str(exc)[:300]}


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
