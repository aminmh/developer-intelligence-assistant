"""Ephemeral SQL tagging support for evaluator runs.

The hook is written to the sandbox's staged copy only. Uploaded workspace
files are never edited, and every successful injection has an explicit cleanup
operation. Executors that do not expose the optional injection capability
return a structured unavailable result.
"""

from __future__ import annotations

import base64
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


HOOK_SOURCE = """<?php
// Evaluator-only SQL tagger. Loaded through the sandbox runtime, never tracked.
if (class_exists('Illuminate\\Database\\Capsule\\Manager')) {
    // Framework adapters may register a listener here; the core only carries
    // the bounded marker contract and does not assume a framework API.
}
"""


@dataclass
class HookResult:
    status: str
    path: str | None = None
    reason: str | None = None


class EphemeralSqlTagger:
    def __init__(self, *, source: str = HOOK_SOURCE) -> None:
        self.source = source
        self._temp: tempfile.TemporaryDirectory[str] | None = None
        self._project_id: str | None = None

    def prepare(self) -> HookResult:
        if not self.source or len(self.source.encode("utf-8")) > 64 * 1024:
            return HookResult("unavailable", reason="hook source is empty or exceeds 64 KiB")
        self._temp = tempfile.TemporaryDirectory(prefix="evaluator-hook-")
        path = Path(self._temp.name) / "sql_tagger.php"
        path.write_text(self.source, encoding="utf-8")
        return HookResult("ready", path=str(path))

    def inject(self, executor: Any, project_id: str) -> HookResult:
        if self._temp is None:
            prepared = self.prepare()
            if prepared.status != "ready":
                return prepared
        method = getattr(executor, "inject_evaluator_hook", None)
        if not callable(method):
            return HookResult("unavailable", reason="sandbox executor does not support evaluator hooks")
        try:
            result = method(project_id, self.source)
        except Exception as exc:  # noqa: BLE001 - evaluator availability is explicit
            return HookResult("unavailable", reason=str(exc)[:300])
        self._project_id = project_id
        if isinstance(result, dict) and result.get("status") not in ("done", "ready", "available"):
            return HookResult("unavailable", reason=str(result.get("reason") or result.get("error") or "hook injection failed")[:300])
        return HookResult("injected", path="/app/.evaluator/sql_tagger.php")

    def cleanup(self, executor: Any) -> HookResult:
        if self._project_id:
            method = getattr(executor, "remove_evaluator_hook", None)
            if callable(method):
                try:
                    method(self._project_id)
                except Exception as exc:  # noqa: BLE001 - cleanup remains best-effort
                    return HookResult("cleanup_failed", reason=str(exc)[:300])
        self._project_id = None
        if self._temp is not None:
            self._temp.cleanup()
            self._temp = None
        return HookResult("cleaned")

    def __enter__(self) -> "EphemeralSqlTagger":
        self.prepare()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._temp is not None:
            self._temp.cleanup()
            self._temp = None


def encode_hook(source: str = HOOK_SOURCE) -> str:
    """Return a transport-safe payload for Docker exec/archive adapters."""
    return base64.b64encode(source.encode("utf-8")).decode("ascii")
