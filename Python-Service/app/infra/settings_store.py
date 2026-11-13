"""SQLite-backed store for UI-configurable settings.

The Python service is the single source of truth for the RAG configuration that
the web UI edits. Values persist in a local SQLite file and are read at action
time, so a change takes effect on the next request without a restart.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, Optional

# Canonical UI-editable settings and their defaults. Mirrors web/src/types.ts
# (RAGConfig). The Python service is the single source of truth; the web server
# reads these and proxies edits back here.
RAG_CONFIG_DEFAULTS: Dict[str, Any] = {
    "embeddingProvider": "ollama",
    "cohereApiKey": "",
    "cloudflareAccountId": "",
    "cloudflareApiToken": "",
    "cloudflareEmbeddingModel": "@cf/baai/bge-base-en-v1.5",
    "ollamaBaseUrl": "http://host.docker.internal:11434",
    "ollamaEmbedModel": "BGE-M3",
    "embeddingBaseUrl": "",
    "ollamaLlmModel": "gemma3-4b",
    "ollamaEvalModel": "gemma3-4b",
    "llmRemoteBaseUrl": "",
    "llmRemoteModel": "",
    "llmRemoteApiKey": "",
    "docRetrievalEnabled": True,
    "docContext7Enabled": True,
    "docGitmcpEnabled": True,
    "docStrictMode": False,
    "domainConceptsEnabled": True,
    "docContext7TimeoutS": 10,
    "docGitmcpTimeoutS": 15,
    "contextPreviewMode": "off",
    "rerankerEnabled": False,
    "rerankerModel": "BAAI/bge-reranker-v2-m3",
    "rerankerMaxCandidates": 40,
    "rerankerTimeoutS": 10,
    "logDeletionMode": "soft",
    "qdrantUrl": "",
    "qdrantApiKey": "",
    "temperature": 0.7,
    "topP": 0.9,
}

# Fields returned masked by GET /settings. They are write-only: a PUT only
# overwrites them when a non-empty value is supplied.
SECRET_KEYS = {"qdrantApiKey", "llmRemoteApiKey", "cohereApiKey", "cloudflareApiToken"}

MASKED_VALUE = ""


class SettingsStore:
    """Thread-safe key/value store persisted to SQLite."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = db_path or os.getenv(
            "SETTINGS_DB_PATH", "/app/data/settings.db"
        )
        self._lock = threading.RLock()
        self._cache: Optional[Dict[str, Any]] = None
        self._revision = 0
        self._init_db()

    def _init_db(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            conn = self._connect()
            conn.execute(
                "CREATE TABLE IF NOT EXISTS settings ("
                "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            conn.commit()
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _load_cache(self) -> None:
        if self._cache is not None:
            return
        cache: Dict[str, Any] = {}
        conn = self._connect()
        try:
            for row in conn.execute("SELECT key, value FROM settings"):
                try:
                    cache[row["key"]] = json.loads(row["value"])
                except (json.JSONDecodeError, TypeError):
                    cache[row["key"]] = row["value"]
        finally:
            conn.close()
        self._cache = cache

    @property
    def revision(self) -> int:
        return self._revision

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            self._load_cache()
            return self._cache.get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._load_cache()
            self._cache[key] = value
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, json.dumps(value)),
                )
                conn.commit()
            finally:
                conn.close()
            self._revision += 1

    def set_many(self, values: Dict[str, Any]) -> None:
        with self._lock:
            self._load_cache()
            for key, value in values.items():
                self._cache[key] = value
            conn = self._connect()
            try:
                conn.executemany(
                    "INSERT INTO settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    [(k, json.dumps(v)) for k, v in values.items()],
                )
                conn.commit()
            finally:
                conn.close()
            if values:
                self._revision += 1

    def delete(self, key: str) -> None:
        with self._lock:
            self._load_cache()
            self._cache.pop(key, None)
            conn = self._connect()
            try:
                conn.execute("DELETE FROM settings WHERE key = ?", (key,))
                conn.commit()
            finally:
                conn.close()
            self._revision += 1


def build_config(store: SettingsStore) -> Dict[str, Any]:
    """Merge stored values over defaults to produce the full RAG config."""
    config = dict(RAG_CONFIG_DEFAULTS)
    for key in RAG_CONFIG_DEFAULTS:
        stored = store.get(key)
        if stored is not None:
            config[key] = stored
    if store.get("embeddingProvider") is None and config.get("embeddingBaseUrl"):
        config["embeddingProvider"] = "openAICompatible"
    if store.get("ollamaEvalModel") is None:
        config["ollamaEvalModel"] = config["ollamaLlmModel"]
    return config


def mask_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy with secret fields masked."""
    masked = dict(config)
    for key in SECRET_KEYS:
        if masked.get(key):
            masked[key] = MASKED_VALUE
    return masked


settings_store = SettingsStore()
