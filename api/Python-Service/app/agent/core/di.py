"""Lightweight dependency-injection container (spec §21).

Avoids global singletons: the agent and its collaborators are constructed
explicitly, which keeps tests isolated and future remote execution possible.

TODO(scope-B): the Agent itself is resolved from this container — registered
llm/repository/registry implementations get injected on construction.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")


class Container:
    """Register factories by type name; resolve lazily with cached singletons.

    Singleton semantics: resolving the same key twice returns the same
    instance unless ``singleton=False`` was passed at registration.
    """

    def __init__(self) -> None:
        self._factories: dict[str, Callable[[Container], Any]] = {}
        self._instances: dict[str, Any] = {}
        self._singleton: dict[str, bool] = {}

    def register(
        self,
        key: str,
        factory: Callable[[Container], Any],
        singleton: bool = True,
    ) -> None:
        self._factories[key] = factory
        self._singleton[key] = singleton
        self._instances.pop(key, None)

    def resolve(self, key: str) -> Any:
        factory = self._factories.get(key)
        if factory is None:
            raise KeyError(f"no factory registered for {key!r}")
        if self._singleton.get(key, True) and key in self._instances:
            return self._instances[key]
        instance = factory(self)
        if self._singleton.get(key, True):
            self._instances[key] = instance
        return instance

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self.resolve(key)
        except KeyError:
            return default
