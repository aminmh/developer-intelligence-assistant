"""A minimal MCP client for one remote documentation server over streamable HTTP.

Spec clause 10. Only what the knowledge layer needs: ``initialize``, ``notifications/initialized``,
``tools/list`` and ``tools/call``, each a JSON-RPC POST through the knowledge fetcher, so the host
allowlist, public-address check, size cap and rate-limit rule apply. Replies may be JSON or
server-sent events; the session id from ``initialize`` is sent back on later calls.

Only tools in ``ALLOWED_TOOLS`` can be called. GitMCP also offers a generic URL fetcher
(``fetch_generic_url_content``), and its search results tell the client to use it; calling it would
let the server fetch arbitrary hosts for us, outside the allowlist (found live, 2026-11-23).
"""

from __future__ import annotations

import json
from itertools import count
from typing import Any

from knowledge.http import RemoteUnavailable

PROTOCOL_VERSION = "2025-03-26"
ALLOWED_TOOLS = frozenset({"search_generic_documentation", "fetch_generic_documentation"})


def _decode(body: bytes, content_type: str) -> dict[str, Any] | None:
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    if "text/event-stream" in content_type or text.startswith(("event:", "data:")):
        events = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
        text = events[-1] if events else ""
    try:
        message = json.loads(text)
    except ValueError as exc:
        raise RemoteUnavailable("The documentation server sent an unreadable reply.") from exc
    return message if isinstance(message, dict) else None


class McpClient:
    def __init__(self, fetcher: Any, url: str, *, client_name: str = "devassistant-knowledge"):
        self.fetcher = fetcher
        self.url = url
        self.client_name = client_name
        self.session: str | None = None
        self._ids = count(1)
        self._tools: dict[str, dict[str, Any]] | None = None

    def _post(self, message: dict[str, Any]) -> dict[str, Any] | None:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        response = self.fetcher.send("POST", self.url, data=json.dumps(message).encode("utf-8"), headers=headers)
        if response is None:
            raise RemoteUnavailable("The documentation server endpoint was not found.")
        status, received, body = response
        if received.get("mcp-session-id"):
            self.session = received["mcp-session-id"]
        if "id" not in message:  # a notification: 202 Accepted and no body
            return None
        reply = _decode(body, received.get("content-type", ""))
        if reply is None:
            raise RemoteUnavailable(f"The documentation server sent no reply (HTTP {status}).")
        if reply.get("error"):
            raise RemoteUnavailable(f"The documentation server refused the call: {str(reply['error'].get('message'))[:200]}")
        return reply.get("result") or {}

    def _request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._post({"jsonrpc": "2.0", "id": next(self._ids), "method": method, **({"params": params} if params else {})}) or {}

    def initialize(self) -> dict[str, Any]:
        result = self._request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                              "clientInfo": {"name": self.client_name, "version": "1"}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return result

    def tools(self) -> dict[str, dict[str, Any]]:
        if self._tools is None:
            if self.session is None:
                self.initialize()
            listed = self._request("tools/list").get("tools") or []
            self._tools = {tool["name"]: tool for tool in listed if isinstance(tool, dict) and isinstance(tool.get("name"), str)}
        return self._tools

    def call(self, name: str, arguments: dict[str, Any]) -> str:
        """The text content of one allowed tool call; tools outside the allowlist are never called."""
        if name not in ALLOWED_TOOLS:
            raise RemoteUnavailable(f"The documentation tool {name} is not allowed.")
        if name not in self.tools():
            raise RemoteUnavailable(f"The documentation server does not offer {name}.")
        result = self._request("tools/call", {"name": name, "arguments": arguments})
        text = "".join(part.get("text", "") for part in result.get("content") or [] if isinstance(part, dict))
        if result.get("isError"):
            raise RemoteUnavailable(f"The documentation tool {name} failed: {text[:200]}")
        return text
