#!/usr/bin/env python3
"""Upload a Git archive to the local API and wait for snapshot readiness."""

from __future__ import annotations

import json
import errno
import os
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


def main() -> int:
    if len(sys.argv) not in {3, 4} or (len(sys.argv) == 4 and sys.argv[3] != "--no-wait"):
        print("Expected project ID, full Git revision and optional --no-wait.", file=sys.stderr)
        return 2

    project_id, commit_sha = sys.argv[1:3]
    # --no-wait: return right after the upload with the revision ID, so the caller can configure and
    # start the matching deployment that the snapshot is waiting for.
    no_wait = len(sys.argv) == 4
    try:
        credentials = json.loads(os.environ.get("TELEMETRY_CI_PROJECT_TOKENS", "{}"))
        token = credentials.get(project_id) if isinstance(credentials, dict) else None
    except (ValueError, TypeError):
        token = None
    if not isinstance(token, str) or len(token) < 32:
        print("Project upload token is missing or invalid in the API container.", file=sys.stderr)
        return 2

    base = "http://127.0.0.1:9002/telemetry/projects/{}/snapshots/{}".format(
        quote(project_id, safe=""), commit_sha
    )
    headers = {"Authorization": "Bearer " + token}

    def call(method: str, body: bytes | None = None) -> dict:
        request_headers = dict(headers)
        if method == "PUT":
            request_headers["Content-Type"] = "application/zip"
        for attempt in range(1, 11):
            request = Request(
                base + ("/source" if method == "PUT" else ""),
                data=body,
                headers=request_headers,
                method=method,
            )
            try:
                with urlopen(request, timeout=60) as response:
                    payload = response.read()
                try:
                    return json.loads(payload)
                except (UnicodeDecodeError, ValueError) as error:
                    print("The local source indexing API returned invalid JSON: {}".format(error), file=sys.stderr)
                    raise SystemExit(2) from error
            except HTTPError as error:
                detail = error.read().decode("utf-8", errors="replace")[:1000]
                print("API rejected source snapshot (HTTP {}): {}".format(error.code, detail), file=sys.stderr)
                raise SystemExit(2) from error
            except (URLError, TimeoutError, OSError) as error:
                reason = getattr(error, "reason", error)
                reason_code = getattr(reason, "errno", None)
                transient_errnos = {
                    errno.ECONNREFUSED,
                    errno.ETIMEDOUT,
                    errno.EHOSTUNREACH,
                    errno.ENETUNREACH,
                    errno.ECONNRESET,
                    errno.EPIPE,
                }
                if reason_code not in transient_errnos or attempt == 10:
                    print("Could not reach the local source indexing API: {}".format(error), file=sys.stderr)
                    raise SystemExit(2) from error
                if attempt == 1:
                    print("The local API is starting; retrying the connection briefly.", flush=True)
                time.sleep(1)

    archive = sys.stdin.buffer.read()
    if not archive:
        print("Git produced an empty source archive.", file=sys.stderr)
        return 2

    result = call("PUT", archive)
    state = result.get("state", "unknown")
    if no_wait:
        print(json.dumps({"state": state, "revision_id": result.get("revision_id")}), flush=True)
        return 0
    print("Source snapshot state: {}".format(state), flush=True)
    if state == "ready":
        print("Ready immutable revision: {}".format(result.get("revision_id") or "available"), flush=True)
        return 0

    if state == "awaiting_deployment":
        print(
            "Waiting for a running deployment with this project ID and exact source revision. "
            "Check the Compose telemetry labels and local host agent setup.",
            flush=True,
        )

    deadline = time.monotonic() + 600
    while state in {"awaiting_deployment", "queued", "indexing"} and time.monotonic() < deadline:
        time.sleep(3)
        result = call("GET")
        next_state = result.get("state", "unknown")
        if next_state != state:
            print("Source snapshot state: {}".format(next_state), flush=True)
            state = next_state
        if state == "ready":
            print("Ready immutable revision: {}".format(result.get("revision_id") or "available"), flush=True)
            return 0

    if state == "awaiting_deployment":
        print(
            "The source is saved, but no matching live deployment was reported within 10 minutes. "
            "Start the matching Compose project and telemetry host agent, then rerun just php-otel-index.",
            file=sys.stderr,
        )
    elif state in {"queued", "indexing"}:
        print(
            "Indexing is still in progress. Rerun just php-otel-index to check again; "
            "the same source revision is safe to retry.",
            file=sys.stderr,
        )
    elif state == "failed":
        print(
            "Indexing failed. Check the API logs, fix the reported issue, "
            "then rerun just php-otel-index to retry this source revision.",
            file=sys.stderr,
        )
    else:
        print(
            "Snapshot did not become ready (state: {}). Retry just php-otel-index or check the API logs.".format(state),
            file=sys.stderr,
        )
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
