"""Progress of uploaded-archive indexing jobs, read from the per-job status files in the download directory."""
from __future__ import annotations

import json
import time
from pathlib import Path

from infra.settings import settings

# Progress is written after every batch; a running job silent this long died with its process
# (for example a service restart), so it is reported as failed instead of "indexing" forever.
SILENCE_SECONDS = 300
INTERRUPTED = "Indexing was interrupted (the service restarted or stopped). Start indexing again."


def flag_interrupted(status: dict, path: Path) -> dict:
    if status.get("status") in {"done", "error", "unknown"}:
        return status
    try:
        silent = time.time() - path.stat().st_mtime
    except OSError:
        return status
    if silent > SILENCE_SECONDS:
        return {**status, "status": "error", "stage": "error", "error": INTERRUPTED}
    return status


def latest_for_collection(collection: str) -> dict | None:
    """Newest job status (with its ``download_id``) whose target collection is ``collection``."""
    newest, newest_time = None, 0.0
    for path in Path(settings.download_dir).glob("*.status.json"):
        try:
            mtime = path.stat().st_mtime
            if mtime <= newest_time:
                continue
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if data.get("collection_name") == collection:
            newest = {"download_id": path.name[: -len(".status.json")], **flag_interrupted(data, path)}
            newest_time = mtime
    return newest
