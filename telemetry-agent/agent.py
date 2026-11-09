"""Opt-in Docker deployment reporter. Raw Docker objects stay on this host."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import ssl
import threading
import time
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import HTTPSHandler, Request, build_opener

LABELS = {
    "enabled": "com.devassistant.telemetry.enabled",
    "project": "com.devassistant.telemetry.project",
    "environment": "com.devassistant.telemetry.environment",
    "commit": "com.devassistant.telemetry.commit",
    "service": "com.devassistant.telemetry.service",
}
SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
PROXY = "http://docker-api-proxy:2375"
# Every field the agent reads exists unchanged from API 1.41 on; prefer a version every
# current engine accepts and fall back to the engine's own range when it does not.
PREFERRED_API = (1, 44)
RECONCILE_INTERVAL = 60
DEBOUNCE_SECONDS = 1.5
# Docker's event stream is silent while nothing changes; reconnect rather than hang forever.
EVENT_READ_TIMEOUT = 300

log = logging.getLogger("telemetry-agent")


def selected(container: dict) -> dict | None:
    labels = container.get("Labels")
    if not isinstance(labels, dict) or labels.get(LABELS["enabled"]) != "true":
        return None
    project, environment, commit = (labels.get(LABELS[k]) for k in ("project", "environment", "commit"))
    service = labels.get(LABELS["service"])
    container_id, image_id = container.get("Id"), container.get("ImageID")
    if (not isinstance(project, str) or not SLUG.fullmatch(project)
            or not isinstance(environment, str) or not SLUG.fullmatch(environment)
            or not isinstance(commit, str) or not SHA.fullmatch(commit)
            or not isinstance(service, str) or not SLUG.fullmatch(service)
            or not isinstance(container_id, str) or not re.fullmatch(r"[0-9a-f]{64}", container_id)
            or not isinstance(image_id, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id)):
        return None
    return {"container_id": container_id, "project_id": project, "environment": environment,
            "service": service, "commit_sha": commit, "image_id": image_id, "state": "running"}


def _version(value) -> tuple[int, int] | None:
    match = re.fullmatch(r"(\d{1,3})\.(\d{1,3})", value) if isinstance(value, str) else None
    return (int(match[1]), int(match[2])) if match else None


def negotiate_api_version(info: dict) -> str:
    """Pick PREFERRED_API clamped to the engine's [MinAPIVersion, ApiVersion] range."""
    chosen = PREFERRED_API
    newest, oldest = _version(info.get("ApiVersion")), _version(info.get("MinAPIVersion"))
    if newest and chosen > newest:
        chosen = newest
    if oldest and chosen < oldest:
        chosen = oldest
    return f"v{chosen[0]}.{chosen[1]}"


def event_identity(event: dict) -> str:
    return hashlib.sha256((str(event.get("timeNano")) + str(event.get("id", "")) + str(event.get("Action", ""))).encode()).hexdigest()


class Reporter:
    def __init__(self):
        context = ssl.create_default_context(cafile=os.environ["BACKEND_CA"])
        context.load_cert_chain(os.environ["HOST_CERT"], os.environ["HOST_KEY"])
        self.opener = build_opener(HTTPSHandler(context=context))
        self.backend = os.environ["BACKEND_URL"].rstrip("/") + "/telemetry/host/deployments"
        self.api_version: str | None = None
        self.dirty = threading.Event()
        self.lock = threading.Lock()
        self.pending_event_id: str | None = None

    def docker_get(self, path, timeout=30):
        with self.opener.open(PROXY + path, timeout=timeout) as response:
            return response.read()

    def api(self) -> str:
        if self.api_version is None:
            self.api_version = negotiate_api_version(json.loads(self.docker_get("/version")))
            log.info("docker_api_version_selected version=%s", self.api_version)
        return self.api_version

    def inventory(self):
        # The proxy applies the opt-in label filter itself and returns only telemetry labels.
        raw = json.loads(self.docker_get(f"/{self.api()}/containers/json"))
        return [record for record in (selected(item) for item in raw) if record]

    def report(self, containers, inventory=True, event_id=None):
        body = json.dumps({"containers": containers, "inventory": inventory, "event_id": event_id}).encode()
        request = Request(self.backend, body, {"Content-Type": "application/json"}, method="POST")
        with self.opener.open(request, timeout=15) as response:
            response.read()

    def reconcile(self, event_id=None):
        self.report(self.inventory(), inventory=True, event_id=event_id)

    def mark_dirty(self, event_id: str | None = None):
        with self.lock:
            if event_id:
                self.pending_event_id = event_id
        self.dirty.set()

    def watch_events(self):
        cursor = int(time.time())
        while True:
            try:
                query = urlencode({"since": cursor})
                with self.opener.open(f"{PROXY}/{self.api()}/events?{query}", timeout=EVENT_READ_TIMEOUT) as response:
                    for line in response:
                        if not line.strip():
                            continue
                        event = json.loads(line)
                        stamp = event.get("time")
                        if isinstance(stamp, int):
                            cursor = max(cursor, stamp)
                        # The proxy forwards only lifecycle events of opted-in containers.
                        self.mark_dirty(event_identity(event))
            except TimeoutError:
                continue  # idle stream; `since` replays anything missed while reconnecting
            except Exception as exc:  # noqa: BLE001 — the watcher must never die silently
                log.warning("event_stream_failed error_type=%s", type(exc).__name__)
                self.api_version = None
                # Events may have been missed while the stream was down.
                self.mark_dirty()
                time.sleep(3)

    def run_reconciler(self):
        failing, backoff = False, 3
        while True:
            self.dirty.wait(RECONCILE_INTERVAL)
            if self.dirty.is_set():
                time.sleep(DEBOUNCE_SECONDS)  # coalesce bursts such as `docker compose up`
            with self.lock:
                self.dirty.clear()
                event_id, self.pending_event_id = self.pending_event_id, None
            try:
                self.reconcile(event_id)
                if failing:
                    log.info("reconcile_recovered")
                failing, backoff = False, 3
            except Exception as exc:  # noqa: BLE001 — keep reporting; log without request data
                if not failing:
                    log.warning("reconcile_failed error_type=%s", type(exc).__name__)
                failing = True
                if isinstance(exc, URLError) and not hasattr(exc, "code"):
                    self.api_version = None
                # Retry with backoff instead of waiting for the next periodic reconcile.
                self.mark_dirty(event_id)
                time.sleep(backoff)
                backoff = min(backoff * 2, RECONCILE_INTERVAL)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    reporter = Reporter()
    reporter.dirty.set()  # reconcile at startup
    threading.Thread(target=reporter.watch_events, daemon=True).start()
    reporter.run_reconciler()


if __name__ == "__main__":
    main()
