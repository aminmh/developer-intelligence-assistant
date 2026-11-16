"""Process evidence: CI, container builds and toolchain versions (gap-closing plan, task 15).

Framework-neutral and read-only (evidence spec clause 3). Reports:

* each CI pipeline and container build that exists (info), so a healthy process has citeable evidence;
* no CI configuration at all (warning);
* analysis or test steps whose failures are ignored (``allow_failure``, ``continue-on-error``,
  ``|| true``) (warning);
* toolchain versions that disagree between manifests, version files, base images and CI images;
* container builds that copy secret-bearing files, or copy the whole build context while
  ``.dockerignore`` does not exclude ``.env`` files.
"""

from __future__ import annotations

import re
from fnmatch import fnmatchcase
from typing import Any

from agent.core.capability import Capability  # noqa: F401 — kept for symmetry with other checks
from code_quality.evidence_checks import capped, conflicts, declared_versions, files_of_kind, finding, line_of, plural

NAME = "process-evidence"
VERSION = "process-evidence-1"
CATEGORY = "process"
# Steps whose ignored failure hides a quality signal (analysis, tests, audits).
_QUALITY_STEP = re.compile(r"(?i)\b(phpstan|psalm|phpcs|php-cs-fixer|phpmd|rector|phpunit|pest|paratest|eslint|stylelint|"
                           r"prettier|tsc|jest|vitest|mocha|cypress|playwright|pytest|mypy|ruff|flake8|pylint|rubocop|"
                           r"golangci-lint|semgrep|sonar|trivy|audit|lint|test)\b")
_GITLAB_RESERVED = {"stages", "variables", "include", "default", "workflow", "image", "services", "before_script",
                    "after_script", "cache", "pages"}
_SECRET_COPY = re.compile(r"(?i)^(COPY|ADD)\b.*?(\s|/)(\.env(\.[\w.-]+)?|auth\.json|\.npmrc|\.pypirc|id_[a-z0-9]+|[\w.-]+\.(pem|key|p12|pfx))(\s|$)")
_WHOLE_CONTEXT = re.compile(r"(?i)^(COPY|ADD)\b(?:\s+--[\w-]+(?:=\S+)?)*\s+\.\s+\S+")
_ENV_PROBES = (".env", ".env.local", ".env.prod", "config/.env")


def _yaml(text: str | None) -> Any:
    import yaml

    try:
        return yaml.safe_load(text) if text else None
    except yaml.YAMLError:
        return None


def _jobs(path: str, data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    if ".github/workflows/" in f"/{path}":
        return data.get("jobs") if isinstance(data.get("jobs"), dict) else {}
    return {key: value for key, value in data.items()
            if isinstance(value, dict) and not str(key).startswith(".") and key not in _GITLAB_RESERVED}


def _ci(context: Any) -> tuple[list[dict[str, Any]], int]:
    rows, pipelines = [], 0
    for path in sorted(files_of_kind(context, "ci")):
        text = context.read_text(path, 512_000) or ""
        data = _yaml(text) if path.endswith((".yml", ".yaml")) else None
        jobs = _jobs(path, data)
        pipelines += 1
        stages = data.get("stages") if isinstance(data, dict) and isinstance(data.get("stages"), list) else []
        detail = f" with {plural(len(jobs), 'job')}" if jobs else ""
        detail += f" across stages {', '.join(map(str, stages[:8]))}" if stages else ""
        rows.append(finding(path, 1, "process-ci-pipeline", CATEGORY, "info", f"CI pipeline {path}{detail}."))
        for name, job in jobs.items():
            ignored = job.get("allow_failure") is True or job.get("continue-on-error") is True
            body = str(job.get("script") or "") + str(job.get("steps") or "")
            if ignored and _QUALITY_STEP.search(f"{name} {body}"):
                key = "allow_failure" if "allow_failure" in job else "continue-on-error"
                rows.append(finding(path, line_of(text, re.compile(rf"^\s*{re.escape(str(name))}\s*:")), "process-ignored-check-failure",
                                    CATEGORY, "warning", f"CI job '{name}' runs a quality check but sets {key}: true, so its failure never blocks the pipeline."))
        for number, row in enumerate(text.splitlines(), 1):
            if re.search(r"\|\|\s*(true|exit 0|:)\s*$", row.strip().strip("'\"")) and _QUALITY_STEP.search(row):
                rows.append(finding(path, number, "process-ignored-check-failure", CATEGORY, "warning",
                                    f"A CI step ignores its own failure ('{row.strip()[:120]}'), so analysis or test failures never fail the build."))
    return rows, pipelines


def _ignored(patterns: list[str], probe: str) -> bool:
    result = False
    for pattern in patterns:
        negate = pattern.startswith("!")
        body = pattern[1:] if negate else pattern
        body = body.lstrip("/")
        candidates = {body, body.removeprefix("**/")}
        if any(fnmatchcase(probe, c) or fnmatchcase(probe.rsplit("/", 1)[-1], c) for c in candidates):
            result = not negate
    return result


def _containers(context: Any) -> list[dict[str, Any]]:
    rows = []
    ignore_text = context.read_text(".dockerignore", 64_000)
    patterns = [line.strip() for line in (ignore_text or "").splitlines() if line.strip() and not line.strip().startswith("#")]
    for path in sorted(p for p in files_of_kind(context, "container") if "dockerfile" in p.lower().rsplit("/", 1)[-1]
                       or "containerfile" in p.lower().rsplit("/", 1)[-1]):
        text = context.read_text(path, 512_000) or ""
        lines = text.splitlines()
        bases = [m.group(1) for row in lines if (m := re.match(r"(?i)^\s*FROM\s+(?:--\S+\s+)*(\S+)", row))]
        rows.append(finding(path, line_of(text, re.compile(r"(?i)^\s*FROM\s")), "process-container-build", CATEGORY, "info",
                            f"Container build {path} from {', '.join(bases[:4]) or 'an unnamed base'}."))
        for number, row in enumerate(lines, 1):
            stripped = row.strip()
            if (match := _SECRET_COPY.match(stripped)):
                rows.append(finding(path, number, "process-secret-copied-into-image", CATEGORY, "error",
                                    f"{path} copies {match.group(3)} into the image; anyone with the image can read it. "
                                    "Pass credentials as build secrets (RUN --mount=type=secret) or at runtime."))
            elif _WHOLE_CONTEXT.match(stripped):
                exposed = [probe for probe in _ENV_PROBES if not _ignored(patterns, probe)]
                if ignore_text is None or ".env" in exposed:
                    reason = "there is no .dockerignore" if ignore_text is None else ".dockerignore does not exclude " + ", ".join(exposed[:3])
                    rows.append(finding(path, number, "process-build-context-includes-secrets", CATEGORY, "warning",
                                        f"{path} copies the whole build context and {reason}, so local .env files would end up in the image."))
    return rows


def run(collections: dict[Capability, Any], context: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows, pipelines = _ci(context)
    rows += _containers(context)
    declared = declared_versions(context)
    for left, right, text in conflicts(declared):
        rows.append(finding(right.file, right.line, "process-toolchain-version-conflict", CATEGORY, "warning",
                            f"Toolchain versions disagree: {text} ({left.file}:{left.line})."))
    manifests = files_of_kind(context, "manifest")
    if not pipelines and manifests:
        anchor = sorted(manifests)[0]
        rows.append(finding(anchor, 1, "process-no-ci", CATEGORY, "warning",
                            "No CI pipeline configuration was found in this revision, so nothing checks changes before they merge."))
    summary = {"ci_files": pipelines, "container_files": len(files_of_kind(context, "container")),
               "toolchains": sorted({f"{d.toolchain} {d.version} ({d.source})" for d in declared})[:20]}
    if not rows:
        summary["reason"] = "No CI, container or dependency manifest files were found, so process cannot be assessed."
    return capped(rows, summary)


def check():
    from code_quality.framework_evidence import FrameworkCheck

    return FrameworkCheck(NAME, VERSION, (), run, accepts_context=True, runs_without_providers=True, category=CATEGORY)
