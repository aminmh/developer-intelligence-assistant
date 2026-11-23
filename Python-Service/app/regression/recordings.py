"""Replay recordings in app storage, never in telemetry.

Decision: @.archcore/regression/replay-recordings-stay-out-of-telemetry.adr.md. One JSON file per
probe and pass under ``<snapshot root>/regression_recordings/<project>/<check>/<run>/``. A body over
``MAX_BODY_BYTES``, or one that would push the check past ``MAX_CHECK_BYTES``, is kept as its
SHA-256 only (spec failure clause 6). Recordings older than ``RETENTION_DAYS`` are purged, and a
check's recordings are deleted with the check.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import time
from hashlib import sha256
from pathlib import Path
from typing import Any

MAX_BODY_BYTES = 1024 * 1024
MAX_CHECK_BYTES = 50 * 1024 * 1024
RETENTION_DAYS = 30
_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def _safe(value: str, name: str) -> str:
    if not isinstance(value, str) or not _ID.match(value) or value in (".", ".."):
        raise ValueError(f"Invalid {name}.")
    return value


def _project_dir(project_id: str) -> str:
    return sha256(project_id.encode("utf-8")).hexdigest()[:32]


def _default_root() -> Path:
    from infra.settings import settings

    return Path(os.environ.get("TELEMETRY_SNAPSHOT_ROOT") or Path(settings.workspace_root).parent / "snapshots") \
        / "regression_recordings"


class Recordings:
    def __init__(self, root: Path | str | None = None):
        self.root = Path(root) if root else _default_root()

    def _check_dir(self, project_id: str, check_id: str) -> Path:
        return self.root / _project_dir(project_id) / _safe(check_id, "check id")

    def _file(self, project_id: str, check_id: str, run_id: str, probe_id: str, pass_no: int) -> Path:
        return (self._check_dir(project_id, check_id) / _safe(run_id, "run id")
                / f"{_safe(probe_id, 'probe id')}.{int(pass_no)}.json")

    def usage(self, project_id: str, check_id: str) -> int:
        folder = self._check_dir(project_id, check_id)
        return sum(path.stat().st_size for path in folder.rglob("*.json")) if folder.exists() else 0

    def save(self, project_id: str, check_id: str, run_id: str, probe_id: str, pass_no: int,
             response: dict[str, Any]) -> dict[str, Any]:
        """Store one response; returns its metadata (what the report keeps)."""
        body = response.get("body")
        digest = response.get("body_sha256") or (sha256(body).hexdigest() if body else None)
        keep = bool(body) and not response.get("truncated") and len(body) <= MAX_BODY_BYTES \
            and self.usage(project_id, check_id) + len(body) * 4 // 3 <= MAX_CHECK_BYTES
        meta = {"status": response.get("status"), "headers": response.get("headers") or {},
                "error": response.get("error"), "body_sha256": digest, "size": response.get("size", len(body or b"")),
                "truncated": bool(body) and not keep or bool(response.get("truncated")),
                "elapsed_ms": response.get("elapsed_ms")}
        record = {**meta, "body_b64": base64.b64encode(body).decode("ascii") if keep else None}
        path = self._file(project_id, check_id, run_id, probe_id, pass_no)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record), encoding="utf-8")
        return meta

    def load(self, project_id: str, check_id: str, run_id: str, probe_id: str, pass_no: int) -> dict[str, Any] | None:
        path = self._file(project_id, check_id, run_id, probe_id, pass_no)
        if not path.is_file():
            return None
        record = json.loads(path.read_text(encoding="utf-8"))
        encoded = record.pop("body_b64", None)
        record["body"] = base64.b64decode(encoded) if encoded else None
        return record

    def delete_check(self, project_id: str, check_id: str) -> None:
        shutil.rmtree(self._check_dir(project_id, check_id), ignore_errors=True)

    def purge(self, older_than_days: int = RETENTION_DAYS, now: float | None = None) -> int:
        """Remove check folders untouched for ``older_than_days``; returns how many."""
        if not self.root.exists():
            return 0
        cutoff = (now or time.time()) - older_than_days * 86400
        removed = 0
        for project in self.root.iterdir():
            for check in project.iterdir() if project.is_dir() else []:
                newest = max((p.stat().st_mtime for p in check.rglob("*")), default=check.stat().st_mtime)
                if newest < cutoff:
                    shutil.rmtree(check, ignore_errors=True)
                    removed += 1
        return removed
