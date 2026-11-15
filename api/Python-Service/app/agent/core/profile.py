"""Project profile — the structured answer to project detection (spec §7).

Shape mirrors the spec::

    {
      "languages": [{"name": "php", "confidence": 0.99}],
      "frameworks": [{"name": "laravel", "version": "11", "confidence": 0.99}],
      "runtime": {"name": "php", "version": "8.3"}
    }

TODO(scope-B): consumed by the agent loop to pick analyzers/validators/tools.
TODO(scope-C): extended with code-graph references (graph_store.py) for retrieval.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent.core.capability import Capability


@dataclass(frozen=True)
class LanguageProfile:
    name: str
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "confidence": self.confidence}


@dataclass(frozen=True)
class FrameworkProfile:
    name: str
    confidence: float
    version: str | None = None
    capabilities: tuple[Capability, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class RuntimeProfile:
    name: str
    version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "version": self.version}


@dataclass
class ProjectProfile:
    path: Path
    languages: list[LanguageProfile] = field(default_factory=list)
    frameworks: list[FrameworkProfile] = field(default_factory=list)
    runtime: RuntimeProfile | None = None

    @property
    def capabilities(self) -> set[Capability]:
        caps: set[Capability] = set()
        for fw in self.frameworks:
            caps.update(fw.capabilities)
        return caps

    def to_dict(self) -> dict[str, Any]:
        return {
            "languages": [l.to_dict() for l in self.languages],
            "frameworks": [f.to_dict() for f in self.frameworks],
            "runtime": self.runtime.to_dict() if self.runtime else None,
        }
