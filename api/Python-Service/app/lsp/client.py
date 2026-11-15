"""Minimal LSP (Language Server Protocol) client over stdio.

Speaks JSON-RPC to a language-server binary over stdin/stdout. The server
pushes `textDocument/publishDiagnostics` for opened documents. Language servers
typically publish multiple times per open (fast lint first, then richer
analysis), so `validate_many` waits until diagnostics settle before returning.

This client is language-agnostic: binary, arguments, and language id come from
the per-language config (`config/lsps.yml`).
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from infra.exception_logger import get_exception_logger


class LspConnectionError(Exception):
    """Raised when the language server cannot be reached or misbehaves."""


def encode_message(payload: Dict[str, Any]) -> bytes:
    body = json.dumps(payload).encode("utf-8")
    return b"Content-Length: %d\r\n\r\n" % len(body) + body


class LspClient:
    """Persistent, thread-safe stdio client for one language server."""

    def __init__(
        self,
        binary: str,
        args: Optional[List[str]] = None,
        language_id: str = "php",
        interpreter: Optional[str] = None,
        validate_dir: str = "/tmp/lsp-validate",
        timeout: float = 10.0,
        settle: float = 2.0,
    ) -> None:
        self._binary = binary
        self._args = list(args or [])
        self._interpreter = interpreter
        self._language_id = language_id
        self._validate_dir = validate_dir
        self._timeout = timeout
        self._settle = settle
        self._root_uri = "file://" + validate_dir

        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._request_id = 0
        self._pending: Dict[int, List[Dict[str, Any]]] = {}
        self._diagnostics: Dict[str, List[Dict[str, Any]]] = {}
        self._reader_stop = threading.Event()
        self._reader_thread: Optional[threading.Thread] = None
        self._consecutive_failures = 0

    # ─── Lifecycle ───────────────────────────────────────────────────────────

    def _spawn_command(self) -> List[str]:
        cmd = [self._binary] + self._args
        if self._interpreter:
            cmd = [self._interpreter] + cmd
        return cmd

    def _spawn(self) -> None:
        Path(self._validate_dir).mkdir(parents=True, exist_ok=True)
        self._proc = subprocess.Popen(
            self._spawn_command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._reader_stop.clear()
        self._diagnostics.clear()
        self._pending.clear()
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()

    def _kill(self) -> None:
        self._reader_stop.set()
        proc = self._proc
        self._proc = None
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=3)
            except Exception:
                pass

    def _read_loop(self) -> None:
        if self._proc is None or self._proc.stdout is None:
            return
        stream = self._proc.stdout
        buf = b""
        while not self._reader_stop.is_set():
            ready, _, _ = select.select([stream], [], [], 0.5)
            if not ready:
                if self._proc is not None and self._proc.poll() is not None:
                    return
                continue
            try:
                chunk = os.read(stream.fileno(), 65536)
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            while True:
                idx = buf.find(b"\r\n\r\n")
                if idx < 0:
                    break
                header = buf[:idx].decode()
                length = 0
                for line in header.split("\r\n"):
                    if line.lower().startswith("content-length:"):
                        length = int(line.split(":", 1)[1].strip())
                body = buf[idx + 4 : idx + 4 + length]
                if len(body) < length:
                    break
                try:
                    self._dispatch(json.loads(body))
                except Exception as exc:  # pragma: no cover
                    get_exception_logger().warning("LSP message parse failed: %s", exc)
                buf = buf[idx + 4 + length :]

    def _dispatch(self, msg: Dict[str, Any]) -> None:
        if "id" in msg and "method" not in msg:
            queue = self._pending.pop(msg.get("id"), None)
            if queue is not None:
                queue.append(msg)
        elif msg.get("method") == "textDocument/publishDiagnostics":
            params = msg.get("params") or {}
            uri = params.get("uri")
            if uri:
                params = dict(params)
                params["_t"] = time.time()
                self._diagnostics.setdefault(uri, []).append(params)
        elif msg.get("method") and "id" in msg:
            # Server -> client request (e.g. workspace/configuration): acknowledge.
            self._write({"jsonrpc": "2.0", "id": msg["id"], "result": None})

    def _write(self, msg: Dict[str, Any]) -> None:
        with self._write_lock:
            if self._proc is None or self._proc.stdin is None or self._proc.poll() is not None:
                raise LspConnectionError("LSP process is not running")
            try:
                self._proc.stdin.write(encode_message(msg))
                self._proc.stdin.flush()
            except Exception as exc:
                raise LspConnectionError(f"LSP write failed: {exc}")

    def _request(self, method: str, params: Any, timeout: Optional[float] = None) -> Dict[str, Any]:
        with self._lock:
            self._request_id += 1
            rid = self._request_id
            queue: List[Dict[str, Any]] = []
            self._pending[rid] = queue
            self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            deadline = time.time() + (timeout or self._timeout)
            while time.time() < deadline:
                if queue:
                    return queue[0]
                if self._proc is not None and self._proc.poll() is not None:
                    raise LspConnectionError("LSP process exited")
                time.sleep(0.05)
            raise LspConnectionError(f"LSP request {method} timed out")

    def _ensure_started(self) -> None:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return
            self._spawn()
            init = self._request(
                "initialize",
                {
                    "processId": os.getpid(),
                    "rootUri": self._root_uri,
                    "capabilities": {
                        "textDocument": {
                            "synchronization": {"didOpen": True},
                            "publishDiagnostics": {"relatedInformation": True},
                        }
                    },
                },
                timeout=min(30.0, self._timeout + 15.0),
            )
            if not init or "result" not in init:
                raise LspConnectionError("LSP initialize failed")
            self._write({"jsonrpc": "2.0", "method": "initialized", "params": {}})

    # ─── Validation ──────────────────────────────────────────────────────────

    def validate(self, code: str, filename: Optional[str] = None) -> Dict[str, Any]:
        """Validate a snippet. Returns {uri: {"diagnostics": [...]}} keyed by URI."""
        filename = filename or f"Block_{uuid.uuid4().hex[:12]}.{_ext_for(self._language_id)}"
        uri = f"{self._root_uri}/{Path(filename).name}"
        results = self.validate_many([{"uri": uri, "text": code}])
        return results.get(uri, {"diagnostics": []})

    def validate_many(self, specs: List[Dict[str, str]]) -> Dict[str, Dict[str, Any]]:
        """Open all documents, wait for diagnostics to settle, return per-uri results.

        specs: [{"uri": str, "text": str}]
        Returns: {uri: {"diagnostics": [...LSP diagnostic dicts...]}}
        """
        try:
            return self._validate_many_inner(specs)
        except LspConnectionError:
            self._consecutive_failures += 1
            if self._consecutive_failures > 1:
                raise
            get_exception_logger().warning("LSP server failed; restarting once")
            self._kill()
            return self._validate_many_inner(specs)

    def _validate_many_inner(self, specs: List[Dict[str, str]]) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            self._ensure_started()
            uri_list = [s["uri"] for s in specs]
            for s in specs:
                self._diagnostics[s["uri"]] = []
                self._write(
                    {
                        "jsonrpc": "2.0",
                        "method": "textDocument/didOpen",
                        "params": {
                            "textDocument": {
                                "uri": s["uri"],
                                "languageId": self._language_id,
                                "version": 1,
                                "text": s["text"],
                            }
                        },
                    }
                )

            start = time.time()
            last_updated = {u: time.time() for u in uri_list}
            min_wait = min(1.0, self._settle)

            while True:
                elapsed = time.time() - start
                if elapsed > self._timeout:
                    break
                if elapsed >= min_wait and all(
                    time.time() - last_updated[u] > self._settle for u in uri_list
                ):
                    break
                for u in uri_list:
                    pubs = self._diagnostics.get(u) or []
                    if pubs:
                        last_updated[u] = max(last_updated[u], pubs[-1].get("_t", 0))
                time.sleep(0.1)

            results: Dict[str, Dict[str, Any]] = {}
            for u in uri_list:
                pubs = self._diagnostics.get(u) or []
                diagnostics = pubs[-1].get("diagnostics", []) if pubs else []
                results[u] = {"diagnostics": diagnostics}
                self._write(
                    {
                        "jsonrpc": "2.0",
                        "method": "textDocument/didClose",
                        "params": {"textDocument": {"uri": u}},
                    }
                )
            self._consecutive_failures = 0
            return results

    def shutdown(self) -> None:
        try:
            self._write({"jsonrpc": "2.0", "id": "shutdown", "method": "shutdown", "params": None})
            self._write({"jsonrpc": "2.0", "method": "exit", "params": None})
        except Exception:
            pass
        self._kill()


_DEFAULT_EXTS = {
    "php": "php",
    "python": "py",
    "typescript": "ts",
    "javascript": "js",
    "go": "go",
    "rust": "rs",
}


def _ext_for(language_id: str) -> str:
    return _DEFAULT_EXTS.get(language_id, language_id.replace("/", "_") or "txt")
