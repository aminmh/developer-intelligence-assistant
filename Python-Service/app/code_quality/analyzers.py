"""Pinned, offline Semgrep execution over the assessed source inventory."""
from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import resource
import signal
import shutil
import subprocess
import tempfile
from typing import Any
import zipfile

import yaml

from code_quality.rules import validate_rule_yaml
from code_quality.service import AssessmentError, MAX_FINDINGS

SEMGREP_VERSION = "1.181.0"
BUILTIN_RULESET_VERSION = "code-quality-builtins-5"
TIMEOUT_SECONDS = 90
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_RESULT_COUNT = MAX_FINDINGS // 2
ANALYZER_REGISTRY = {
    "semgrep": {
        "version": SEMGREP_VERSION,
        "execution": "offline-local-rules",
        "timeout_seconds": TIMEOUT_SECONDS,
        "output_bytes": MAX_OUTPUT_BYTES,
        "findings": MAX_RESULT_COUNT,
    }
}

BUILTIN_RULES_PATH = Path(__file__).parent / "rules" / "builtin.yml"


class AnalyzerUnavailable(RuntimeError):
    pass


def _limits() -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (45, 45))
    # Semgrep needs more than 1.5 GB of virtual address space here; the API
    # container's 1 GB memory limit still caps resident usage.
    resource.setrlimit(resource.RLIMIT_AS, (4_000_000_000, 4_000_000_000))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT_BYTES, MAX_OUTPUT_BYTES))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))


def validate_semgrep_yaml(document: str) -> None:
    """Use the pinned local CLI to reject invalid rule syntax before saving it."""
    executable = shutil.which("semgrep")
    if not executable:
        raise AssessmentError("The pinned Semgrep validator is unavailable in this API runtime.")
    validate_rule_yaml(document)
    try:
        with tempfile.TemporaryDirectory(prefix="code-quality-rule-validation-") as temp_root:
            root = Path(temp_root)
            config = root / "rules.yml"
            config.write_text(document, encoding="utf-8")
            source_root = root / "source"
            source_root.mkdir()
            (source_root / "validation.py").write_text("value = None\n", encoding="utf-8")
            output = subprocess.run(
                [executable, "scan", "--json", "--config", str(config), "--metrics=off",
                 "--no-git-ignore", "--quiet", "--jobs=1", "--timeout=10", str(source_root)],
                cwd=root, env={**os.environ, "SEMGREP_SEND_METRICS": "off", "SEMGREP_ENABLE_VERSION_CHECK": "0",
                               "SEMGREP_APP_TOKEN": "", "HOME": str(root)},
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=15, check=False, preexec_fn=_limits if os.name == "posix" else None,
            )
            if len(output.stdout) + len(output.stderr) > 256_000:
                raise AssessmentError("Semgrep rule validation returned oversized output.")
            if output.returncode not in (0, 1):
                raise AssessmentError("Semgrep rejected the rule syntax. Check pattern and language names.")
            try:
                payload = json.loads(output.stdout.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise AssessmentError("Semgrep returned malformed validation output.") from exc
            if payload.get("errors"):
                raise AssessmentError("Semgrep rejected the rule syntax. Check pattern and language names.")
    except AssessmentError:
        raise
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise AssessmentError("Semgrep could not validate this rule set. Check the rule syntax and retry.") from exc


def _rule_document(custom_rule_sets: list[dict[str, Any]], include_builtins: bool) -> tuple[str, list[dict[str, Any]], list[str], list[str]]:
    merged = yaml.safe_load(BUILTIN_RULES_PATH.read_text(encoding="utf-8"))["rules"] if include_builtins else []
    categories = {rule["metadata"]["category"] for rule in merged}
    languages = {language for rule in merged for language in rule["languages"]}
    snapshots = []
    for item in custom_rule_sets:
        parsed = validate_rule_yaml(item["yaml"])
        snapshots.append({"rule_id": item["rule_id"], "content_hash": item["content_hash"],
                          "title": item["title"], "yaml": item["yaml"]})
        if len(merged) + len(parsed) > 100:
            raise AssessmentError("At most 100 rules can run in one assessment.")
        for index, rule in enumerate(parsed):
            copied = dict(rule)
            copied["id"] = f"custom-{item['rule_id']}-{index}"
            merged.append(copied)
            categories.add(copied["metadata"]["category"])
            languages.update(copied["languages"])
    return yaml.safe_dump({"rules": merged}, sort_keys=False, allow_unicode=True), snapshots, sorted(categories), sorted(languages)


def _normalize_results(payload: dict[str, Any], allowed_files: set[str]) -> list[dict[str, Any]]:
    normalized = []
    for item in payload.get("results", [])[:MAX_RESULT_COUNT]:
        extra = item.get("extra") if isinstance(item, dict) else None
        if not isinstance(extra, dict):
            continue
        raw_path = item.get("path")
        if not isinstance(raw_path, str):
            continue
        path = PurePosixPath(raw_path).as_posix()
        if "/source/" in path:
            path = path.split("/source/", 1)[1]
        elif path.startswith("source/"):
            path = path[len("source/"):]
        while path.startswith("./"):
            path = path[2:]
        if path not in allowed_files:
            continue
        metadata = extra.get("metadata") if isinstance(extra.get("metadata"), dict) else {}
        category = metadata.get("category")
        if category not in {"security", "clean_code", "maintainability", "reliability", "performance"}:
            continue
        rule_id = str(item.get("check_id", "semgrep-rule"))[-120:]
        line = (item.get("start") or {}).get("line", 1)
        message = str(extra.get("message", "Static analyzer finding"))[:240]
        normalized.append({"file": path, "line": line if isinstance(line, int) and line > 0 else 1,
                           "rule": rule_id, "category": category, "message": message,
                           "severity": str(extra.get("severity", "WARNING"))[:20], "kind": "finding"})
        if len(normalized) >= MAX_RESULT_COUNT:
            break
    return normalized


def run_semgrep(archive_data: bytes, source_files: list[str], custom_rule_sets: list[dict[str, Any]],
                include_builtins: bool = True) -> dict[str, Any]:
    executable = shutil.which("semgrep")
    if not executable:
        raise AnalyzerUnavailable("Pinned Semgrep is not installed in the API runtime.")
    rules_yaml, snapshots, categories, languages = _rule_document(custom_rule_sets, include_builtins)
    allowed = set(source_files)
    if not allowed:
        return {"findings": [], "rules": snapshots, "categories": categories,
                "languages": languages, "version": ANALYZER_REGISTRY["semgrep"]["version"]}
    try:
        with tempfile.TemporaryDirectory(prefix="code-quality-") as temp_root:
            root = Path(temp_root)
            source_root = root / "source"
            source_root.mkdir()
            with zipfile.ZipFile(__import__("io").BytesIO(archive_data)) as archive:
                for relative in source_files:
                    path = PurePosixPath(relative)
                    if path.is_absolute() or ".." in path.parts or "\\" in relative:
                        raise AssessmentError("The source inventory contains an unsafe path.")
                    target = source_root.joinpath(*path.parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.read(relative))
            config = root / "rules.yml"
            config.write_text(rules_yaml, encoding="utf-8")
            output_file = root / "semgrep.json"
            env = {**os.environ, "SEMGREP_SEND_METRICS": "off", "SEMGREP_ENABLE_VERSION_CHECK": "0",
                   "SEMGREP_APP_TOKEN": "", "HOME": str(root)}
            try:
                with output_file.open("wb") as output:
                    process = subprocess.Popen(
                        [executable, "scan", "--json", "--config", str(config), "--metrics=off",
                         "--no-git-ignore", "--quiet", "--jobs=1", "--timeout=10", str(source_root)],
                        cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=output,
                        stderr=subprocess.DEVNULL, start_new_session=(os.name == "posix"),
                        preexec_fn=_limits if os.name == "posix" else None,
                    )
                    try:
                        return_code = process.wait(timeout=TIMEOUT_SECONDS)
                    except subprocess.TimeoutExpired as exc:
                        if os.name == "posix":
                            os.killpg(process.pid, signal.SIGKILL)
                        else:
                            process.kill()
                        process.wait()
                        raise AnalyzerUnavailable("Semgrep exceeded its 90 second time limit.") from exc
            except subprocess.TimeoutExpired as exc:
                raise AnalyzerUnavailable("Semgrep exceeded its 90 second time limit.") from exc
            except OSError as exc:
                raise AnalyzerUnavailable("Semgrep could not start the configured offline scan.") from exc
            if output_file.stat().st_size > MAX_OUTPUT_BYTES:
                raise AnalyzerUnavailable("Semgrep output exceeded its size limit.")
            if return_code not in (0, 1):
                raise AnalyzerUnavailable("Semgrep could not complete the configured offline scan.")
            try:
                payload = json.loads(output_file.read_text(encoding="utf-8"))
            except (ValueError, OSError) as exc:
                raise AnalyzerUnavailable("Semgrep returned malformed or unreadable structured output.") from exc
            if payload.get("errors"):
                raise AnalyzerUnavailable("Semgrep reported one or more incomplete or invalid analyzer passes.")
            return {"findings": _normalize_results(payload, allowed), "rules": snapshots,
                    "categories": categories, "languages": languages,
                    "version": ANALYZER_REGISTRY["semgrep"]["version"]}
    except (OSError, zipfile.BadZipFile) as exc:
        raise AnalyzerUnavailable("The source archive could not be prepared for static analysis.") from exc
