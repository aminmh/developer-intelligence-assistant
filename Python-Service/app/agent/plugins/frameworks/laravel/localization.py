"""Laravel translation catalogues and Blade views (capabilities ``translations``, ``templates``).

Catalogues are ``lang/<locale>.json`` and ``lang/<locale>/<domain>.php`` (``resources/lang`` before
Laravel 9; Localization docs). PHP catalogues are read from PHP facts (the returned array),
never by parsing PHP here; nested keys are joined with dots, as ``__('domain.key')`` reads them.
Views are ``resources/views/**/*.blade.php``.
"""

from __future__ import annotations

import json
import re
from typing import Any

from agent.core.facts import literal_items, literal_str
from agent.core.records import TemplateRecord, TranslationRecord
from agent.core.revision import RevisionContext

FRAMEWORK = "laravel"
_ROOTS = ("lang/", "resources/lang/")
_LOCALE = r"[A-Za-z]{2,3}(?:[_-][A-Za-z0-9]{2,8})*"
_JSON = re.compile(rf"^(?:resources/)?lang/(?P<locale>{_LOCALE})\.json$")
_PHP = re.compile(rf"^(?:resources/)?lang/(?P<locale>{_LOCALE})/(?P<domain>[^/]+)\.php$")
MAX_MESSAGES = 5_000


def _flatten_literal(value: Any, prefix: str = "") -> list[tuple[str, str]]:
    items = literal_items(value)
    if items:
        rows: list[tuple[str, str]] = []
        for key, item in items:
            name = literal_str(key)
            if name is not None:
                rows.extend(_flatten_literal(item, f"{prefix}.{name}" if prefix else name))
        return rows
    text = literal_str(value)
    return [(prefix, text)] if prefix and text is not None else []


def _flatten_json(value: Any) -> list[tuple[str, str]]:
    return [(str(k), str(v)) for k, v in value.items() if isinstance(v, (str, int, float))] if isinstance(value, dict) else []


def provide_translations(context: RevisionContext, facts: Any | None) -> list[TranslationRecord]:
    records = []
    for path in sorted(context.files):
        if not path.startswith(_ROOTS):
            continue
        if match := _JSON.match(path):
            try:
                messages = _flatten_json(json.loads(context.read_text(path, 2_000_000) or "{}"))
            except ValueError:
                messages = []
            records.append(TranslationRecord(framework=FRAMEWORK, domain="json", locale=match["locale"].replace("-", "_"),
                                             file=path, messages=tuple(messages[:MAX_MESSAGES])))
        elif (match := _PHP.match(path)) and facts is not None:
            returned = (facts.file(path) or {}).get("returns")
            records.append(TranslationRecord(framework=FRAMEWORK, domain=match["domain"], locale=match["locale"].replace("-", "_"),
                                             file=path, messages=tuple(_flatten_literal(returned)[:MAX_MESSAGES])))
    return records


def provide_templates(context: RevisionContext) -> list[TemplateRecord]:
    return [TemplateRecord(framework=FRAMEWORK, engine="blade", file=path,
                           kind="email" if re.search(r"(^|/)(e?mails?)/", path) else "view")
            for path in sorted(context.files) if path.startswith("resources/views/") and path.endswith(".blade.php")]
