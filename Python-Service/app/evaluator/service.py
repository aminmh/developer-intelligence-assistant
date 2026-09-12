"""Evaluator MVP orchestration and review-only recommendation contract."""

from __future__ import annotations

import threading
import uuid
from pathlib import Path
from typing import Any

from analysis.analysis_store import analysis_store
from infra.settings import settings
from .linker import SourceLinker
from .models import EvaluatorReport
from .parsers import analyze_explain, parse_mysql_slow_log, parse_postgres_slow_log


class EvaluatorService:
    def __init__(self) -> None:
        self._runs: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    def start(self, body: dict[str, Any]) -> dict[str, Any]:
        run_id = str(uuid.uuid4())
        filename = str(body.get("filename") or "evaluator.zip")
        session_id = str(body.get("analysis_id") or uuid.uuid4())
        if not body.get("analysis_id"):
            analysis_store.save(session_id, run_id, filename, label="Database evaluator", profile={"evaluator_reports": []}, kind="evaluator")
        state = {"run_id": run_id, "analysis_id": session_id, "filename": filename, "status": "queued", "reports": [], "context": {}, "error": None}
        with self._lock:
            self._runs[run_id] = state
        threading.Thread(target=self._run, args=(run_id, dict(body)), daemon=True).start()
        return self.status(run_id) or state

    def status(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            state = self._runs.get(run_id)
            return dict(state) if state else None

    def reports(self, run_id: str) -> list[dict[str, Any]]:
        state = self.status(run_id)
        return list(state.get("reports") or []) if state else []

    def context(self, run_id: str) -> dict[str, Any]:
        state = self.status(run_id)
        return dict(state.get("context") or {}) if state else {}

    def recommendation(self, run_id: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        reports = [EvaluatorReport.from_dict(item) for item in self.reports(run_id)]
        if not reports:
            return {"availability": "insufficient_evidence", "error": "No normalized evaluator reports are available.", "review_required": True}
        issues = sorted({issue for report in reports for issue in report.issues})
        evidence = [e for report in reports for e in report.evidence]
        root = "Evidence is insufficient to identify a database bottleneck."
        if issues:
            root = "Observed query evidence indicates: " + ", ".join(issues) + "."
        database = ""
        if any(issue in issues for issue in ("full_table_scan", "missing_index_coverage", "high_rows_examined")):
            database = "Review WHERE/JOIN predicates and add a covering or composite index only after confirming selectivity."
        application = "Inspect call frequency and selected columns; consider eager loading, projection, pagination, or batching when the linked source supports it."
        migration = ""
        if "missing_index_coverage" in issues or "full_table_scan" in issues:
            migration = "-- Review-only suggestion; confirm table and column order before applying.\nSchema::table('<table>', function (Blueprint $table) { $table->index(['<predicate_column>']); });"
        return {
            "availability": "available" if issues else "insufficient_evidence",
            "root_cause_analysis": root,
            "evidence": sorted(set(evidence)),
            "confidence": "high" if issues else "low",
            "application_optimization": application,
            "database_recommendation": database,
            "migration_or_ddl": migration,
            "unified_diff_patch": "",
            "review_required": True,
            "auto_apply": False,
            "notes": [] if issues else ["Profiler/plan evidence did not establish a concrete issue."],
        }

    def _run(self, run_id: str, body: dict[str, Any]) -> None:
        self._set(run_id, status="running")
        try:
            database = str(body.get("database") or "mysql")
            raw = body.get("slow_log") or body.get("slow_query_log") or ""
            reports = (parse_postgres_slow_log(raw) if database.casefold() in {"postgres", "postgresql"} else parse_mysql_slow_log(raw)) if raw else []
            explain = body.get("explain")
            if explain is not None:
                reports.append(analyze_explain(explain, str(body.get("query") or "")))
            workspace = body.get("workspace")
            workspace_path = Path(str(workspace)).resolve() if workspace else None
            allowed_root = Path(settings.workspace_root).resolve()
            if workspace_path and not (workspace_path == allowed_root or allowed_root in workspace_path.parents):
                workspace_path = None
            if workspace_path:
                linker = SourceLinker(workspace_path)
                for report in reports:
                    if report.source_trace.file:
                        report.source_trace = linker.link(source_hint=report.source_trace)
            schema_context = body.get("schema") or {}
            if isinstance(schema_context, dict):
                for report in reports:
                    report.schema_context = dict(schema_context)
            serialized = [report.to_dict() for report in reports[:500]]
            context = {"workspace": str(workspace_path) if workspace_path else None, "schema": schema_context, "linked": sum(bool(item.get("source_trace", {}).get("available")) for item in serialized)}
            state = self.status(run_id) or {}
            analysis_store.save_evaluator_reports(state.get("analysis_id", run_id), serialized, context=context)
            self._set(run_id, status="completed", reports=serialized, context=context)
        except Exception as exc:  # noqa: BLE001 - lifecycle must surface honest failure
            self._set(run_id, status="error", error=str(exc)[:500])

    def _set(self, run_id: str, **updates: Any) -> None:
        with self._lock:
            if run_id in self._runs:
                self._runs[run_id].update(updates)


evaluator_service = EvaluatorService()
