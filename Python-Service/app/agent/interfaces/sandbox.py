"""Sandbox plugin interface (spec §13).

The core agent never knows whether execution happens under Docker Desktop,
gVisor/runsc, Firecracker, Kubernetes or a remote server — it only talks to
this interface. TODO(scope-B): a GVisorSandbox implementation built on the
existing ``app/sandbox`` package (ReproduceSpec + compose_generator + runsc).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agent.core.types import (
    ExecutionResult,
    SandboxConfig,
    SandboxInstance,
    Workspace,
)


@runtime_checkable
class Sandbox(Protocol):
    async def create(
        self, workspace: Workspace, config: SandboxConfig
    ) -> SandboxInstance: ...

    async def execute(
        self, instance: SandboxInstance, command: list[str]
    ) -> ExecutionResult: ...

    async def destroy(self, instance: SandboxInstance) -> None: ...
