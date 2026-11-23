"""Laravel ``probe_hints``: files that show valid request parameters and data.

Seeders and factories show which records exist after `migrate --seed`, so route parameters
such as `{order}` can name real ids; form requests hold validation rules for request bodies.
Only the Laravel file layout is used here; contents are read later as bounded excerpts.
"""

from __future__ import annotations

import re

from agent.core.records import ParamHintRecord
from agent.core.revision import RevisionContext

MAX_HINTS = 60
_KINDS = (("database/seeders/", "seeder"), ("database/seeds/", "seeder"), ("database/factories/", "factory"),
          ("app/Http/Requests/", "validation"), ("tests/Fixtures/", "fixture"))


def provide_probe_hints(context: RevisionContext) -> list[ParamHintRecord]:
    hints: list[ParamHintRecord] = []
    for path in context.files:
        if not path.endswith(".php"):
            continue
        kind = next((kind for prefix, kind in _KINDS if path.startswith(prefix)), None)
        if kind is None:
            continue
        name = re.sub(r"\.php$", "", path.rsplit("/", 1)[-1])
        hints.append(ParamHintRecord(framework="laravel", kind=kind, name=name, file=path))
    # Seeders first: they decide which ids exist; then validation rules for bodies.
    order = {"seeder": 0, "factory": 1, "validation": 2, "fixture": 3}
    hints.sort(key=lambda hint: (order.get(hint.kind, 9), hint.file))
    return hints[:MAX_HINTS]
