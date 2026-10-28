"""Validation and matching for project-scoped assessment path exclusions."""
from __future__ import annotations

from functools import lru_cache
from fnmatch import fnmatchcase

MAX_EXCLUSION_PATTERNS = 100
MAX_EXCLUSION_PATTERN_LENGTH = 300


def normalize_exclusion_patterns(value: object) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_EXCLUSION_PATTERNS:
        raise ValueError(f"Provide at most {MAX_EXCLUSION_PATTERNS} exclusion patterns.")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError("Each exclusion pattern must be text.")
        pattern = item.strip().rstrip("/")
        if not pattern:
            continue
        if (len(pattern) > MAX_EXCLUSION_PATTERN_LENGTH or pattern.startswith("/")
                or "\\" in pattern or any(ord(character) < 32 for character in pattern)):
            raise ValueError("Exclusion patterns must be short project-relative paths using '/'.")
        parts = pattern.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise ValueError("Exclusion patterns cannot contain empty, '.' or '..' path segments.")
        if pattern not in normalized:
            normalized.append(pattern)
    return sorted(normalized)


def _matches_segments(pattern: tuple[str, ...], path: tuple[str, ...]) -> bool:
    @lru_cache(maxsize=None)
    def match(pattern_index: int, path_index: int) -> bool:
        if pattern_index == len(pattern):
            return path_index == len(path)
        segment = pattern[pattern_index]
        if segment == "**":
            return (match(pattern_index + 1, path_index)
                    or (path_index < len(path) and match(pattern_index, path_index + 1)))
        return (path_index < len(path)
                and fnmatchcase(path[path_index], segment)
                and match(pattern_index + 1, path_index + 1))

    return match(0, 0)


def matches_exclusion(path: str, patterns: list[str]) -> bool:
    """Match a file or any ancestor directory using project-relative globs."""
    path_parts = tuple(path.split("/"))
    for pattern in patterns:
        pattern_parts = tuple(pattern.split("/"))
        for end in range(1, len(path_parts) + 1):
            if _matches_segments(pattern_parts, path_parts[:end]):
                return True
    return False
