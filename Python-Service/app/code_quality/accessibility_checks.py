"""Accessibility evidence in templates (gap-closing plan, task 19; evidence spec clause 9).

Framework-neutral; reads ``TemplateRecord`` rows (capability ``templates``) and plain HTML files
from the evidence inventory. Per template it checks the document language (``<html lang>``),
image alternative text, data-table header cells, and the contrast of colour pairs it can compute
(an inline style or a ``<style>`` rule that sets both a text and a background colour as hex or
``rgb()``; WCAG 2.x contrast ratio, 4.5:1 for normal text). Front-end component checks report
their own accessibility findings separately.
"""

from __future__ import annotations

import re
from typing import Any

from agent.core.capability import Capability
from agent.core.records import TemplateRecord
from code_quality.evidence_checks import capped, files_of_kind, finding, plural

NAME = "accessibility-evidence"
VERSION = "accessibility-evidence-1"
CATEGORY = "accessibility"
MIN_CONTRAST = 4.5
_HTML_OPEN = re.compile(r"<html\b[^>]*>", re.I)
_IMG = re.compile(r"<img\b[^>]*>", re.I | re.S)
_TABLE = re.compile(r"<table\b([^>]*)>(.*?)</table>", re.I | re.S)
_STYLE_ATTR = re.compile(r"""\bstyle\s*=\s*(["'])(.*?)\1""", re.I | re.S)
_STYLE_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
_STYLE_BLOCK = re.compile(r"<style\b[^>]*>(.*?)</style>", re.I | re.S)
_COLOR = re.compile(r"(?:^|;)\s*color\s*:\s*([^;!]+)", re.I)
_BACKGROUND = re.compile(r"(?:^|;)\s*background(?:-color)?\s*:\s*([^;!]+)", re.I)


def _rgb(value: str) -> tuple[float, float, float] | None:
    value = value.strip().lower()
    if (match := re.fullmatch(r"#([0-9a-f]{3}|[0-9a-f]{6})", value)):
        digits = match.group(1)
        digits = "".join(ch * 2 for ch in digits) if len(digits) == 3 else digits
        return tuple(int(digits[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]
    if (match := re.fullmatch(r"rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*(?:,\s*1(?:\.0+)?\s*)?\)", value)):
        return tuple(min(255, int(part)) / 255 for part in match.groups())  # type: ignore[return-value]
    named = {"white": (1.0, 1.0, 1.0), "black": (0.0, 0.0, 0.0)}
    return named.get(value)


def contrast(foreground: str, background: str) -> float | None:
    def luminance(rgb: tuple[float, float, float]) -> float:
        channels = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
        return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]

    fg, bg = _rgb(foreground), _rgb(background.split()[0] if background.strip() else background)
    if fg is None or bg is None:
        return None
    light, dark = sorted((luminance(fg), luminance(bg)), reverse=True)
    return round((light + 0.05) / (dark + 0.05), 2)


def _line(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _template_rows(path: str, text: str) -> list[dict[str, Any]]:
    rows = []
    for match in _HTML_OPEN.finditer(text):
        if not re.search(r"\blang\s*=", match.group(0), re.I):
            rows.append(finding(path, _line(text, match.start()), "accessibility-missing-lang", CATEGORY, "error",
                                "<html> has no lang attribute, so screen readers cannot choose the right pronunciation."))
    missing_alt = [m for m in _IMG.finditer(text) if not re.search(r"\balt\s*=", m.group(0), re.I)]
    if missing_alt:
        rows.append(finding(path, _line(text, missing_alt[0].start()), "accessibility-image-without-alt", CATEGORY, "error",
                            f"{plural(len(missing_alt), 'image')} in {path} have no alt attribute; screen readers announce the file name or nothing."))
    for match in _TABLE.finditer(text):
        attributes, body = match.groups()
        if re.search(r"""role\s*=\s*["'](presentation|none)["']""", attributes, re.I):
            continue
        if re.search(r"<td\b", body, re.I) and not re.search(r"<th\b", body, re.I):
            rows.append(finding(path, _line(text, match.start()), "accessibility-table-without-headers", CATEGORY, "warning",
                                "A data table has no <th> header cells, so screen readers cannot tell which column or row a value belongs to."))
    declarations = [(m.start(), m.group(2)) for m in _STYLE_ATTR.finditer(text)]
    for block in _STYLE_BLOCK.finditer(text):
        declarations += [(block.start(1) + rule.start(), rule.group(2)) for rule in _STYLE_RULE.finditer(block.group(1))]
    for offset, style in declarations:
        color, background = _COLOR.search(";" + style), _BACKGROUND.search(";" + style)
        if color and background and (ratio := contrast(color.group(1), background.group(1))) is not None and ratio < MIN_CONTRAST:
            rows.append(finding(path, _line(text, offset), "accessibility-low-contrast", CATEGORY, "warning",
                                f"Text colour {color.group(1).strip()} on {background.group(1).strip()} has a contrast ratio of "
                                f"{ratio}:1, below the {MIN_CONTRAST}:1 WCAG minimum for normal text."))
    return rows


def run(collections: dict[Capability, Any], context: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    templates = {r.file: r.engine for r in collections[Capability.TEMPLATES].records if isinstance(r, TemplateRecord)}
    for path in files_of_kind(context, "templates"):
        if path.lower().endswith((".html", ".htm", ".xhtml")):
            templates.setdefault(path, "html")
    rows: list[dict[str, Any]] = []
    checked = 0
    for path in sorted(templates):
        text = context.read_text(path, 512_000)
        if text is None:
            continue
        checked += 1
        rows += _template_rows(path, text)
    engines: dict[str, int] = {}
    for engine in templates.values():
        engines[engine] = engines.get(engine, 0) + 1
    summary = {"templates_checked": checked, "engines": dict(sorted(engines.items()))}
    if not checked:
        summary["reason"] = "No templates or HTML files were found, so accessibility is assessed only from front-end checks if any."
        return [], summary
    rows.append(finding(sorted(templates)[0], 1, "accessibility-inventory", CATEGORY, "info",
                        f"Checked {plural(checked, 'template')} ({', '.join(f'{k} {v}' for k, v in sorted(engines.items()))}) "
                        "for document language, image alt text, table headers and computable colour contrast."))
    return capped(rows, summary)


def check():
    from code_quality.framework_evidence import FrameworkCheck

    return FrameworkCheck(NAME, VERSION, (Capability.TEMPLATES,), run,
                          accepts_context=True, runs_without_providers=True, category=CATEGORY)
