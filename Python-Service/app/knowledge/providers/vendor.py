"""The ``vendor`` provider: documentation from the installed package inside the revision archive.

Spec clauses 3, 7, 8 and failure clause 4. Reads only the package's own UPGRADE, CHANGELOG and README
files (and a ``docs/`` folder) straight from the ZIP; nothing is extracted or run. Files are split
into sections at Markdown headings, ATX (``## 7.4``) or setext (``7.4`` over ``---``), so excerpts and
version ranges map to the headings the package's authors wrote.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from typing import Any

from knowledge.model import MAX_EXCERPT_CHARS, Excerpt, cap, in_range, parse_version

PROVIDER = "vendor"
MAX_DOC_BYTES = 1024 * 1024
MAX_DOC_FILES = 40
_DOC_NAME = re.compile(r"^(upgrade[^/]*|changelog[^/]*|readme[^/]*|history[^/]*)\.(md|markdown|txt|rst)$|^(changelog|history)$", re.I)
_UPGRADE_FILE = re.compile(r"^upgrade[-_ ]?v?(\d+(?:\.\d+){0,2})\.(md|markdown|txt)$", re.I)
_ATX = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_SETEXT = re.compile(r"^(=+|-+)\s*$")
_VERSION_HEADING = re.compile(r"^\[?v?(\d+(?:\.\d+){0,2})\]?(?:\s|$|[-–(:])")


@dataclass
class Section:
    path: str
    heading: str
    line: int
    text: str


def _package_members(names: list[str], package_path: str) -> dict[str, str]:
    """``{path inside the package: archive member}`` for the package's documentation files."""
    marker = package_path.strip("/") + "/"
    found: dict[str, str] = {}
    for name in names:
        index = name.find(marker)
        if index < 0 or (index > 0 and name[index - 1] != "/") or name.endswith("/"):
            continue
        inner = name[index + len(marker):]
        depth = inner.count("/")
        base = inner.rsplit("/", 1)[-1]
        if (depth == 0 and _DOC_NAME.match(base)) or (depth <= 2 and inner.lower().startswith("docs/")
                                                      and base.lower().endswith((".md", ".markdown"))):
            found.setdefault(inner, name)
    return dict(sorted(found.items())[:MAX_DOC_FILES])


def sections(path: str, text: str) -> list[Section]:
    """Split a document at its headings; text before the first heading is its own section."""
    lines = text.splitlines()
    out: list[Section] = []
    heading, start, body = "", 1, []

    def flush() -> None:
        content = "\n".join(body).strip()
        if content or heading:
            out.append(Section(path, heading, start, content))

    i = 0
    while i < len(lines):
        line = lines[i]
        atx = _ATX.match(line)
        setext = i + 1 < len(lines) and line.strip() and _SETEXT.match(lines[i + 1]) and not line.startswith((" ", "\t", "-", "*"))
        if atx or setext:
            flush()
            heading, start, body = (atx.group(2) if atx else line.strip()), i + 1, []
            i += 1 if atx else 2
            continue
        body.append(line)
        i += 1
    flush()
    return out


def _words(topic: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9_]{3,}", topic.lower())]


def _score(section: Section, words: list[str]) -> tuple[int, tuple[int, int, int], int]:
    """Rank by how many distinct topic words match, then by newer version headings, then by occurrences.

    Found on pizza-builder: counting occurrences alone put an old ``5.1.0`` section that says "cookie"
    often above the ``7.x`` notes the topic asked about.
    """
    haystack = f"{section.heading}\n{section.text}".lower()
    distinct = sum(1 for word in words if word in haystack)
    heading = _VERSION_HEADING.match(section.heading)
    version = (parse_version(heading.group(1)) if heading else None) or (0, 0, 0)
    return distinct, version, sum(haystack.count(word) for word in words)


class VendorDocs:
    """The installed package's documentation inside one revision archive."""

    def __init__(self, archive: bytes):
        self.archive = archive
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            self.names = [info.filename for info in bundle.infolist() if not info.is_dir()]

    def documents(self, package_path: str) -> dict[str, str]:
        members = _package_members(self.names, package_path)
        out: dict[str, str] = {}
        with zipfile.ZipFile(io.BytesIO(self.archive)) as bundle:
            for inner, member in members.items():
                info = bundle.getinfo(member)
                if info.file_size <= MAX_DOC_BYTES:
                    out[inner] = bundle.read(member).decode("utf-8", errors="replace")
        return out

    def present(self, package_path: str) -> bool:
        marker = package_path.strip("/") + "/"
        return any(name.startswith(marker) or f"/{marker}" in name for name in self.names)


def _excerpt(record: Any, section: Section, limit: int = MAX_EXCERPT_CHARS) -> Excerpt:
    heading = f"{section.heading}\n\n" if section.heading else ""
    text, truncated = cap(heading + section.text, limit)
    return Excerpt(provider=PROVIDER, library=record.name, version_or_ref=record.version,
                   path_or_url=f"{record.path}/{section.path}", line=section.line, text=text, truncated=truncated,
                   section=section.heading)


def topic_excerpts(docs: VendorDocs, record: Any, topic: str, limit: int = 5) -> list[Excerpt]:
    """Clause 3: the sections of the installed package's documents that best match ``topic``."""
    all_sections = [s for path, text in docs.documents(record.path).items() for s in sections(path, text)]
    words = _words(topic)
    if not words:  # no topic: the start of the README, else of the first document
        readme = [s for s in all_sections if s.path.lower().startswith("readme")]
        return [_excerpt(record, s) for s in (readme or all_sections)[:1]]
    scored = [(s, _score(s, words)) for s in all_sections]
    matching = [(s, score) for s, score in scored if score[0] > 0]
    ranked = [s for s, _ in sorted(matching, key=lambda item: (-item[1][0], tuple(-n for n in item[1][1]), -item[1][2],
                                                                    item[0].path, item[0].line))]
    return [_excerpt(record, s) for s in ranked[:limit]]


def upgrade_excerpts(docs: VendorDocs, record: Any, target: str) -> tuple[list[Excerpt], list[str]]:
    """Clause 8: version sections after the installed version, up to ``target``; returns (excerpts, versions found)."""
    installed, wanted = parse_version(record.version), parse_version(target)
    if installed is None or wanted is None:
        return [], []
    found: list[tuple[tuple[int, int, int], Excerpt]] = []
    for path, text in docs.documents(record.path).items():
        base = path.rsplit("/", 1)[-1]
        upgrade_file = _UPGRADE_FILE.match(base)
        if upgrade_file:  # UPGRADE-8.0.md covers the 8.0 line as a whole
            version = parse_version(upgrade_file.group(1))
            if version and in_range(version, installed, wanted, precision=2):
                for section in sections(path, text):
                    found.append((version, _excerpt(record, section)))
            continue
        for section in sections(path, text):
            heading = _VERSION_HEADING.match(section.heading)
            version = parse_version(heading.group(1)) if heading else None
            precision = heading.group(1).count(".") + 1 if heading else 3
            if version and in_range(version, installed, wanted, precision=min(precision, 3)):
                found.append((version, _excerpt(record, section)))
    found.sort(key=lambda item: (item[0], item[1].path_or_url, item[1].line or 0))
    versions = sorted({".".join(map(str, v)) for v, _ in found}, key=lambda s: tuple(map(int, s.split("."))))
    return [excerpt for _, excerpt in found], versions
