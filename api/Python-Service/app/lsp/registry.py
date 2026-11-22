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

    # ─── Index-time symbol driver ────────────────────────────────────────────

    def index_config(self, lang: str) -> Dict[str, Any]:
        cfg = self._lang_config(lang).get("index")
        return cfg if isinstance(cfg, dict) else {}

    def index_enabled(self, lang: str) -> bool:
        raw = resolve_env(self.index_config(lang).get("enabled", False))
        enabled = raw if isinstance(raw, bool) else str(raw).strip().lower() in ("1", "true", "yes", "on")
        return self.is_enabled(lang) and enabled

    def index_languages(self) -> List[str]:
        return [lang for lang in self.languages() if self.index_enabled(lang)]

    def server_argv(self, lang: str) -> List[str]:
        """Full server command: interpreter (if any), binary, args."""
        lsp = self._lang_config(lang).get("lsp") or {}
        binary = resolve_env(lsp.get("binary") or "")
        if not binary:
            return []
        args = [resolve_env(a) for a in lsp.get("args") or []]
        interpreter = resolve_env(lsp.get("interpreter")) if lsp.get("interpreter") else None
        return [interpreter, binary, *args] if interpreter else [binary, *args]

    def server_installed(self, lang: str) -> bool:
        """True when the configured server binary (and interpreter, if any) can be found."""
        import shutil

        lsp = self._lang_config(lang).get("lsp") or {}
        binary = resolve_env(lsp.get("binary") or "")
        interpreter = resolve_env(lsp.get("interpreter")) if lsp.get("interpreter") else None
        found = bool(binary) and (os.path.isfile(binary) or shutil.which(binary) is not None)
        return found and (interpreter is None or shutil.which(interpreter) is not None)

    def index_workspace(self, lang: str, root: str) -> Dict[str, Any]:
        """Index one extracted revision for one language (origin `lsp`). Never raises.

        Each run starts its own server process with isolated HOME/XDG directories;
        the long-lived validation client is not reused.
        """
        from lsp.indexer import IndexLimits, index_workspace

        unavailable = {"name": "lsp", "origin": "lsp", "language": lang, "status": "unavailable",
                       "definitions": [], "references": [], "truncated": False, "stats": {},
                       "limits_reached": []}
        if not self.index_enabled(lang):
            return {**unavailable, "reason": f"LSP indexing is not enabled for {lang}."}
        argv = self.server_argv(lang)
        if not argv:
            return {**unavailable, "reason": f"No language server is configured for {lang}."}
        try:
            adapter = self.adapter(lang)
        except Exception as exc:  # noqa: BLE001 - adapter import failures become coverage
            return {**unavailable, "reason": f"Adapter for {lang} could not load: {exc}"[:300]}
        lsp = self._lang_config(lang).get("lsp") or {}
        return index_workspace(
            root, adapter, argv, resolve_env(lsp.get("language_id")) or lang,
            IndexLimits.from_config({k: resolve_env(v) for k, v in self.index_config(lang).items()}),
        )

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
