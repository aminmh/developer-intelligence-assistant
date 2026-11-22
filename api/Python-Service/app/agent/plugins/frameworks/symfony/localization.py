"""Symfony translation catalogues and Twig templates (capabilities ``translations``, ``templates``).

Catalogues are ``translations/<domain>.<locale>.<yaml|yml|xlf|xliff|json>`` (Translation docs);
nested YAML keys are joined with dots, as the translator does. Templates are ``templates/**/*.twig``.
No PHP is parsed.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ElementTree
from typing import Any

from agent.core.records import TemplateRecord, TranslationRecord
from agent.core.revision import RevisionContext
from agent.plugins.frameworks.symfony.routes import FRAMEWORK, load_yaml

_CATALOGUE = re.compile(r"^translations/(?:.*/)?(?P<domain>[^/.]+)(?:\+intl-icu)?\.(?P<locale>[A-Za-z]{2,3}(?:[_-][A-Za-z0-9]{2,8})*)\.(?P<ext>ya?ml|xlf|xliff|json)$")
MAX_MESSAGES = 5_000


def _flatten(value: Any, prefix: str = "") -> list[tuple[str, str]]:
    if isinstance(value, dict):
        rows: list[tuple[str, str]] = []
        for key, item in value.items():
            rows.extend(_flatten(item, f"{prefix}.{key}" if prefix else str(key)))
        return rows
    return [(prefix, str(value))] if prefix and value is not None and not isinstance(value, list) else []


def _xliff(text: str) -> list[tuple[str, str]] | None:
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return None
    rows = []
    for unit in root.iter():
        tag = unit.tag.rsplit("}", 1)[-1]
        if tag not in ("trans-unit", "unit"):
            continue
        texts = {child.tag.rsplit("}", 1)[-1]: "".join(child.itertext()) for child in unit.iter() if child is not unit}
        key = unit.get("resname") or unit.get("name") or texts.get("source") or unit.get("id")
        if key:
            rows.append((key, texts.get("target", texts.get("source", ""))))
    return rows


def provide_translations(context: RevisionContext) -> list[TranslationRecord]:
    records = []
    for path in sorted(context.files):
        match = _CATALOGUE.match(path)
        if not match:
            continue
        text = context.read_text(path, 2_000_000)
        if text is None:
            continue
        ext = match["ext"]
        if ext in ("yml", "yaml"):
            messages = _flatten(load_yaml(text) or {})
        elif ext == "json":
            try:
                messages = _flatten(json.loads(text))
            except ValueError:
                messages = []
        else:
            messages = _xliff(text) or []
        records.append(TranslationRecord(framework=FRAMEWORK, domain=match["domain"], locale=match["locale"].replace("-", "_"),
                                         file=path, messages=tuple(messages[:MAX_MESSAGES])))
    return records


def provide_templates(context: RevisionContext) -> list[TemplateRecord]:
    return [TemplateRecord(framework=FRAMEWORK, engine="twig", file=path,
                           kind="email" if re.search(r"(^|/)(e?mails?|emails)/", path) else "view")
            for path in sorted(context.files) if path.startswith("templates/") and path.endswith(".twig")]
