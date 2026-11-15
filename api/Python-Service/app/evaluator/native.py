"""Native EXPLAIN execution seam."""

from __future__ import annotations

from typing import Any


def run_explain(executor: Any, project_id: str, query: str, *, database: str = "mysql", framework: str = "php") -> dict[str, Any]:
    """Ask an execution adapter to run EXPLAIN without owning Docker/DB APIs."""
    if not isinstance(query, str) or not query.strip():
        return {"status": "unavailable", "reason": "query is empty"}
    if len(query) > 20_000 or "\x00" in query:
        return {"status": "unavailable", "reason": "query exceeds evaluator command bounds"}
    method = getattr(executor, "run_explain", None)
    if not callable(method):
        return {"status": "unavailable", "reason": "adapter does not expose EXPLAIN execution"}
    try:
        result = method(project_id, query, database=database, framework=framework)
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "reason": str(exc)[:300]}
    if not isinstance(result, dict):
        return {"status": "error", "reason": "EXPLAIN adapter returned invalid output"}
    return {"status": result.get("status", "done"), "output": result.get("output") or result.get("stdout") or "", "error": result.get("error") or result.get("stderr")}
