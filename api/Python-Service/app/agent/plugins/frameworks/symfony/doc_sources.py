"""Symfony ``doc_sources``: the guides live in ``symfony/symfony-docs``, one branch per minor line (``7.4``).

Upgrade notes come from each component's own CHANGELOG at the target tag, so no upgrade guide is named.
"""

from __future__ import annotations

from agent.core.records import DocSourceRecord
from agent.core.revision import RevisionContext

SYMFONY_DOCS = DocSourceRecord(
    framework="symfony", package="symfony/framework-bundle", repository="symfony/symfony-docs", ref="{major}.{minor}",
    topics=(("test", "testing.rst"), ("assert", "testing.rst"), ("rout", "routing.rst"), ("validat", "validation.rst"),
            ("fixture", "testing.rst"), ("controller", "controller.rst")),
)


def provide_doc_sources(context: RevisionContext) -> list[DocSourceRecord]:
    return [SYMFONY_DOCS]
