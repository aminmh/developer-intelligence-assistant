"""Outbound HTTPS for the knowledge layer: allowlisted hosts only, public addresses only, bounded.

Spec clause 10 and failure clauses 1 and 3; decision
@.archcore/agent-tools/mcp-servers-reach-only-allowlisted-hosts-through-egress-proxy.adr.md.
Requests carry only what a URL built from repository names, refs and file paths contains; the
caller ranks by topic locally, so topic words never go to GitHub. Redirects are followed only to
allowlisted hosts. After a rate limit, the same fetcher refuses further calls for the request.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request
from typing import Callable
from urllib.parse import urlsplit

DEFAULT_HOSTS = frozenset({"raw.githubusercontent.com", "api.github.com", "gitmcp.io"})
TIMEOUT_S = 10
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
USER_AGENT = "DevAssistant-Knowledge/1"
_TOKEN_HOSTS = frozenset({"raw.githubusercontent.com", "api.github.com"})


class RemoteUnavailable(RuntimeError):
    """The host could not be reached or refused the request (failure clause 1)."""


class RateLimited(RemoteUnavailable):
    """The host rate-limited the request (failure clause 3)."""


def allowed_hosts() -> frozenset[str]:
    """Default hosts plus ``KNOWLEDGE_EXTRA_HOSTS`` (a self-hosted GitMCP, comma-separated)."""
    extra = {h.strip().lower() for h in os.environ.get("KNOWLEDGE_EXTRA_HOSTS", "").split(",") if h.strip()}
    return DEFAULT_HOSTS | extra


def _public(host: str) -> bool:
    from infra.tool_executor import _blocked_host

    return not _blocked_host(host)


class _NoForeignRedirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, hosts: frozenset[str]):
        self.hosts = hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102 — urllib hook
        if (urlsplit(newurl).hostname or "").lower() not in self.hosts or urlsplit(newurl).scheme != "https":
            raise RemoteUnavailable(f"Refused a redirect to {urlsplit(newurl).hostname} outside the allowlist.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _urlopen(request: urllib.request.Request, hosts: frozenset[str]):
    return urllib.request.build_opener(_NoForeignRedirects(hosts)).open(request, timeout=TIMEOUT_S)


class Fetcher:
    """One request's outbound calls; ``sent`` records every URL for audit and tests."""

    def __init__(self, *, hosts: frozenset[str] | None = None, opener: Callable | None = None,
                 public: Callable[[str], bool] = _public, token: str | None = None):
        self.hosts = hosts or allowed_hosts()
        self.opener = opener or _urlopen
        self.public = public
        self.token = token if token is not None else os.environ.get("KNOWLEDGE_GITHUB_TOKEN") or None
        self.sent: list[str] = []
        self.limited: str | None = None

    def get(self, url: str, *, accept: str = "application/vnd.github+json") -> bytes | None:
        """Body of a 200 response, None for 404; raises RemoteUnavailable or RateLimited otherwise."""
        if self.limited:
            raise RateLimited(self.limited)
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" or host not in self.hosts:
            raise RemoteUnavailable(f"{host or url} is not an allowed documentation host.")
        if not self.public(host):
            raise RemoteUnavailable(f"{host} does not resolve to a public address.")
        headers = {"User-Agent": USER_AGENT, "Accept": accept}
        if self.token and host in _TOKEN_HOSTS:
            headers["Authorization"] = f"Bearer {self.token}"
        self.sent.append(url)
        try:
            with self.opener(urllib.request.Request(url, headers=headers), self.hosts) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            if exc.code == 429 or (exc.code == 403 and exc.headers.get("X-RateLimit-Remaining") == "0"):
                self.limited = f"{host} rate-limited the request; set KNOWLEDGE_GITHUB_TOKEN or retry later."
                raise RateLimited(self.limited) from exc
            raise RemoteUnavailable(f"{host} answered HTTP {exc.code}.") from exc
        except RemoteUnavailable:
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RemoteUnavailable(f"{host} could not be reached: {type(exc).__name__}.") from exc
        if len(body) > MAX_RESPONSE_BYTES:
            raise RemoteUnavailable(f"{host} returned more than {MAX_RESPONSE_BYTES // 1024} KB for one file.")
        return body
