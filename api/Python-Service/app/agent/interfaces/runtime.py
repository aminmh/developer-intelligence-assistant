"""Runtime plugin interface (spec §12)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agent.core.types import ExecutionResult, RuntimeInstance


@runtime_checkable
class Runtime(Protocol):
    """How a language/framework executes commands.

    Framework plugins never manipulate Docker directly (spec §12) — they ask
    the runtime to run something.
    """

    @property
    def name(self) -> str: ...

    async def start(self) -> RuntimeInstance: ...

    async def execute(self, command: list[str]) -> ExecutionResult: ...

    async def stop(self) -> None: ...
