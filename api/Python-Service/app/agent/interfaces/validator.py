"""Validator plugin interface (spec §11)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agent.core.types import ValidationContext, ValidationResult


@runtime_checkable
class Validator(Protocol):
    @property
    def name(self) -> str: ...

    async def validate(self, context: ValidationContext) -> ValidationResult: ...
