"""Project-declared intentionally public routes for the route-authorization check.

Some frameworks have no way to say a route is public on purpose (Symfony has
``PUBLIC_ACCESS``; Laravel has nothing), so a login, OTP or guest-cart route is
otherwise reported as "no authorization". A project lists such routes as patterns
in its Code Quality settings; a route with no protection that matches one is
reported public, with the pattern as the reason. Framework-neutral.

Pattern: ``METHOD /path``. ``METHOD`` is an HTTP method, ``ANY`` or ``*`` (every
method); a bare ``/path`` means every method. In the path, ``*`` matches any
characters including ``/``; placeholders are matched as written in the report
(``/api/v1/cart/items/{cartItemId}``).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

MAX_PUBLIC_ROUTE_PATTERNS = 100
MAX_PUBLIC_ROUTE_PATTERN_LENGTH = 300
METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "ANY", "*"})


def normalize_public_route_patterns(value: object) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_PUBLIC_ROUTE_PATTERNS:
        raise ValueError(f"Provide at most {MAX_PUBLIC_ROUTE_PATTERNS} public route patterns.")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError("Each public route pattern must be text.")
        text = " ".join(item.split())
        if not text:
            continue
        method, _, path = text.partition(" ") if " " in text else ("*", "", text)
        method = method.upper()
        if (method not in METHODS or not path.startswith("/") or " " in path
                or len(text) > MAX_PUBLIC_ROUTE_PATTERN_LENGTH or any(ord(c) < 32 for c in path)):
            raise ValueError("Public route patterns look like 'POST /api/v1/auth/*': a method (or * for any) "
                             "and a path starting with '/'.")
        pattern = f"{'*' if method == 'ANY' else method} {path}"
        if pattern not in normalized:
            normalized.append(pattern)
    return sorted(normalized)


def matching_pattern(method: str, path: str, patterns: list[str]) -> str | None:
    """The first pattern that declares ``method path`` public, or None."""
    for pattern in patterns:
        pattern_method, _, pattern_path = pattern.partition(" ")
        if pattern_method not in ("*", method.upper()):
            continue
        if fnmatchcase(path, pattern_path):
            return pattern
    return None


class PublicRouteStore:
    """Per-project intentionally public route patterns."""

    def __init__(self, path: Path | None = None):
        from code_quality.service import _store_path

        self.path = path or _store_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS assessment_public_routes (
                project_id TEXT PRIMARY KEY, patterns_json TEXT NOT NULL, updated_at TEXT NOT NULL)""")

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def patterns(self, project_id: str) -> list[str]:
        with self._connect() as db:
            row = db.execute("SELECT patterns_json FROM assessment_public_routes WHERE project_id=?",
                             (project_id,)).fetchone()
        return json.loads(row["patterns_json"]) if row else []

    def save(self, project_id: str, patterns: Any) -> list[str]:
        value = normalize_public_route_patterns(patterns)
        with self._connect() as db:
            db.execute("INSERT INTO assessment_public_routes(project_id,patterns_json,updated_at) VALUES(?,?,?) "
                       "ON CONFLICT(project_id) DO UPDATE SET patterns_json=excluded.patterns_json,"
                       "updated_at=excluded.updated_at",
                       (project_id, json.dumps(value, ensure_ascii=False), datetime.now(timezone.utc).isoformat()))
        return value
