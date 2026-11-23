"""Download a contract document from a public HTTPS URL.

Spec: @.archcore/frontend-assist/api-contract-sources-and-versions.spec.md (behavior 12,
the URL constraints, failure rules 2-3).

The server fetches an address a user typed, so every hop is checked before any
byte is sent: HTTPS only, no credentials, and the host must resolve only to
public addresses. The connection is then made to the address that was checked,
so a second DNS answer cannot redirect it to an internal host. Proxy settings
from the environment are ignored on purpose.

The MCP egress proxy (@.archcore/agent-tools/mcp-servers-reach-only-allowlisted-hosts-through-egress-proxy.adr.md)
is not built yet; these checks stand in for it.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import time
from typing import Callable
from urllib.parse import urljoin, urlsplit

from api_contracts.model import MAX_DOCUMENT_BYTES

TIMEOUT_SECONDS = 15.0
MAX_REDIRECTS = 3
MAX_URL_LENGTH = 2000
_REDIRECTS = {301, 302, 303, 307, 308}

Resolver = Callable[[str, int], list[str]]


class FetchRefused(ValueError):
    """The URL is not allowed; retrying the same URL cannot succeed."""


class FetchFailed(RuntimeError):
    """The download did not complete; a later retry may succeed."""


def validate_url(url: object) -> tuple[str, str, int]:
    """Return ``(normalised url, host, port)`` or raise ``FetchRefused``."""
    if not isinstance(url, str) or not url.strip() or len(url) > MAX_URL_LENGTH:
        raise FetchRefused("A contract URL is required (at most 2000 characters).")
    url = url.strip()
    try:
        parts = urlsplit(url)
        port = parts.port or 443
    except ValueError as exc:
        raise FetchRefused("The contract URL is not valid.") from exc
    if parts.scheme.lower() != "https":
        raise FetchRefused("The contract URL must use HTTPS.")
    if parts.username is not None or parts.password is not None:
        raise FetchRefused("The contract URL must not contain credentials.")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise FetchRefused("The contract URL has no host.")
    return url, host, port


def _is_public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        mapped = ip.ipv4_mapped or ip.sixtofour or (ip.teredo[1] if ip.teredo else None)
        if mapped is not None:
            ip = mapped
    # is_global is False for loopback, private, link-local (cloud metadata), shared, and reserved ranges.
    return ip.is_global and not ip.is_multicast


def _system_resolver(host: str, port: int) -> list[str]:
    try:
        results = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as exc:
        raise FetchFailed("The contract URL's host could not be resolved.") from exc
    return list(dict.fromkeys(result[4][0] for result in results))


def public_addresses(host: str, port: int, resolver: Resolver | None = None) -> list[str]:
    """Addresses for ``host``, refused unless every one of them is public."""
    addresses = (resolver or _system_resolver)(host, port)
    if not addresses:
        raise FetchFailed("The contract URL's host could not be resolved.")
    if not all(_is_public(address) for address in addresses):
        raise FetchRefused("The contract URL points to a loopback, private, or link-local address, which is not allowed.")
    return addresses


class _PinnedConnection(http.client.HTTPSConnection):
    """HTTPS to one already-checked address, verifying the certificate for the original host."""

    def __init__(self, host: str, port: int, address: str, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._address = address

    def connect(self) -> None:
        raw = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


def _request(url: str, host: str, port: int, address: str, deadline: float) -> tuple[int, str | None, bytes]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise FetchFailed("The contract download timed out.")
    parts = urlsplit(url)
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    connection = _PinnedConnection(host, port, address, remaining)
    try:
        connection.request("GET", target, headers={
            "Host": host if port == 443 else f"{host}:{port}",
            "Accept": "application/json, application/yaml, text/yaml, text/plain, */*",
            "Accept-Encoding": "identity", "User-Agent": "contract-fetcher", "Connection": "close"})
        response = connection.getresponse()
        if response.status in _REDIRECTS:
            return response.status, response.getheader("Location"), b""
        if response.status != 200:
            raise FetchFailed(f"The contract URL answered with HTTP {response.status}.")
        declared = response.getheader("Content-Length")
        if declared and declared.isdigit() and int(declared) > MAX_DOCUMENT_BYTES:
            raise FetchFailed("The document at the contract URL exceeds the 10 MB limit.")
        chunks, size = [], 0
        while True:
            if time.monotonic() > deadline:
                raise FetchFailed("The contract download timed out.")
            chunk = response.read(64 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_DOCUMENT_BYTES:
                raise FetchFailed("The document at the contract URL exceeds the 10 MB limit.")
            chunks.append(chunk)
        return 200, None, b"".join(chunks)
    except (FetchFailed, FetchRefused):
        raise
    except (OSError, http.client.HTTPException) as exc:  # includes ssl.SSLError and socket.timeout
        reason = "timed out" if isinstance(exc, (socket.timeout, TimeoutError)) else "failed"
        raise FetchFailed(f"The contract download {reason} ({type(exc).__name__}).") from exc
    finally:
        connection.close()


def fetch_contract(url: str, *, resolver: Resolver | None = None,
                   request: Callable[[str, str, int, str, float], tuple[int, str | None, bytes]] | None = None) -> bytes:
    """Download the document, re-checking the address after every redirect."""
    deadline = time.monotonic() + TIMEOUT_SECONDS
    send = request or _request
    for _hop in range(MAX_REDIRECTS + 1):
        url, host, port = validate_url(url)
        address = public_addresses(host, port, resolver)[0]
        status, location, body = send(url, host, port, address, deadline)
        if status == 200:
            return body
        if not location:
            raise FetchFailed("The contract URL redirected without a destination.")
        url = urljoin(url, location)
    raise FetchFailed("The contract URL redirected more than 3 times.")
