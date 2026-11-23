"""Coverage map, route verdicts, partial evidence, tier and baseline checklist.

Contract: @.archcore/regression/regression-check.spec.md clauses 6-9. Reads only normalized
records (impact rows, ``TestRecord``, ``RunHintRecord``, capability coverage) and plain project
files; never names a language or framework.
"""

from __future__ import annotations

import re
from typing import Any

from regression.model import (CHANGED_EXPECTED, NOT_COVERED, SUSPECTED_REGRESSION, TIER_REPLAY, TIER_STATIC,
                              TIER_TELEMETRY, UNCHANGED)

# Worst first: a route takes the worst verdict among the changed symbols it depends on.
_SEVERITY = {SUSPECTED_REGRESSION: 0, NOT_COVERED: 1, CHANGED_EXPECTED: 2, UNCHANGED: 3}
NO_DYNAMIC_EVIDENCE = "no test, runtime span or replay result"

# What each capability gap takes away from the report (clause 7).
AFFECTS = {
    "route_analysis": "routes handled by changed code may be missing",
    "test_conventions": "tests that reach changed routes are not counted as coverage",
    "run_hints": "the baseline checklist has no framework-specific steps",
}

# Plain files any project may have; read as data for the baseline checklist (clause 9).
NEUTRAL_FILES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml", ".env.example",
                 "Dockerfile", "package.json", "composer.json", "pyproject.toml", "requirements.txt", "go.mod",
                 "pom.xml", "build.gradle", "Gemfile", "Procfile")


def _worst(verdicts: list[str | None]) -> str:
    known = [v if v in _SEVERITY else NOT_COVERED for v in verdicts]
    return min(known, key=_SEVERITY.__getitem__) if known else NOT_COVERED


def _changed_ends(fqn: str, changed: set[str], impacted: list[dict[str, Any]]) -> set[str]:
    """Changed symbols ``fqn`` depends on: itself when changed, else the end of each via path."""
    if fqn in changed:
        return {fqn}
    return {row["via"][-1] for row in impacted if row.get("fqn") == fqn and row.get("via")
            and row["via"][-1] in changed}


def _tested_fqns(report: dict[str, Any]) -> set[str]:
    """Changed symbols a test reaches through the graph (impact stops its walk at tests)."""
    return {row["via"][-1] for row in report.get("impacted", []) if row.get("test") and row.get("via")}


def _test_reaches(route: dict[str, Any], tests: list[Any]) -> bool:
    from code_quality.testing_checks import reaches

    handler_class = (route.get("fqn") or "").split("::", 1)[0].lstrip("\\")
    for record in tests:
        for reference in getattr(record, "references", ()) or ():
            if reference.startswith("/") and reaches(route.get("path", ""), reference):
                return True
            if handler_class and reference.lstrip("\\") == handler_class:
                return True
    return False


def apply_coverage(report: dict[str, Any], *, tests: list[Any] | None = None) -> None:
    """Clause 6: route verdicts from their changed symbols, and the uncovered list."""
    tests = tests or []
    symbols = report.get("symbols", [])
    by_fqn = {row["fqn"]: row for row in symbols}
    changed = set(by_fqn)
    impacted = report.get("impacted", [])
    tested = _tested_fqns(report)
    uncovered: list[dict[str, Any]] = []
    covered_symbols: set[str] = set()
    for route in report.get("routes", []):
        ends = _changed_ends(route.get("fqn", ""), changed, impacted)
        route["depends_on"] = sorted(ends)
        if route.get("verdict") is None:
            route["verdict"] = _worst([by_fqn[fqn].get("verdict") for fqn in ends])
        dynamic = []
        if route.get("source") == "runtime":
            dynamic.append("runtime")
        if _test_reaches(route, tests) or ends & tested:
            dynamic.append("test")
        route["coverage"] = dynamic
        if dynamic:
            covered_symbols |= ends
        else:
            uncovered.append({"kind": "route", "name": f"{route.get('method', '')} {route.get('path', '')}".strip(),
                              "path": route.get("handler_path", ""), "line": route.get("handler_line"),
                              "verdict": NOT_COVERED, "reason": NO_DYNAMIC_EVIDENCE})
    for row in symbols:
        if row.get("verdict") is None:
            row["verdict"] = NOT_COVERED
        dynamic = sorted({"test"} if row["fqn"] in tested else set()) + (
            ["route"] if row["fqn"] in covered_symbols else [])
        row["coverage"] = dynamic
        if not dynamic and row.get("change") != "removed":
            uncovered.append({"kind": "symbol", "name": row["fqn"], "path": row.get("path", ""),
                              "line": row.get("line"), "verdict": NOT_COVERED, "reason": NO_DYNAMIC_EVIDENCE})
    report["uncovered"] = uncovered


def partial_from(collections: dict[Any, Any]) -> list[dict[str, Any]]:
    """Clause 7: one row per capability not fully provided, naming what it takes away."""
    rows = []
    for capability, collection in sorted(collections.items(), key=lambda item: getattr(item[0], "value", str(item[0]))):
        coverage = collection.coverage()
        if coverage["status"] == "complete":
            continue
        name = coverage["capability"]
        reasons = [gap.get("reason", "") for gap in coverage.get("gaps") or []]
        if not coverage.get("detected_frameworks"):
            reasons.append("No framework plugin was detected for this revision.")
        rows.append({"capability": name, "status": coverage["status"],
                     "affects": AFFECTS.get(name, "related evidence is incomplete"), "reasons": reasons})
    return rows


def tier_for(report: dict[str, Any], *, replay_results: bool = False) -> str:
    """Clause 8: the evidence tier the check reached."""
    if replay_results and report.get("runtime_observed"):
        return TIER_TELEMETRY
    if replay_results:
        return TIER_REPLAY
    return TIER_STATIC


def _services(compose: str) -> list[str]:
    """Top-level service names of a compose file, read as text (no YAML execution)."""
    names, inside = [], False
    for line in compose.splitlines():
        if re.match(r"^services\s*:", line):
            inside = True
            continue
        if inside and re.match(r"^\S", line):
            break
        match = re.match(r"^ {2}([A-Za-z0-9_.-]+)\s*:", line) if inside else None
        if match:
            names.append(match.group(1))
    return names


def baseline_checklist(files: dict[str, str], hints: list[Any] | None = None) -> list[dict[str, Any]]:
    """Clause 9: steps to start a version, from plugin run hints and plain project files."""
    items: list[dict[str, Any]] = []
    for hint in hints or []:
        items.append({"kind": hint.kind, "name": hint.name, "detail": hint.detail, "file": hint.file,
                      "line": hint.line, "source": hint.framework})
    seen = {(item["kind"], item["name"]) for item in items}

    def add(kind: str, name: str, detail: str, file: str, line: int | None = None) -> None:
        if (kind, name) not in seen:
            seen.add((kind, name))
            items.append({"kind": kind, "name": name, "detail": detail, "file": file, "line": line,
                          "source": "project files"})

    for name in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
        if name in files:
            add("command", "docker compose up", "Start the services this compose file defines.", name)
            for service in _services(files[name]):
                add("service", service, "Service defined in the compose file.", name)
            break
    if "Dockerfile" in files:
        for number, line in enumerate(files["Dockerfile"].splitlines(), start=1):
            match = re.match(r"^\s*EXPOSE\s+(\d+)", line, re.I)
            if match:
                add("port", match.group(1), "Port the container exposes.", "Dockerfile", number)
    if ".env.example" in files:
        for number, line in enumerate(files[".env.example"].splitlines(), start=1):
            key = line.split("=", 1)[0].strip()
            if re.fullmatch(r"[A-Z][A-Z0-9_]*", key) and "=" in line and not line.split("=", 1)[1].strip():
                add("env", key, "Empty in .env.example; set it before starting.", ".env.example", number)
    manifests = [name for name in NEUTRAL_FILES if name in files and name not in (".env.example", "Dockerfile")
                 and not name.startswith(("docker-compose", "compose"))]
    for name in manifests:
        add("note", name, "Install dependencies declared here.", name)
    add("note", "same data", "Start base and candidate with the same database contents so responses compare.", "")
    return items
