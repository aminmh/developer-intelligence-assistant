#!/usr/bin/env python3
"""Rebuild the domain frequency dictionary for SymSpell typo correction.

Thin wrapper around ``app/domain_dictionary.generate_domain_dictionary`` so the
dictionary can be regenerated manually (and is also regenerated automatically at
the end of code indexing).

Run offline:

    docker compose exec python-api python scripts/generate_domain_dictionary.py
    # or on the host:
    QDRANT_URL=http://localhost:6333 python3 scripts/generate_domain_dictionary.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))

from domain_dictionary import generate_domain_dictionary  # noqa: E402

QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant:6333")


def main() -> None:
    try:
        count = generate_domain_dictionary(QDRANT_URL)
    except Exception as exc:
        print(f"error: could not generate domain dictionary: {exc}")
        raise SystemExit(1)

    if not count:
        print("no terms collected — is there an indexed chunk_code_* collection?")
        raise SystemExit(1)

    print(f"wrote {count} terms to {APP_DIR / 'config' / 'domain_dictionary.txt'}")


if __name__ == "__main__":
    main()
