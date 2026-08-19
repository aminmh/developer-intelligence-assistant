"""Domain frequency dictionary generation for SymSpell typo correction.

The dictionary is built from the domain jargon terms plus the indexed code
corpus (Qdrant ``chunk_code_*`` metadata). It is generated offline AND can be
regenerated after indexing so newly added class/method names become correctable.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Set

from qdrant_client import QdrantClient

DOMAIN_TERMS_PATH = Path(__file__).parent / "config" / "domain_terms.json"
DICTIONARY_PATH = Path(__file__).parent / "config" / "domain_dictionary.txt"

# Metadata fields on each code chunk that carry domain vocabulary.
META_FIELDS = ("class", "name", "class_fqn", "namespace")


def _camel_split(value: str) -> list[str]:
    # "planWeekly" -> "plan weekly" -> ["plan", "weekly"]
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    return [p for p in re.split(r"[^a-z0-9]+", spaced.lower()) if p]


def extract_tokens(value: str) -> Set[str]:
    if not value:
        return set()
    value = value.lower()
    tokens: Set[str] = set()

    # Whole token (no separators) — e.g. a full method name.
    if re.fullmatch(r"[a-z0-9]+", value):
        tokens.add(value)

    for segment in re.split(r"[\\/_]+", value):
        if not segment:
            continue
        if re.fullmatch(r"[a-z0-9]+", segment):
            tokens.add(segment)
        tokens.update(_camel_split(segment))

    return tokens


def load_jargon_terms() -> Counter:
    counter = Counter()
    try:
        data = json.loads(DOMAIN_TERMS_PATH.read_text(encoding="utf-8"))
        for key, value in data.items():
            if str(key).startswith("_"):
                continue
            counter.update(extract_tokens(str(key)))
            counter.update(extract_tokens(str(value)))
    except Exception:
        pass
    return counter


def collect_corpus_terms(client: QdrantClient) -> Counter:
    counter = Counter()
    collections = client.get_collections().collections

    for col in collections:
        if not col.name.startswith("chunk_code_"):
            continue
        offset = None
        while True:
            records, offset = client.scroll(
                collection_name=col.name,
                limit=1000,
                with_payload=True,
                with_vectors=False,
                offset=offset,
            )
            for point in records:
                payload = point.payload or {}
                for field in META_FIELDS:
                    value = payload.get(field)
                    if value:
                        counter.update(extract_tokens(str(value)))
            if offset is None:
                break
    return counter


def generate_domain_dictionary(qdrant_url: str) -> int:
    """Rebuild the domain dictionary from jargon terms + the indexed corpus.

    Returns the number of distinct terms written.
    """
    client = QdrantClient(url=qdrant_url)

    counter = load_jargon_terms()
    counter.update(collect_corpus_terms(client))

    DICTIONARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(DICTIONARY_PATH, "w", encoding="utf-8") as handle:
        for term, count in sorted(counter.items()):
            handle.write(f"{term}\t{count}\n")

    return len(counter)
