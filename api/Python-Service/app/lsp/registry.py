"""LspRegistry — loads `config/lsps.yml` and dispatches validation.

For each enabled language it lazily builds a language adapter (module
`lsp.adapters.<adapter-key>`) and a stdio LSP client from the config's `lsp`
block. `${VAR:-default}` in config strings is resolved from the environment, so
server binaries can be overridden without editing the YAML.
"""

from __future__ import annotations

import importlib
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from infra.exception_logger import get_exception_logger
from lsp.base import build_validation_note, flatten_summary, validate_and_repair_blocks
from lsp.client import LspClient
from infra.settings import settings

_WARMUP_SNIPPETS: Dict[str, str] = {
    "php": "<?php\nclass __Warmup {}\n",
    "python": "x = 1\n",
    "typescript": "const x = 1;\n",
    "javascript": "const x = 1;\n",
    "go": "package main\n",
    "rust": "fn main() {}\n",
}


def resolve_env(value: Any) -> Any:
    """Resolve `${VAR}` and `${VAR:-default}` in config strings from the env."""
    if not isinstance(value, str):
        return value
    if value.startswith("${") and value.endswith("}"):
        inner = value[2:-1]
        var, _, default = inner.partition(":-")
        return os.environ.get(var, default)
    return value


class LspRegistry:
    def __init__(self, config_path: str) -> None:
        self._config_path = Path(config_path)
        self._config = self._load()
        self._adapters: Dict[str, Any] = {}
        self._clients: Dict[str, Optional[LspClient]] = {}
        self._lock = threading.RLock()

    def _load(self) -> Dict[str, Any]:
        if not self._config_path.exists():
            return {}
        try:
            with open(self._config_path, "r", encoding="utf-8") as handle:
                data = yaml.safe_load(handle) or {}
        except Exception as exc:
            get_exception_logger().error("Failed to load lsps.yml: %s", exc)
            return {}
        return data if isinstance(data, dict) else {}

    def _lang_config(self, lang: str) -> Dict[str, Any]:
        cfg = (self._config or {}).get(lang)
        return cfg if isinstance(cfg, dict) else {}

    def languages(self) -> List[str]:
        return [
            k
            for k, v in (self._config or {}).items()
            if isinstance(v, dict) and v.get("enabled")
        ]

    def is_enabled(self, lang: str) -> bool:
        return bool(self._lang_config(lang).get("enabled"))

    def adapter(self, lang: str) -> Any:
        """Return (and lazily build) the language adapter instance."""
        with self._lock:
            if lang not in self._adapters:
                adapter_key = self._lang_config(lang).get("adapter") or lang
                module = importlib.import_module(f"lsp.adapters.{adapter_key}")
                self._adapters[lang] = module.Adapter()
            return self._adapters[lang]

    def client(self, lang: str) -> Optional[LspClient]:
        """Return (and lazily build) the language's LSP client, or None when no
        binary is configured (validation then uses the adapter's fallback)."""
        with self._lock:
            if lang not in self._clients:
                lsp = self._lang_config(lang).get("lsp") or {}
                binary = resolve_env(lsp.get("binary") or "")
                if not binary:
                    self._clients[lang] = None
                    return None
                args = lsp.get("args")
                if isinstance(args, list):
                    args = [resolve_env(a) for a in args]
                self._clients[lang] = LspClient(
                    binary=binary,
                    args=args,
                    language_id=resolve_env(lsp.get("language_id")) or lang,
                    interpreter=resolve_env(lsp.get("interpreter")) if lsp.get("interpreter") else None,
                    validate_dir=resolve_env(lsp.get("validate_dir"))
                    or f"/tmp/lsp-validate/{lang}",
                    timeout=float(resolve_env(lsp.get("timeout")) or settings.lsp_timeout),
                )
            return self._clients[lang]

    def validate(self, lang: str, code: str, filename: Optional[str] = None) -> Dict[str, Any]:
        """Validate a snippet for one language. Returns
        {valid, errors, warnings, source}."""
        if not self.is_enabled(lang):
            return {"valid": True, "errors": [], "warnings": [], "source": "disabled"}
        return self.adapter(lang).validate(code, filename, self.client(lang))

    def validate_markdown(
        self,
        text: str,
        question: str = "",
        max_retries: Optional[int] = None,
    ) -> tuple[str, List[Dict[str, Any]]]:
        """Validate + repair code blocks of every enabled language in `text`.

        Returns (new_text, summary) where summary entries are
        {language, index, status, errors, warnings}.
        """
        retries = int(max_retries if max_retries is not None else settings.lsp_max_retries)
        summary: List[Dict[str, Any]] = []
        new_text = text
        for lang in self.languages():
            adapter = self.adapter(lang)
            new_text, lang_summary = validate_and_repair_blocks(
                adapter, new_text, question, self.client(lang), max_retries=retries
            )
            summary.extend(lang_summary)
        return new_text, summary

    def warmup(self) -> None:
        """Spawn each enabled language server with a trivial snippet so caches
        are warm before the first real answer. Non-blocking (daemon thread)."""

        def _run() -> None:
            for lang in self.languages():
                try:
                    snippet = _WARMUP_SNIPPETS.get(lang, "x = 1\n")
                    self.validate(lang, snippet, "warmup")
                except Exception as exc:
                    get_exception_logger().warning("LSP warmup failed for %s: %s", lang, exc)

        threading.Thread(target=_run, daemon=True).start()

    def shutdown(self) -> None:
        with self._lock:
            for lang, client in self._clients.items():
                if client is not None:
                    try:
                        client.shutdown()
                    except Exception:
                        pass
            self._clients.clear()
