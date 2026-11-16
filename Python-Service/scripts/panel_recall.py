"""Offline recall of the deterministic assessment stages against a panel gold set (plan task 28).

Runs only the deterministic, offline stages of a Code Quality assessment on one revision archive
(``git archive --format=zip``) and scores how many of the gold set's findings they reproduce:

    python -I scripts/panel_recall.py --archive rev.zip \\
        --gold app/tests/fixtures/panel_gold/skiplanner-09c17755.json [--history history.zip --commit SHA] [--json out.json]

Stages: the inventory's source signals, Semgrep with the bundled rules, Psalm taint analysis when
installed, the framework and evidence-category checks, strict-types, and (with ``--history``) the
Git-history secret scan. No model is called, nothing is sent anywhere, no project code runs.

A gold finding is reproduced when a finding matches its scope: ``line`` (same file, within the
item's tolerance), ``file`` (any finding in that file), ``revision`` (a finding of that category),
or ``rule:<prefix>[:<path prefix>]``. Category agreement is reported separately.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))


def _stage(name: str, run: Callable[[], list[dict[str, Any]]], timings: dict[str, Any]) -> list[dict[str, Any]]:
    started = time.monotonic()
    try:
        rows = run()
        timings[name] = {"status": "complete", "findings": len(rows), "seconds": round(time.monotonic() - started, 1)}
        return [{**row, "stage": name} for row in rows]
    except Exception as exc:  # noqa: BLE001 — a missing analyzer is a gap in the measurement, not a crash
        timings[name] = {"status": "unavailable", "reason": f"{type(exc).__name__}: {str(exc)[:160]}"}
        return []


def deterministic_findings(archive: bytes, history: bytes | None = None, commit: str | None = None) -> tuple[list[dict], dict]:
    from code_quality import evidence_files
    from code_quality.service import inventory_archive

    timings: dict[str, Any] = {}
    _, inventory_rows, files, _, _ = inventory_archive(archive)
    rows = [{**row, "stage": "inventory"} for row in inventory_rows]
    timings["inventory"] = {"status": "complete", "findings": len(rows)}
    evidence = evidence_files.inventory_archive(archive)
    paths = list(dict.fromkeys([*files, *(r["path"] for r in evidence["files"] if r["readable"])]))

    def semgrep():
        from code_quality.analyzers import run_semgrep

        return run_semgrep(archive, files, [])["findings"]

    def taint():
        from code_quality.framework_evidence import taint_stubs
        from code_quality.taint_analysis import available, run_taint_analysis

        if not available():
            raise RuntimeError("Psalm is not installed here")
        stubs, _ = taint_stubs(archive, files)
        return run_taint_analysis(archive, files, stubs)["findings"]

    def framework():
        from code_quality.framework_evidence import run_on_archive

        found, coverage = run_on_archive(archive, paths)
        for check in coverage.get("checks", []):   # coverage counts some gold items name (scope "summary")
            for key, value in (check.get("summary") or {}).items():
                if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                    found.append({"rule": f"summary:{check['name']}.{key}", "value": value, "category": check.get("category")})
        return found

    def strict_types():
        from code_quality.php_strict_types import run_on_archive

        row, _ = run_on_archive(archive, files)
        return [row] if row else []

    def history_scan():
        from code_quality import history_secrets

        result = history_secrets.run_history_scan(history, commit, history_secrets.TIMEOUT_SECONDS)
        if result["status"] != "complete":
            raise RuntimeError(result.get("reason") or result["status"])
        return history_secrets.findings(result)

    for name, run in (("semgrep", semgrep), ("psalm-taint", taint), ("framework-and-evidence", framework),
                      ("strict-types", strict_types)):
        rows += _stage(name, run, timings)
    if history is not None and commit:
        rows += _stage("history-secrets", history_scan, timings)
    return rows, timings


# Inventory findings say what exists; they never reproduce a defect.
_INVENTORY_RULES = ("-inventory", "documentation-file", "-ci-pipeline", "-container-build", "-handler-reach")


def matches(gold: dict[str, Any], row: dict[str, Any]) -> bool:
    scope, rule = gold["scope"], str(row.get("rule", ""))
    if scope == "summary":
        return rule == f"summary:{gold['summary']}"
    if rule.startswith("summary:") or rule.endswith(_INVENTORY_RULES):
        return False
    if scope == "revision":
        return row.get("category") == gold["category"]
    if scope.startswith("rule:"):
        prefix, _, path = scope[5:].partition(":")
        return str(row.get("rule", "")).startswith(prefix) and str(row.get("file", "")).startswith(path)
    if row.get("file") != gold["file"] or row.get("category") not in {gold["category"], *gold.get("also_categories", [])}:
        return False
    return scope == "file" or abs(int(row.get("line") or 0) - int(gold["line"])) <= int(gold.get("tolerance", 3))


def score(gold: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    results = []
    for item in gold["findings"]:
        hits = [row for row in rows if matches(item, row)]
        results.append({"id": item["id"], "category": item["category"], "deterministic": item["deterministic"],
                        "found": bool(hits), "same_category": any(h.get("category") == item["category"] for h in hits),
                        "by": sorted({f"{h['stage']}:{h.get('rule')}" for h in hits})[:4], "claim": item["claim"]})
    found = [r for r in results if r["found"]]
    required = [r for r in results if r["deterministic"]]
    by_category: dict[str, list[int]] = {}
    for r in results:
        by_category.setdefault(r["category"], [0, 0])
        by_category[r["category"]][1] += 1
        by_category[r["category"]][0] += r["found"]
    return {"gold": len(results), "found": len(found), "recall": round(len(found) / len(results), 3) if results else None,
            "deterministic_required": len(required), "deterministic_found": sum(r["found"] for r in required),
            "by_category": {k: f"{v[0]}/{v[1]}" for k, v in sorted(by_category.items())}, "items": results}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--gold", required=True, type=Path)
    parser.add_argument("--history", type=Path)
    parser.add_argument("--commit")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    gold = json.loads(args.gold.read_text())
    started = time.monotonic()
    rows, timings = deterministic_findings(args.archive.read_bytes(), args.history.read_bytes() if args.history else None,
                                           args.commit or gold.get("commit"))
    result = score(gold, rows)
    result |= {"stages": timings, "seconds": round(time.monotonic() - started, 1), "findings_total": len(rows),
               "findings_by_category": dict(Counter(r.get("category") for r in rows))}
    print(f"Recall {result['found']}/{result['gold']} ({result['recall']:.0%}); required deterministic "
          f"{result['deterministic_found']}/{result['deterministic_required']}; {result['findings_total']} findings in {result['seconds']} s")
    for stage, info in timings.items():
        print(f"  {stage}: {info}")
    print("  by category: " + ", ".join(f"{k} {v}" for k, v in result["by_category"].items()))
    for item in result["items"]:
        mark = "✓" if item["found"] else "✗"
        extra = f" via {', '.join(item['by'])}" if item["found"] else ""
        print(f"  {mark} {item['id']:3s} {'[required] ' if item['deterministic'] else ''}{item['claim'][:90]}{extra}")
    if args.json:
        args.json.write_text(json.dumps(result, indent=1))
    return 0 if result["deterministic_found"] == result["deterministic_required"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
