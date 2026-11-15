"""Static Laravel behaviors extractor (jobs / events / listeners / schedule).

Answers "what happens after X?", "are there scheduled jobs?":
  * jobs           — app/Jobs/*.php classes (ShouldQueue => queueable)
  * events         — app/Events/*.php
  * listeners      — app/Listeners/*.php (+ which event the handle() hints at)
  * notifications  — app/Notifications/*.php
  * schedule       — entries declared in routes/console.php via Schedule facade
                     (->command(...)->daily(), ->call(...)->everyMinute(), …)
  * dispatch sites — files that dispatch(new SomeJob/Event) — count per target

Static only; no runtime. Missing directories are reported as absent lists, not
errors.
"""

from __future__ import annotations

import re
from typing import Any

_CLASS_RE = re.compile(r"class\s+(\w+)(?:\s+extends\s+[\\\w]+)?")
_SHOULD_QUEUE_RE = re.compile(r"implements\s+([\w,\s\\]+)")
_QUEUEABLE_RE = re.compile(r"ShouldQueue")
_HANDLE_PARAM_RE = re.compile(
    r"function\s+handle\s*\(\s*([\w\\]+)\s+\$\w+"
)
_SCHEDULE_RE = re.compile(
    r"Schedule::\s*(command|call|job|exec)\s*\((.*?)\)\s*->\s*([\w]+)\s*\(",
    re.DOTALL,
)
_COMMAND_STR_RE = re.compile(r"['\"]([^'\"]+)['\"]")
_DISPATCH_RE = re.compile(r"dispatch\s*\(\s*new\s+([\w\\]+)\s*[,(]")
_EVENT_DISPATCH_RE = re.compile(r"event\s*\(\s*new\s+([\w\\]+)\s*[,(]")

_ARTIFACT_DIRS = {
    "jobs": "app/Jobs",
    "events": "app/Events",
    "listeners": "app/Listeners",
    "notifications": "app/Notifications",
}


def _classes_in(
    files: list[tuple[str, str]], prefix: str
) -> list[dict[str, Any]]:
    """Inventory classes under one app/ dir (relative path prefix)."""
    out: list[dict[str, Any]] = []
    for rel, src in files:
        if not rel.startswith(prefix + "/"):
            continue
        # only depth-1 files (skip subdirs) — keep flat inventory
        rest = rel[len(prefix) + 1 :]
        if "/" in rest:
            continue
        cm = _CLASS_RE.search(src)
        out.append(
            {
                "name": cm.group(1) if cm else rest.replace(".php", ""),
                "file": rel,
            }
        )
    return out


def extract_behaviors(
    files: list[tuple[str, str]], console_routes_src: str = ""
) -> dict[str, Any]:
    """Aggregate behaviors from an app/ file tuple list.

    ``files`` = [(relative_path, source)] under the framework root;
    ``console_routes_src`` = routes/console.php content when present.
    """
    jobs = _classes_in(files, _ARTIFACT_DIRS["jobs"])
    for j in jobs:
        src = next((s for n, s in files if n == j["file"]), "")
        j["queueable"] = bool(_QUEUEABLE_RE.search(src))
    events = _classes_in(files, _ARTIFACT_DIRS["events"])
    listeners = _classes_in(files, _ARTIFACT_DIRS["listeners"])
    for lsn in listeners:
        src = next((s for n, s in files if n == lsn["file"]), "")
        hm = _HANDLE_PARAM_RE.search(src)
        lsn["handles"] = hm.group(1).split("\\")[-1] if hm else None
    notifications = _classes_in(files, _ARTIFACT_DIRS["notifications"])

    schedule: list[dict[str, Any]] = []
    if console_routes_src:
        for sm in _SCHEDULE_RE.finditer(console_routes_src):
            task = sm.group(2).strip()
            tm = _COMMAND_STR_RE.search(task)
            schedule.append(
                {
                    "task": tm.group(1) if tm else f"{sm.group(1)}(...)",
                    "kind": sm.group(1),
                    "frequency": sm.group(3),
                }
            )

    # dispatch/event call sites across app code (cap noise: count by target)
    dispatch_targets: dict[str, int] = {}
    event_targets: dict[str, int] = {}
    for rel, src in files:
        if not rel.startswith("app/") or rel.endswith(".blade.php"):
            continue
        for dm in _DISPATCH_RE.finditer(src):
            t = dm.group(1).split("\\")[-1]
            dispatch_targets[t] = dispatch_targets.get(t, 0) + 1
        for em in _EVENT_DISPATCH_RE.finditer(src):
            t = em.group(1).split("\\")[-1]
            event_targets[t] = event_targets.get(t, 0) + 1

    return {
        "jobs": jobs,
        "events": events,
        "listeners": listeners,
        "notifications": notifications,
        "schedule": schedule,
        "dispatch_sites": dispatch_targets,
        "event_sites": event_targets,
    }
