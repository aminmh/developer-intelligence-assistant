"""Localization evidence over translation catalogues and templates (gap-closing plan, task 18).

Framework-neutral; reads ``TranslationRecord`` rows (capability ``translations``) and
``TemplateRecord`` rows (``templates``) that plugins provide (evidence spec clauses 7-8). Per
catalogue domain it reports keys missing in a locale, placeholders that differ between locales,
and messages identical across locales (likely untranslated). When the project has catalogues,
it also reports user-facing text hardcoded in templates and number formatting that ignores the
locale. Without catalogues, localization is left unassessed with a reason.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from agent.core.capability import Capability
from agent.core.records import TemplateRecord, TranslationRecord
from code_quality.evidence_checks import capped, finding, line_of, plural

NAME = "localization-evidence"
VERSION = "localization-evidence-1"
CATEGORY = "localization"
_PLACEHOLDER = re.compile(r"%[A-Za-z_][\w.]*%|\{\{\s*[\w.]+\s*\}\}|\{[\w.]+\}|(?<![\w:]):[a-z_]\w*|%(?:\d+\$)?[sdf]")
_TEMPLATE_CODE = re.compile(r"\{\{--.*?--\}\}|\{#.*?#\}|\{\{.*?\}\}|\{!!.*?!!\}|\{%.*?%\}|<\?(?:php|=).*?\?>|<!--.*?-->", re.S)
_SKIP_BLOCKS = re.compile(r"<(script|style|pre|code)\b.*?</\1>", re.S | re.I)
_TEXT = re.compile(r">([^<>]+)<")
_ATTRIBUTE_TEXT = re.compile(r"""\b(placeholder|title|alt|aria-label)\s*=\s*["']([^"'{}<>@]+)["']""", re.I)
_DIRECTIVE = re.compile(r"^\s*@\w+")
_LOCALE_BLIND = re.compile(r"\bnumber_format\s*\(|\|\s*number_format\b|\.toFixed\s*\(")
MIN_WORD_LETTERS = 3


def _placeholders(text: str) -> frozenset[str]:
    return frozenset(re.sub(r"\s+", "", p) for p in _PLACEHOLDER.findall(text or ""))


def _key_line(record: TranslationRecord, key: str, cache: dict[str, str], context: Any) -> int:
    text = cache.setdefault(record.file, context.read_text(record.file, 2_000_000) or "")
    leaf = key.rsplit(".", 1)[-1]
    return line_of(text, re.compile(rf"""(^|[\s"'])({re.escape(key)}|{re.escape(leaf)})["']?\s*(:|=>|")"""))


def _catalogues(records: list[TranslationRecord], context: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    texts: dict[str, str] = {}
    by_domain: dict[tuple[str, str], dict[str, TranslationRecord]] = defaultdict(dict)
    for record in records:
        by_domain[(record.framework, record.domain)][record.locale] = record
    for (_framework, domain), locales in sorted(by_domain.items()):
        if len(locales) < 2:
            continue
        messages = {locale: dict(record.messages) for locale, record in locales.items()}
        reference = max(sorted(messages), key=lambda locale: len(messages[locale]))
        keys = set().union(*(set(m) for m in messages.values()))
        for locale, record in sorted(locales.items()):
            missing = sorted(keys - set(messages[locale]))
            if missing:
                rows.append(finding(record.file, 1, "localization-missing-keys", CATEGORY, "warning",
                                    f"{plural(len(missing), 'key')} of domain '{domain}' have no {locale} message "
                                    f"(e.g. {', '.join(missing[:4])}); users of that locale see raw keys or fallback text."))
        identical: dict[str, list[str]] = defaultdict(list)
        for key in sorted(keys):
            values = {locale: messages[locale][key] for locale in messages if key in messages[locale]}
            expected = _placeholders(values.get(reference, ""))
            for locale, value in sorted(values.items()):
                if locale != reference and reference in values and _placeholders(value) != expected:
                    record = locales[locale]
                    rows.append(finding(record.file, _key_line(record, key, texts, context), "localization-placeholder-mismatch",
                                        CATEGORY, "error",
                                        f"'{domain}.{key}' in {locale} has placeholders {sorted(_placeholders(value)) or 'none'} "
                                        f"but {reference} has {sorted(expected) or 'none'}; the message renders wrong or fails."))
            texts_by_value = defaultdict(list)
            for locale, value in values.items():
                if sum(ch.isalpha() for ch in value) >= 4 and not _PLACEHOLDER.fullmatch(value.strip()):
                    texts_by_value[value.strip().lower()].append(locale)
            for value, same in texts_by_value.items():
                if len(same) > 1:
                    identical[" and ".join(sorted(same))].append(key)
        for pair, same_keys in sorted(identical.items()):
            first = locales[pair.split(" and ")[-1]]
            rows.append(finding(first.file, _key_line(first, same_keys[0], texts, context), "localization-identical-messages",
                                CATEGORY, "info",
                                f"{plural(len(same_keys), 'message')} of domain '{domain}' are identical in {pair} "
                                f"(e.g. {', '.join(same_keys[:4])}); they may be untranslated."))
    return rows


def _templates(templates: list[TemplateRecord], context: Any) -> tuple[list[dict[str, Any]], int]:
    rows, hardcoded_total = [], 0
    for template in templates:
        text = context.read_text(template.file, 512_000)
        if text is None:
            continue
        stripped = _SKIP_BLOCKS.sub(lambda m: "\n" * m.group(0).count("\n"), text)
        stripped = _TEMPLATE_CODE.sub(lambda m: "\n" * m.group(0).count("\n"), stripped)
        found: list[tuple[int, str]] = []
        for match in _TEXT.finditer(stripped):
            for piece in match.group(1).splitlines():
                words = piece.strip()
                if (sum(ch.isalpha() for ch in words) >= MIN_WORD_LETTERS and re.search(r"[A-Za-z]{2,}", words)
                        and not _DIRECTIVE.match(words) and not words.startswith(("&", "http"))):
                    found.append((stripped.count("\n", 0, match.start()) + 1, words))
        for match in _ATTRIBUTE_TEXT.finditer(stripped):
            if sum(ch.isalpha() for ch in match.group(2)) >= MIN_WORD_LETTERS:
                found.append((stripped.count("\n", 0, match.start()) + 1, match.group(2).strip()))
        if found:
            hardcoded_total += len(found)
            line, sample = min(found)
            rows.append(finding(template.file, line, "localization-hardcoded-text", CATEGORY, "warning",
                                f"{template.file} shows {plural(len(found), 'text')} that bypass the translator "
                                f"(e.g. '{sample[:60]}'), so they appear untranslated in every locale."))
        for number, row in enumerate(text.splitlines(), 1):
            if _LOCALE_BLIND.search(row):
                rows.append(finding(template.file, number, "localization-locale-blind-number", CATEGORY, "info",
                                    "Numbers are formatted with fixed separators instead of the user's locale."))
                break
    return rows, hardcoded_total


def run(collections: dict[Capability, Any], context: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    records = [r for r in collections[Capability.TRANSLATIONS].records if isinstance(r, TranslationRecord)]
    templates = [r for r in collections[Capability.TEMPLATES].records if isinstance(r, TemplateRecord)]
    locales = sorted({r.locale for r in records})
    summary: dict[str, Any] = {"catalogues": len(records), "locales": locales,
                               "domains": len({(r.framework, r.domain) for r in records}), "templates": len(templates)}
    if not records:
        summary["reason"] = "No translation catalogues were found, so localization is not assessed."
        return [], summary
    rows = [finding(sorted(r.file for r in records)[0], 1, "localization-inventory", CATEGORY, "info",
                    f"{plural(len(records), 'translation catalogue')} in {plural(summary['domains'], 'domain')} "
                    f"for {plural(len(locales), 'locale')} ({', '.join(locales[:8])}).")]
    rows += _catalogues(records, context)
    template_rows, summary["hardcoded_texts"] = _templates(templates, context)
    rows += template_rows
    return capped(rows, summary)


def check():
    from code_quality.framework_evidence import FrameworkCheck

    return FrameworkCheck(NAME, VERSION, (Capability.TRANSLATIONS, Capability.TEMPLATES), run,
                          accepts_context=True, runs_without_providers=True, category=CATEGORY)
