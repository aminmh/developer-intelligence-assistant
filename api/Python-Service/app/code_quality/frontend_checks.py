"""Static checks for Angular, React, and Vue source: accessibility, security, reliability, performance.

Plan: @.archcore/frontend-assist/frontend-developer-assistance.plan.md (Phase 6, task 32).

These run in-process on source text, with no Node toolchain and no project
dependencies installed, so they work on any uploaded revision. They are not a
replacement for ESLint or the TypeScript compiler: each rule looks for one
pattern that is wrong in almost every codebase and reports it with file and
line. Markup is read tag by tag (Vue templates, Angular templates, and JSX
through its syntax tree), so a rule sees an element's attributes, not a regex
guess over the whole file.

Areas: ``accessibility`` is reported on its own. The other areas map onto the
Code Quality categories (security, reliability, performance, maintainability),
so an assessment can count them.
"""

from __future__ import annotations

import io
import os
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

CHECKS_VERSION = "frontend-checks-1"
MAX_FILES = 4000
MAX_FILE_BYTES = 1_000_000
MAX_FINDINGS = 1000
MAX_PER_RULE = 150
LARGE_COMPONENT_LINES = 600
AREAS = ("accessibility", "security", "reliability", "performance", "maintainability")
_SKIP_DIRS = {"node_modules", "vendor", "dist", "build", "coverage", ".next", ".nuxt", ".output", ".angular",
              "storybook-static", "__tests__", "__mocks__", "e2e", "cypress", "public"}
_SKIP_FILE = re.compile(r"\.(spec|test|stories|d|min)\.[cm]?[jt]sx?$")
_SUFFIXES = (".vue", ".jsx", ".tsx", ".ts", ".js", ".mjs", ".html")

# rule id -> (area, severity, message)
RULES: dict[str, tuple[str, str, str]] = {
    "a11y-img-alt": ("accessibility", "WARNING", "Image has no alt text. Add alt, or alt=\"\" if it is decorative."),
    "a11y-click-on-noninteractive": ("accessibility", "WARNING", "A click handler on a {tag} cannot be reached by keyboard. "
                                     "Use a button, or add role and tabindex with a key handler."),
    "a11y-control-no-name": ("accessibility", "WARNING", "This {tag} has no accessible name: it shows no text and has no aria-label."),
    "a11y-input-no-label": ("accessibility", "WARNING", "Form field has no label. Wrap it in a label, or add aria-label or a matching label for=."),
    "a11y-positive-tabindex": ("accessibility", "WARNING", "A positive tabindex breaks the natural keyboard order. Use 0 or -1."),
    "a11y-link-no-href": ("accessibility", "WARNING", "Link has a click handler but no destination, so it is not a link to keyboards and "
                          "screen readers. Use a button, or give it an href."),
    "frontend-raw-html": ("security", "WARNING", "Raw HTML is rendered ({how}). If any part of it comes from a user or an API, this allows "
                          "script injection. Render text, or sanitize the HTML first."),
    "frontend-token-in-web-storage": ("security", "WARNING", "A credential is written to browser storage, where any script on the page "
                                      "can read it. Prefer an HttpOnly cookie."),
    "frontend-eval": ("security", "ERROR", "Code is built from a string and executed ({how})."),
    "frontend-list-without-key": ("reliability", "WARNING", "List items are rendered without a key, so the framework can mix up their state "
                                  "when the list changes."),
    "frontend-index-as-key": ("reliability", "INFO", "The list index is used as the key. Reordering or removing items will reuse the wrong "
                              "element state. Use a stable ID."),
    "frontend-timer-not-cleared": ("reliability", "WARNING", "This file starts an interval but never clears one, so it keeps running after "
                                   "the component is gone."),
    "frontend-listener-not-removed": ("reliability", "WARNING", "This file adds a window or document listener but never removes one."),
    "frontend-ngfor-without-trackby": ("performance", "INFO", "*ngFor without trackBy re-creates every row when the list changes."),
    "frontend-large-component": ("maintainability", "INFO", "This component file has {lines} lines. Files this long are hard to review and "
                                 "test; consider splitting it."),
}
_INTERACTIVE = {"button", "a", "input", "select", "textarea", "summary", "label", "option", "details"}
_NAME_ATTRIBUTES = ("aria-label", "aria-labelledby", "title", ":aria-label", "[attr.aria-label]", "[aria-label]",
                    ":title", "[title]", "v-bind:aria-label", "v-tooltip", "matTooltip", "[matTooltip]")
_CLICK = ("@click", "v-on:click", "(click)", "onclick")
_CREDENTIAL_KEY = re.compile(r"(token|jwt|secret|password|passwd|api[_-]?key|credential|session[_-]?id|bearer)", re.IGNORECASE)


@dataclass
class Element:
    tag: str
    attributes: dict[str, str | None]
    line: int
    has_text: bool = False                 # shows text or an expression to the user
    inside: tuple[str, ...] = field(default_factory=tuple)   # tags of enclosing elements, nearest last

    def has(self, *names: str) -> bool:
        lowered = {key.lower() for key in self.attributes}
        return any(name.lower() in lowered for name in names)

    def value(self, name: str) -> str | None:
        return next((value for key, value in self.attributes.items() if key.lower() == name.lower()), None)

    def bound(self, name: str) -> bool:
        """The attribute is present in any of its plain or framework-bound spellings."""
        return self.has(name, f":{name}", f"v-bind:{name}", f"[{name}]", f"[attr.{name}]")


# ── markup scanners ──────────────────────────────────────────────────────────

_TAG_NAME = re.compile(r"[A-Za-z][A-Za-z0-9.:-]*")
_VOID = {"img", "input", "br", "hr", "meta", "link", "area", "base", "col", "embed", "source", "track", "wbr"}


def template_elements(markup: str, first_line: int = 1) -> Iterator[Element]:
    """Elements of an HTML-like template (Vue or Angular), in document order.

    A small scanner instead of a regex: attribute values are quoted strings that may contain ``>``.
    """
    position, length = 0, len(markup)
    stack: list[tuple[str, Element]] = []
    while position < length:
        start = markup.find("<", position)
        if start < 0:
            break
        if markup.startswith("<!--", start):
            end = markup.find("-->", start + 4)
            position = length if end < 0 else end + 3
            continue
        if markup.startswith("</", start):
            name = _TAG_NAME.match(markup, start + 2)
            close = markup.find(">", start)
            if name:
                tag = name.group(0).lower()
                for index in range(len(stack) - 1, -1, -1):
                    if stack[index][0] == tag:
                        del stack[index:]
                        break
            position = length if close < 0 else close + 1
            continue
        name = _TAG_NAME.match(markup, start + 1)
        if not name:
            position = start + 1
            continue
        tag, cursor = name.group(0), name.end()
        attributes: dict[str, str | None] = {}
        self_closing = False
        while cursor < length:
            while cursor < length and markup[cursor].isspace():
                cursor += 1
            if cursor >= length:
                break
            if markup[cursor] == ">":
                cursor += 1
                break
            if markup.startswith("/>", cursor):
                self_closing, cursor = True, cursor + 2
                break
            key_start = cursor
            while cursor < length and not markup[cursor].isspace() and markup[cursor] not in "=>" and not markup.startswith("/>", cursor):
                cursor += 1
            key = markup[key_start:cursor]
            if not key:
                cursor += 1
                continue
            value: str | None = None
            if cursor < length and markup[cursor] == "=":
                cursor += 1
                if cursor < length and markup[cursor] in "\"'":
                    quote = markup[cursor]
                    end = markup.find(quote, cursor + 1)
                    end = length if end < 0 else end
                    value, cursor = markup[cursor + 1:end], end + 1
                else:
                    value_start = cursor
                    while cursor < length and not markup[cursor].isspace() and markup[cursor] != ">":
                        cursor += 1
                    value = markup[value_start:cursor]
            attributes[key] = value
        element = Element(tag=tag, attributes=attributes, line=first_line + markup.count("\n", 0, start),
                          inside=tuple(name for name, _element in stack))
        # Text directly inside any open element counts as visible text for all of them.
        if not self_closing and tag.lower() not in _VOID:
            following = markup.find("<", cursor)
            text = markup[cursor: following if following >= 0 else length]
            if text.strip():
                element.has_text = True
                for _name, parent in stack:
                    parent.has_text = True
            stack.append((tag.lower(), element))
        elif tag.lower() in ("img", "svg") and (element.bound("alt") and element.value("alt") or element.bound("aria-label")):
            for _name, parent in stack:
                parent.has_text = True
        yield element
        position = cursor
    return


def jsx_elements(source: bytes, grammar: str) -> list[Element]:
    """Elements of JSX, read from the syntax tree."""
    from projects.frontend.parsing import field as node_field, text, walk
    from tree_sitter_language_pack import get_parser

    root = get_parser(grammar).parse(source).root_node
    elements: list[Element] = []
    for node in walk(root):
        if node.type not in ("jsx_self_closing_element", "jsx_opening_element"):
            continue
        name = node_field(node, "name")
        if name is None:
            continue
        attributes: dict[str, str | None] = {}
        for child in node.named_children:
            if child.type != "jsx_attribute" or not child.named_children:
                continue
            key = text(child.named_children[0], source)
            raw = text(child.named_children[1], source) if len(child.named_children) > 1 else None
            attributes[key] = raw[1:-1] if raw and raw[:1] in "\"'{" else raw
        if any(child.type == "jsx_expression" and text(child, source).startswith("{...") for child in node.named_children):
            attributes["{...spread}"] = None            # props passed through: an attribute may be there at run time
        has_text = False
        parent = node.parent
        if node.type == "jsx_opening_element" and parent is not None:
            inner = source[node.end_byte:parent.end_byte].decode("utf-8", errors="replace")
            has_text = bool(re.sub(r"<[^<>]*>", "", inner).strip(" \n\t")) or "aria-label" in inner or "alt=" in inner
        inside = []
        ancestor = parent.parent if node.type == "jsx_opening_element" and parent is not None else parent
        while ancestor is not None:
            if ancestor.type == "jsx_element":
                opening = next((c for c in ancestor.named_children if c.type == "jsx_opening_element"), None)
                opening_name = node_field(opening, "name") if opening is not None else None
                if opening_name is not None:
                    inside.append(text(opening_name, source).lower())
            ancestor = ancestor.parent
        elements.append(Element(tag=text(name, source), attributes=attributes, line=int(node.start_point.row) + 1,
                                has_text=has_text, inside=tuple(reversed(inside))))
    return elements


# ── rules ────────────────────────────────────────────────────────────────────

def _markup_findings(elements: list[Element], framework: str) -> Iterator[tuple[str, int, dict[str, Any]]]:
    labelled_ids = {element.value("for") or element.value("htmlFor") for element in elements if element.tag.lower() == "label"}
    for element in elements:
        tag = element.tag.lower()
        spread = element.has("{...spread}", "v-bind")          # attributes may arrive at run time
        clicks = element.has(*_CLICK) if framework != "jsx" else element.has("onClick")
        named = element.has_text or any(element.has(name) for name in _NAME_ATTRIBUTES) or spread
        if tag == "img" and not (element.bound("alt") or element.bound("aria-hidden") or element.has("role") or spread):
            yield "a11y-img-alt", element.line, {}
        if clicks and tag in ("div", "span", "li", "td", "tr", "p", "img", "section", "article") \
                and not (element.bound("role") and element.bound("tabindex")) and not spread:
            yield "a11y-click-on-noninteractive", element.line, {"tag": tag}
        if tag == "button" and not named and not element.has("v-text", "v-html", "[innerText]", "[textContent]", "children"):
            yield "a11y-control-no-name", element.line, {"tag": "button"}
        if tag == "a":
            destination = element.bound("href") or element.has("routerLink", "[routerLink]", "to", ":to", "v-bind:to")
            if clicks and not destination and not spread:
                yield "a11y-link-no-href", element.line, {}
            elif destination and not named:
                yield "a11y-control-no-name", element.line, {"tag": "link"}
        if tag in ("input", "select", "textarea") and not spread:
            kind = (element.value("type") or "text").lower()
            identifier = element.value("id")
            labelled = (any(element.bound(name) for name in ("aria-label", "aria-labelledby", "title"))
                        or "label" in element.inside or (identifier is not None and identifier in labelled_ids)
                        or element.bound("id") and not identifier)             # a bound id may match a label at run time
            if kind not in ("hidden", "submit", "button", "reset", "image") and not labelled:
                yield "a11y-input-no-label", element.line, {}
        index = element.value("tabindex") or element.value("tabIndex")
        if index and re.fullmatch(r"\{?\s*[1-9]\d*\s*\}?", index):
            yield "a11y-positive-tabindex", element.line, {}
        if element.has("v-html"):
            yield "frontend-raw-html", element.line, {"how": "v-html"}
        if element.has("[innerHTML]"):
            yield "frontend-raw-html", element.line, {"how": "[innerHTML]"}
        if element.has("dangerouslySetInnerHTML"):
            yield "frontend-raw-html", element.line, {"how": "dangerouslySetInnerHTML"}
        loop = element.value("v-for")
        if loop is not None and tag != "template":
            key = element.value(":key") or element.value("v-bind:key")
            if key is None and not element.has("key"):
                yield "frontend-list-without-key", element.line, {}
            elif key and re.fullmatch(r"\s*(index|idx|i)\s*", key) and re.search(r",\s*(index|idx|i)\s*\)", loop):
                yield "frontend-index-as-key", element.line, {}
        ng_for = element.value("*ngFor")
        if ng_for is not None and "trackBy" not in ng_for:
            yield "frontend-ngfor-without-trackby", element.line, {}
        jsx_key = element.value("key") if framework == "jsx" else None
        if jsx_key and re.fullmatch(r"\s*(index|idx|i)\s*", jsx_key):
            yield "frontend-index-as-key", element.line, {}


_SCRIPT_RULES = (
    ("frontend-eval", re.compile(r"(?<![\w.$])eval\s*\("), {"how": "eval"}),
    ("frontend-eval", re.compile(r"\bnew\s+Function\s*\("), {"how": "new Function"}),
    ("frontend-raw-html", re.compile(r"\bbypassSecurityTrust(Html|Script|Url|ResourceUrl|Style)\s*\("), {"how": "bypassSecurityTrust"}),
    ("frontend-raw-html", re.compile(r"\.innerHTML\s*=(?!=)"), {"how": "innerHTML"}),
)
_STORAGE_WRITE = re.compile(r"\b(localStorage|sessionStorage)\s*\.\s*setItem\s*\(\s*([^,)]{1,120})")
_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)


def _script_findings(script: str, first_line: int = 1) -> Iterator[tuple[str, int, dict[str, Any]]]:
    # Blank out comments but keep line breaks, so commented-out code is not reported and lines stay right.
    code = _COMMENT.sub(lambda match: re.sub(r"[^\n]", " ", match.group(0)), script)
    line = lambda position: first_line + code.count("\n", 0, position)  # noqa: E731
    for rule, pattern, details in _SCRIPT_RULES:
        for match in pattern.finditer(code):
            yield rule, line(match.start()), details
    for match in _STORAGE_WRITE.finditer(code):
        if _CREDENTIAL_KEY.search(match.group(2)):
            yield "frontend-token-in-web-storage", line(match.start()), {}
    if re.search(r"\bsetInterval\s*\(", code) and not re.search(r"\bclearInterval\s*\(", code):
        yield "frontend-timer-not-cleared", line(re.search(r"\bsetInterval\s*\(", code).start()), {}
    added = re.search(r"\b(window|document)\s*\.\s*addEventListener\s*\(", code)
    if added and not re.search(r"\bremoveEventListener\s*\(", code) and "AbortController" not in code:
        yield "frontend-listener-not-removed", line(added.start()), {}


def check_file(path: str, source: bytes) -> list[dict[str, Any]]:
    """Findings for one source file."""
    suffix = Path(path).suffix.lower()
    text = source.decode("utf-8", errors="replace")
    raw: list[tuple[str, int, dict[str, Any]]] = []
    component = False
    if suffix == ".vue":
        from projects.frontend.parsing import split_vue

        scripts, template = split_vue(source)
        component = True
        if template:
            offset = source.count(b"\n", 0, source.find(template)) + 1
            raw.extend(_markup_findings(list(template_elements(template.decode("utf-8", errors="replace"), offset)), "vue"))
        for script, _grammar, offset in scripts:
            raw.extend(_script_findings(script.decode("utf-8", errors="replace"), offset + 1))
    elif suffix == ".html":
        raw.extend(_markup_findings(list(template_elements(text)), "angular"))
    else:
        if suffix in (".jsx", ".tsx") or (suffix == ".js" and re.search(r"<[A-Za-z][\w.]*[\s/>]", text) and "React" in text):
            try:
                elements = jsx_elements(source, "tsx" if suffix == ".tsx" else "javascript")
            except (ValueError, RuntimeError, ImportError):
                elements = []
            component = bool(elements)
            raw.extend(_markup_findings(elements, "jsx"))
        inline = re.search(r"\btemplate\s*:\s*`", text) if "@Component" in text else None
        if inline:                                                   # an Angular component with an inline template
            end = text.find("`", inline.end())
            raw.extend(_markup_findings(list(template_elements(text[inline.end(): end if end > 0 else len(text)],
                                                               text.count("\n", 0, inline.end()) + 1)), "angular"))
        component = component or "@Component" in text
        raw.extend(_script_findings(text))
    lines = len(text.splitlines())
    if component and lines > LARGE_COMPONENT_LINES:
        raw.append(("frontend-large-component", 1, {"lines": lines}))
    findings = []
    for rule, line, details in sorted(set((rule, line, tuple(sorted(details.items()))) for rule, line, details in raw)):
        area, severity, message = RULES[rule]
        findings.append({"file": path, "line": line, "rule": rule, "area": area, "severity": severity,
                         "message": message.format(**dict(details)) if details else message})
    return findings


def _wanted(path: str) -> bool:
    lowered = path.lower()
    if not lowered.endswith(_SUFFIXES) or _SKIP_FILE.search(lowered):
        return False
    if lowered.endswith(".html") and not lowered.endswith(".component.html"):
        return False
    return not any(part in _SKIP_DIRS for part in lowered.split("/")[:-1])


def check_sources(sources: Iterable[tuple[str, bytes]]) -> dict[str, Any]:
    """Run every rule over ``(path, content)`` pairs."""
    findings: list[dict[str, Any]] = []
    per_rule: dict[str, int] = {}
    files_checked = 0
    cut = 0
    for path, source in sources:
        files_checked += 1
        for finding in check_file(path, source):
            per_rule[finding["rule"]] = per_rule.get(finding["rule"], 0) + 1
            if per_rule[finding["rule"]] > MAX_PER_RULE or len(findings) >= MAX_FINDINGS:
                cut += 1
                continue
            findings.append(finding)
    findings.sort(key=lambda f: (AREAS.index(f["area"]), {"ERROR": 0, "WARNING": 1, "INFO": 2}[f["severity"]], f["file"], f["line"]))
    by_area = {area: sum(1 for finding in findings if finding["area"] == area) for area in AREAS}
    notes = [f"{cut} more finding(s) were left out; the most frequent rules are capped at {MAX_PER_RULE}."] if cut else []
    return {"version": CHECKS_VERSION, "files_checked": files_checked, "findings": findings, "by_area": by_area,
            "rule_counts": dict(sorted(per_rule.items())), "notes": notes}


def run_on_directory(root: str | Path) -> dict[str, Any]:
    root = Path(root)

    def sources() -> Iterator[tuple[str, bytes]]:
        count = 0
        for directory, dirs, names in os.walk(root):
            dirs[:] = sorted(name for name in dirs if name.lower() not in _SKIP_DIRS and not name.startswith("."))
            for name in sorted(names):
                path = Path(directory) / name
                relative = path.relative_to(root).as_posix()
                if not _wanted(relative) or count >= MAX_FILES:
                    continue
                try:
                    if path.stat().st_size > MAX_FILE_BYTES:
                        continue
                    data = path.read_bytes()
                except OSError:
                    continue
                count += 1
                yield relative, data

    return check_sources(sources())


def run_on_archive(archive_data: bytes, files: list[str]) -> dict[str, Any] | None:
    """Checks for the assessed files of a revision archive; None when it holds no front-end source."""
    wanted = [path for path in files if _wanted(path)][:MAX_FILES]
    if not any(path.lower().endswith((".vue", ".jsx", ".tsx", ".component.html", ".component.ts")) for path in wanted):
        return None
    selected = set(wanted)

    def sources() -> Iterator[tuple[str, bytes]]:
        with zipfile.ZipFile(io.BytesIO(archive_data)) as archive:
            seen: set[str] = set()
            for member in sorted(archive.infolist(), key=lambda item: item.filename):
                name = member.filename
                if name not in selected or name in seen or member.is_dir() or member.file_size > MAX_FILE_BYTES:
                    continue
                seen.add(name)
                yield name, archive.read(member)

    return check_sources(sources())
