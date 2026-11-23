"""Laravel ``doc_sources``: Laravel's guides live in ``laravel/docs``, one branch per major line.

Found live (2026-11-23): ``laravel/framework``'s CHANGELOG at ``v13.0.0`` still holds 12.x notes, and the
upgrade guide for 13.0 is ``upgrade.md`` on the ``13.x`` branch of ``laravel/docs``.
"""

from __future__ import annotations

from agent.core.records import DocSourceRecord
from agent.core.revision import RevisionContext

LARAVEL_DOCS = DocSourceRecord(
    framework="laravel", package="laravel/framework", repository="laravel/docs", ref="{major}.x",
    upgrade_path="upgrade.md",
    topics=(("test", "http-tests.md"), ("assert", "http-tests.md"), ("rout", "routing.md"), ("binding", "routing.md"),
            ("validat", "validation.md"), ("request", "requests.md"), ("respons", "responses.md"),
            ("eloquent", "eloquent.md"), ("seed", "seeding.md"), ("factor", "eloquent-factories.md")),
)


def provide_doc_sources(context: RevisionContext) -> list[DocSourceRecord]:
    return [LARAVEL_DOCS]
