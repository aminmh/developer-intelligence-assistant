"""Optional profiler/provider registry used by evaluator integrations."""

from __future__ import annotations

from typing import Any

from .models import EvaluatorReport, ProfilerAdapter


class EvaluatorAdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, ProfilerAdapter] = {}

    def register(self, adapter: ProfilerAdapter) -> None:
        name = str(getattr(adapter, "name", "")).strip()
        if not name:
            raise ValueError("evaluator adapter requires a name")
        self._adapters[name] = adapter

    def get(self, name: str) -> ProfilerAdapter | None:
        return self._adapters.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))

    def collect(self, name: str, context: Any = None) -> list[EvaluatorReport]:
        adapter = self.get(name)
        if adapter is None:
            return []
        try:
            return list(adapter.normalize(adapter.collect(context)))
        except Exception:
            return []


evaluator_adapters = EvaluatorAdapterRegistry()
