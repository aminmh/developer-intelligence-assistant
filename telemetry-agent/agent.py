"""Opt-in Docker deployment reporter. Raw Docker objects stay on this host."""
from __future__ import annotations

import hashlib
import json
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


class Reporter:
    def __init__(self):
        context = ssl.create_default_context(cafile=os.environ["BACKEND_CA"])
        context.load_cert_chain(os.environ["HOST_CERT"], os.environ["HOST_KEY"])
        self.opener = build_opener(HTTPSHandler(context=context))
        self.backend = os.environ["BACKEND_URL"].rstrip("/") + "/telemetry/host/deployments"

    def docker_get(self, path):
        with self.opener.open("http://docker-api-proxy:2375" + path, timeout=90) as response:
            return response.read()

    def inventory(self):
        filters = json.dumps({"label": [LABELS["enabled"] + "=true"]}, separators=(",", ":"))
        path = "/v1.41/containers/json?" + urlencode({"filters": filters})
        raw = json.loads(self.docker_get(path))
        return [record for record in (selected(item) for item in raw) if record]

    def report(self, containers, inventory=True, event_id=None):
        body = json.dumps({"containers": containers, "inventory": inventory, "event_id": event_id}).encode()
        request = Request(self.backend, body, {"Content-Type": "application/json"}, method="POST")
        with self.opener.open(request, timeout=15) as response:
            response.read()

    def reconcile(self, event_id=None):
        self.report(self.inventory(), inventory=True, event_id=event_id)

    def watch_events(self):
        cursor = int(time.time())
        while True:
            try:
                query = urlencode({"since": cursor, "filters": json.dumps({"type": ["container"]})})
                with self.opener.open("http://docker-api-proxy:2375/v1.41/events?" + query) as response:
                    for line in response:
                        if not line.strip():
                            continue
                        event = json.loads(line)
                        cursor = max(cursor, int(event.get("time", cursor)))
                        attrs = event.get("Actor", {}).get("Attributes", {})
                        # Ignore non-opted-in events; inventory reconciles stops and starts.
                        if attrs.get(LABELS["enabled"]) == "true":
                            identity = hashlib.sha256((str(event.get("timeNano")) + event.get("id", "") + event.get("Action", "")).encode()).hexdigest()
                            self.reconcile(identity)
            except (URLError, OSError, ValueError, KeyError):
                time.sleep(3)


def main():
    reporter = Reporter()
    threading.Thread(target=reporter.watch_events, daemon=True).start()
    while True:
        try:
            reporter.reconcile()
        except (URLError, OSError, ValueError):
            pass
        time.sleep(60)


if __name__ == "__main__":
    main()
