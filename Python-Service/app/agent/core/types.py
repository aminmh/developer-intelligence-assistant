"""Core agent types shared across plugins and the agent itself.

Scope A: shared data contracts only. The agent loop that consumes them is
TODO(scope-B) — see agent/__init__.py roadmap.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent.core.capability import Capability
from agent.core.workspace import Workspace


@dataclass(frozen=True)
class Project:
    """A project under analysis: a root directory plus its file list."""

    root: Path
    files: tuple[Path, ...] = ()

    @classmethod
    def from_path(cls, root: str | Path) -> Project:
        base = Path(root).resolve()
        files = tuple(p for p in sorted(base.rglob("*")) if p.is_file())
        return cls(root=base, files=files)

    def has_any(self, names: tuple[str, ...]) -> bool:
        return any(f.name in names for f in self.files)

    def rel_names(self) -> list[str]:
        return [str(f.relative_to(self.root)) for f in self.files]


@dataclass(frozen=True)
class DetectionResult:
    """A single plugin's opinion about a project."""

    name: str
    confidence: float
    version: str | None = None


@dataclass(frozen=True)
class AnalysisScope:
    kind: str  # e.g. "ast", "routes", "dependencies", "models"
    detail: str = ""


@dataclass(frozen=True)
class AnalysisContext:
    """What an analyzer may look at (spec §10)."""

    workspace: Workspace
    project: Project
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AnalysisFinding:
    message: str
    kind: str = "info"  # info | warning | error
    location: str | None = None


@dataclass(frozen=True)
class AnalysisResult:
    scope: AnalysisScope
    findings: list[AnalysisFinding] = field(default_factory=list)
    summary: str = ""
    # Structured analyzer output (framework-specific), e.g. a Laravel route
    # table. Kept generic: plugins define their own schema under ``data``.
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": {"kind": self.scope.kind, "detail": self.scope.detail},
            "summary": self.summary,
            "findings": [
                {
                    "message": f.message,
                    "kind": f.kind,
                    "location": f.location,
                }
                for f in self.findings
            ],
            "data": self.data,
        }


@dataclass(frozen=True)
class ValidationContext:
    workspace: Workspace
    project: Project
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ValidationIssue:
    message: str
    severity: str = "error"  # error | warning | info
    location: str | None = None


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    issues: list[ValidationIssue] = field(default_factory=list)
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "detail": self.detail,
            "issues": [
                {
                    "message": i.message,
                    "severity": i.severity,
                    "location": i.location,
                }
                for i in self.issues
            ],
        }


@dataclass(frozen=True)
class ExecutionResult:
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_ms: float = 0.0

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "exit_code": self.exit_code,
            "ok": self.ok,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": self.duration_ms,
        }


@dataclass(frozen=True)
class RuntimeInstance:
    """A started runtime handle (spec §12)."""

    name: str
    detail: str = ""


@dataclass(frozen=True)
class SandboxConfig:
    image: str = "alpine:3.20.3"
    read_only: bool = True
    cap_drop: tuple[str, ...] = ("ALL",)
    pids_limit: int = 256
    mem_limit: str = "1g"
    tmpfs: tuple[str, ...] = ("/tmp",)
    runtime: str = "runsc"
    timeout_s: int = 120


@dataclass(frozen=True)
class SandboxInstance:
    """A created sandbox ready for execution (spec §13)."""

    id: str
    workspace_root: Path
    detail: str = ""


@dataclass(frozen=True)
class RuntimeInspection:
    name: str
    version: str | None = None
    capabilities: tuple[Capability, ...] = ()
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "capabilities": [c.value for c in self.capabilities],
            "detail": self.detail,
        }


@dataclass(frozen=True)
class Tool:
    """A capability-driven tool exposed to the LLM (agent loop, scope-B)."""

    name: str
    description: str
    callable: Callable[..., Any]

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "description": self.description}
