"""Registration shape only; external generation is disabled pending independent validation.

The preview service accepts only its trusted built-in implementations. Registering
an external adapter does not make its generated files eligible for download.
"""
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class MigrationAdapter(Protocol):
    def generate(self, operations: list[dict[str, Any]], database: str) -> list[dict[str, str]]: ...
