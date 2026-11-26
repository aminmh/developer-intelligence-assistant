#!/usr/bin/env python3
"""Register the Indexr bundle in a Symfony app's config/bundles.php.

Usage: register-symfony-bundle.py PROJECT_DIR
Prints "added", "present" or "missing-file" (the app has no config/bundles.php, so nothing was changed).

Symfony Flex registers a bundle only through a recipe, which the Indexr package does not have, so without this
line the library is installed but never loaded and no spans are produced. The line is added as the last entry
of the returned array and nothing else in the file is touched.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

BUNDLE = "Indexr\\Otel\\Symfony\\IndexerOtelBundle"
ENTRY = f"    {BUNDLE}::class => ['all' => true],\n"


def register(project: Path) -> str:
    path = project / "config" / "bundles.php"
    if not path.is_file():
        return "missing-file"
    text = path.read_text()
    if "IndexerOtelBundle" in text:
        return "present"
    closing = list(re.finditer(r"^\];[ \t]*$", text, re.M))
    if not closing:
        raise SystemExit(f"{path} does not end with a plain '];' array; add {BUNDLE}::class => ['all' => true] by hand.")
    end = closing[-1].start()
    path.write_text(text[:end] + ENTRY + text[end:])
    return "added"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    print(register(Path(argv[1])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
