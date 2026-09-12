"""Framework-neutral database evaluator contracts."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable
from enum import StrEnum
import re


class AdapterAvailability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    PARTIAL = "partial"
    ERROR = "error"


@dataclass
class SourceTrace:
    file: str | None = None
    line: int | None = None
    class_name: str | None = None
    method: str | None = None
    namespace: str | None = None
    context: list[str] | str = field(default_factory=list)
    language: str | None = None
    confidence: float = 0.0
    available: bool = True
    error: str | None = None

    @property
    def method_name(self) -> str | None:
        return self.method

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EvaluatorReport:
    query_fingerprint: str
    query: str = ""
    database: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    source_trace: SourceTrace = field(default_factory=SourceTrace)
    profiler: dict[str, Any] | str = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    schema_context: dict[str, Any] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)
    availability: str = "available"
    error: str | None = None
    evidence: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_fingerprint": self.query_fingerprint,
            "query": self.query,
            "database": self.database,
            "metrics": dict(self.metrics),
            "source_trace": self.source_trace.to_dict() if isinstance(self.source_trace, SourceTrace) else dict(self.source_trace),
            "profiler": self.profiler if isinstance(self.profiler, str) else dict(self.profiler),
            "provenance": dict(self.provenance),
            "schema_context": dict(self.schema_context),
            "issues": list(self.issues),
            "availability": self.availability,
            "error": self.error,
            "evidence": list(self.evidence),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EvaluatorReport":
        raw_trace = data.get("source_trace") or {}
        trace = raw_trace if isinstance(raw_trace, SourceTrace) else SourceTrace(**raw_trace)
        return cls(
            query_fingerprint=str(data.get("query_fingerprint") or "unknown"),
            query=str(data.get("query") or ""),
            database=data.get("database"),
            metrics=dict(data.get("metrics") or {}),
            source_trace=trace,
            profiler=data.get("profiler") or {},
            provenance=dict(data.get("provenance") or {}),
            schema_context=dict(data.get("schema_context") or {}),
            issues=list(data.get("issues") or []),
            availability=str(data.get("availability") or "available"),
            error=data.get("error"),
            evidence=list(data.get("evidence") or []),
            created_at=str(data.get("created_at") or datetime.now(timezone.utc).isoformat()),
        )


@runtime_checkable
class ProfilerAdapter(Protocol):
    name: str

    def detect(self, context: Any = None) -> dict[str, Any]: ...
    def collect(self, context: Any = None) -> Any: ...
    def normalize(self, raw: Any) -> list[EvaluatorReport]: ...


class TelemetryNormalizer:
    """Normalize native telemetry without knowing a framework or language."""

    @staticmethod
    def report(query: str, metrics: dict[str, Any] | None = None, *, profiler: str = "native", provenance: dict[str, Any] | None = None, issues: list[str] | None = None, availability: str = "available", error: str | None = None) -> EvaluatorReport:
        from .parsers import fingerprint_query
        trace = SourceTrace()
        match = re.search(r"/\*\s*file=['\"]([^'\"]+)['\"],\s*line=(\d+)\s*\*/", query or "", re.I)
        if match:
            trace = SourceTrace(file=match.group(1), line=int(match.group(2)), confidence=1.0)
        return EvaluatorReport(
            query_fingerprint=fingerprint_query(query), query=query,
            source_trace=trace,
            metrics=dict(metrics or {}), profiler={"name": profiler},
            provenance=dict(provenance or {}), issues=list(issues or []),
            availability=availability, error=error,
        )

    def normalize_slow_log(self, raw: str, *, database: str = "mysql", profiler: str = "native") -> list[EvaluatorReport]:
        from .parsers import parse_postgres_slow_log, parse_mysql_slow_log
        parser = parse_postgres_slow_log if database.casefold() in {"postgres", "postgresql"} else parse_mysql_slow_log
        reports = parser(raw)
        for report in reports:
            report.database = database
            report.profiler["name"] = profiler
        return reports

    def normalize_explain(self, query: str, explain: Any, *, database: str = "mysql") -> EvaluatorReport:
        from .parsers import analyze_explain
        report = analyze_explain(explain, query)
        report.database = database
        return report
