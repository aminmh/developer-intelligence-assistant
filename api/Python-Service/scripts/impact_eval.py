"""Check change impact analysis against a live indexed revision (plan Phase 5, task T19).

Acceptance criteria of .archcore/agent-tools/implement-change-impact-analysis.plan.md, checked
through the real HTTP route, ``POST /code-tools/impact``:

  timing      an impact request for a hub symbol at the default depth finishes within 10 s
  citations   every impacted row's path:line exists in the revision (read back via /code-tools/read)
  routes      a one-line diff inside a method reaches at least one route
  comparison  comparing two revisions reports exactly the expected number of changed methods

The python-api container publishes no port, so run it inside the container:

    docker cp scripts/impact_eval.py python_api:/tmp/impact_eval.py
    docker exec python_api python /tmp/impact_eval.py --project pizza --revision latest \\
        --base-revision <older revision id>

Uses only the standard library. Prints one ``pass``/``fail``/``skip`` line per check and exits 1
when any check fails. ``--json`` also writes the raw reports for inspection.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any

DEFAULT_SYMBOL = "App\\Traits\\ApiResponse::successResponse"
TIME_LIMIT_S = 10.0
MAX_CITATIONS = 60


def post(api: str, path: str, body: dict[str, Any], timeout: float = 60.0) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(api.rstrip("/") + path, data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read() or b"{}")
        except ValueError:
            detail = {"detail": str(exc)}
        return exc.code, detail


class Checks:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def record(self, outcome: str, name: str, detail: str) -> None:
        self.rows.append((outcome, name, detail))
        print(f"{outcome:4} {name}: {detail}", flush=True)

    @property
    def failed(self) -> bool:
        return any(outcome == "fail" for outcome, _, _ in self.rows)


def impact(api: str, project: str, revision: str, **inputs: Any) -> tuple[int, dict[str, Any], float]:
    started = time.monotonic()
    status, body = post(api, "/code-tools/impact", {"project_id": project, "revision_id": revision, **inputs})
    return status, body, time.monotonic() - started


def check_timing(api: str, args: argparse.Namespace, checks: Checks, reports: dict[str, Any]) -> dict[str, Any] | None:
    status, report, seconds = impact(api, args.project, args.revision, symbols=[args.symbol])
    reports["timing"] = report
    if status != 200:
        checks.record("fail", "timing", f"HTTP {status}: {report.get('detail')}")
        return None
    if report.get("coverage") == "unavailable" or not report.get("changed"):
        checks.record("fail", "timing", f"{args.symbol} did not resolve (coverage {report.get('coverage')}, "
                                        f"unresolved {report.get('unresolved')})")
        return None
    summary = (f"{seconds:.1f}s, {len(report.get('impacted', []))} impacted, {len(report.get('routes', []))} routes, "
               f"coverage {report.get('coverage')} {report.get('coverage_reasons') or ''}".rstrip())
    checks.record("pass" if seconds <= TIME_LIMIT_S else "fail", "timing", summary)
    return report


def check_citations(api: str, args: argparse.Namespace, report: dict[str, Any], checks: Checks) -> None:
    rows = [row for row in report.get("impacted", []) if row.get("path")]
    if not rows:
        checks.record("fail", "citations", "no impacted row carries a project-relative path")
        return
    sample = rows[:MAX_CITATIONS]
    bad: list[str] = []
    for row in sample:
        line = int(row.get("line") or 0)
        status, body = post(api, "/code-tools/read", {"project_id": args.project,
                                                      "revision_id": report["revision_id"],
                                                      "path": row["path"], "line": max(line, 1)})
        if status != 200 or line < 1 or line > int(body.get("total_lines") or 0):
            bad.append(f"{row['path']}:{line} (HTTP {status})")
    unpathed = len(report.get("impacted", [])) - len(rows)
    detail = f"{len(sample) - len(bad)}/{len(sample)} cited lines exist" + (
        f"; {unpathed} rows had no path" if unpathed else "") + (f"; bad: {', '.join(bad[:5])}" if bad else "")
    checks.record("fail" if bad else "pass", "citations", detail)


def check_routes(api: str, args: argparse.Namespace, report: dict[str, Any], checks: Checks,
                 reports: dict[str, Any]) -> None:
    """A synthetic one-line diff inside the changed method's first body line must reach a route."""
    changed = next((row for row in report.get("changed", []) if row.get("path") and row.get("line")), None)
    if changed is None:
        checks.record("fail", "routes", "the changed symbol has no project-relative path to build a diff from")
        return
    line = int(changed["line"]) + 1
    diff = (f"--- a/{changed['path']}\n+++ b/{changed['path']}\n@@ -{line},1 +{line},1 @@\n"
            f"-// impact_eval original line\n+// impact_eval changed line\n")
    status, routed, seconds = impact(api, args.project, report["revision_id"], diff=diff)
    reports["routes"] = routed
    if status != 200:
        checks.record("fail", "routes", f"HTTP {status}: {routed.get('detail')}")
        return
    routes = routed.get("routes", [])
    names = ", ".join(f"{r.get('method')} {r.get('path')}" for r in routes[:5])
    changed_names = [row.get("fqn") for row in routed.get("changed", [])]
    checks.record("pass" if routes else "fail", "routes",
                  f"diff at {changed['path']}:{line} changed {changed_names}; {len(routes)} routes in {seconds:.1f}s"
                  + (f" ({names})" if names else ""))


def check_comparison(api: str, args: argparse.Namespace, checks: Checks, reports: dict[str, Any]) -> None:
    if not args.base_revision:
        checks.record("skip", "comparison", "pass --base-revision to compare two revisions")
        return
    status, report, seconds = impact(api, args.project, args.revision, base_revision_id=args.base_revision)
    reports["comparison"] = report
    if status != 200:
        checks.record("fail", "comparison", f"HTTP {status}: {report.get('detail')}")
        return
    if report.get("coverage") == "unavailable":
        checks.record("fail", "comparison", f"coverage unavailable for {report.get('unavailable_revision')}")
        return
    methods = [row for row in report.get("changed", []) if row.get("kind") == "method"]
    listed = ", ".join(f"{row.get('fqn')} ({row.get('change')})" for row in methods[:8])
    ok = len(methods) == args.expect_changed
    checks.record("pass" if ok else "fail", "comparison",
                  f"{len(methods)} changed methods, expected {args.expect_changed}, in {seconds:.1f}s"
                  + (f": {listed}" if listed else ""))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--api", default="http://localhost:9002", help="Python API base URL (inside the container)")
    parser.add_argument("--project", required=True)
    parser.add_argument("--revision", default="latest", help="revision id or 'latest'")
    parser.add_argument("--base-revision", help="older revision id for the comparison check")
    parser.add_argument("--expect-changed", type=int, default=1, help="methods expected to differ between revisions")
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL, help="hub symbol for the timing check")
    parser.add_argument("--json", help="write the raw reports to this file")
    args = parser.parse_args(argv)

    checks, reports = Checks(), {}
    report = check_timing(args.api, args, checks, reports)
    if report is not None:
        check_citations(args.api, args, report, checks)
        check_routes(args.api, args, report, checks, reports)
    else:
        checks.record("skip", "citations", "needs a resolved timing report")
        checks.record("skip", "routes", "needs a resolved timing report")
    check_comparison(args.api, args, checks, reports)
    if args.json:
        with open(args.json, "w") as handle:
            json.dump({"checks": checks.rows, "reports": reports}, handle, indent=2)
    return 1 if checks.failed else 0


if __name__ == "__main__":
    sys.exit(main())
