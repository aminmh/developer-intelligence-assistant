"""Analyzer plugin interface (spec §10)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agent.core.types import AnalysisContext, AnalysisResult, AnalysisScope


@runtime_checkable
class Analyzer(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def scope(self) -> AnalysisScope: ...

    async def analyze(self, context: AnalysisContext) -> AnalysisResult: ...
