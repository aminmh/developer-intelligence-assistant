"""Capability provider interface for framework plugins.

Decision: @.archcore/code-quality/framework-knowledge-lives-in-plugins-over-language-facts.adr.md.
A framework plugin that lists a capability in ``capabilities`` and implements
``provide`` returns that capability's normalized records
(@api/Python-Service/app/agent/core/records.py) for one revision. Checks reach
providers only through ``PluginRegistry.collect``, never by framework name.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from agent.core.capability import Capability
from agent.core.records import (
    AccessRuleRecord,
    ConfigRiskRecord,
    GuardRecord,
    RouteRecord,
    TaintStubRecord,
    TemplateRecord,
    TestRecord,
    TranslationRecord,
)
from agent.core.revision import RevisionContext

# Record types each capability may return.
RECORD_TYPES: dict[Capability, tuple[type, ...]] = {
    Capability.ROUTE_ANALYSIS: (RouteRecord,),
    Capability.ROUTE_AUTHORIZATION: (GuardRecord, AccessRuleRecord),
    Capability.TRANSLATIONS: (TranslationRecord,),
    Capability.TEMPLATES: (TemplateRecord,),
    Capability.TEST_CONVENTIONS: (TestRecord,),
    Capability.CONFIG_RISKS: (ConfigRiskRecord,),
    Capability.TAINT_MODEL: (TaintStubRecord,),
}


@runtime_checkable
class CapabilityProvider(Protocol):
    """A framework plugin that turns language facts and configuration into records."""

    @property
    def name(self) -> str: ...

    def provide(self, capability: Capability, context: RevisionContext) -> list[Any]:
        """Records for ``capability`` on this revision; ``[]`` when there are none.

        Raise ``CapabilityUnavailable`` when the plugin cannot produce them for this
        revision (for example, the language facts export failed).
        """
        ...


class CapabilityUnavailable(RuntimeError):
    """A provider cannot produce a capability's records for this revision; the message says why."""
