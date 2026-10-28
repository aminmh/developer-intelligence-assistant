"""Project-scoped, read-only access to Outline business-rule collections."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import ipaddress
import json
import socket
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

MAX_DOCUMENTS = 50
MAX_RULES_PER_ASSESSMENT = 20
MAX_DOCUMENT_CHARS = 12_000
MAX_INSTANCE_URL_CHARS = 2_048
_LOCAL_OUTLINE_HOST = "outline"


class OutlineError(RuntimeError):
    """A safe-to-display Outline connection or document error."""


def normalize_instance_url(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > MAX_INSTANCE_URL_CHARS:
        raise OutlineError("Enter a valid Outline instance URL.")
    parsed = urlsplit(value.strip().rstrip("/"))
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise OutlineError("Use an Outline URL without credentials, query parameters, or fragments.")
    hostname = parsed.hostname.lower().rstrip(".")
    if hostname == _LOCAL_OUTLINE_HOST:
        if parsed.scheme != "http" or parsed.port not in {None, 3000}:
            raise OutlineError("The local Outline service must use http://outline:3000.")
    else:
        if parsed.scheme != "https":
            raise OutlineError("Remote Outline instances must use HTTPS.")
        try:
            addresses = {item[4][0] for item in socket.getaddrinfo(hostname, parsed.port or 443, type=socket.SOCK_STREAM)}
        except OSError as exc:
            raise OutlineError("The Outline host could not be resolved.") from exc
        if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise OutlineError("The Outline host must resolve to public addresses.")
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _store_path():
    from code_quality.service import _store_path as assessment_store_path
    return assessment_store_path()


class OutlineConnectionStore:
    def __init__(self, path=None):
        self.path = path or _store_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS code_quality_outline_connections (
                project_id TEXT PRIMARY KEY, instance_url TEXT NOT NULL, collection_id TEXT NOT NULL,
                credential_id TEXT NOT NULL, updated_at TEXT NOT NULL)""")

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _serialize(row):
        return {"instance_url": row["instance_url"], "collection_id": row["collection_id"],
                "credential_id": row["credential_id"], "updated_at": row["updated_at"]}

    def get(self, project_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM code_quality_outline_connections WHERE project_id=?", (project_id,)).fetchone()
        return self._serialize(row) if row else None

    def save(self, project_id: str, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise OutlineError("Outline connection settings are invalid.")
        instance_url = normalize_instance_url(value.get("instance_url"))
        collection_id = value.get("collection_id")
        credential_id = value.get("credential_id")
        if not isinstance(collection_id, str) or not collection_id.strip() or len(collection_id) > 128:
            raise OutlineError("Select an Outline collection.")
        try:
            collection_id = str(UUID(collection_id.strip()))
        except (ValueError, AttributeError) as exc:
            raise OutlineError("The selected Outline collection identity is invalid.") from exc
        try:
            credential_id = str(UUID(credential_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise OutlineError("Outline credential identity is invalid.") from exc
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute("""INSERT INTO code_quality_outline_connections
                (project_id, instance_url, collection_id, credential_id, updated_at) VALUES(?,?,?,?,?)
                ON CONFLICT(project_id) DO UPDATE SET instance_url=excluded.instance_url,
                collection_id=excluded.collection_id, credential_id=excluded.credential_id,
                updated_at=excluded.updated_at""",
                       (project_id, instance_url, collection_id.strip(), credential_id, now))
        return self.get(project_id)

    def delete(self, project_id: str) -> dict[str, Any] | None:
        current = self.get(project_id)
        with self._connect() as db:
            db.execute("DELETE FROM code_quality_outline_connections WHERE project_id=?", (project_id,))
        return current


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _outline_post(instance_url: str, token: str, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(token, str) or not token.strip() or len(token) > 8192:
        raise OutlineError("Save an Outline API token in the OS credential store and retry.")
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = Request(f"{instance_url}/api/{operation}", data=body, method="POST", headers={
        "Authorization": f"Bearer {token.strip()}", "Content-Type": "application/json", "Accept": "application/json",
    })
    try:
        with build_opener(_NoRedirect).open(request, timeout=8) as response:
            if response.status != 200:
                raise OutlineError("Outline rejected the request. Check the token and collection access.")
            raw = response.read(2_000_001)
    except HTTPError as exc:
        if exc.code in {301, 302, 303, 307, 308}:
            raise OutlineError("Outline redirected the API request; verify the configured instance URL.") from exc
        raise OutlineError("Outline rejected the request. Check the token and collection access.") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise OutlineError("The Outline instance could not be reached. Check its URL and retry.") from exc
    if len(raw) > 2_000_000:
        raise OutlineError("Outline returned more data than the configured size limit.")
    try:
        result = json.loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise OutlineError("Outline returned an invalid API response.") from exc
    if not isinstance(result, dict) or result.get("ok") is False:
        raise OutlineError("Outline could not complete the request. Check collection permissions.")
    return result


def list_collections(instance_url: str, token: str) -> list[dict[str, str]]:
    base = normalize_instance_url(instance_url)
    result = _outline_post(base, token, "collections.list", {"limit": 100, "offset": 0})
    data = result.get("data")
    if not isinstance(data, list):
        raise OutlineError("Outline returned an invalid collection list.")
    return sorted(({"id": str(item.get("id", "")), "name": str(item.get("name", ""))[:160]}
                   for item in data if isinstance(item, dict) and item.get("id") and item.get("name")),
                  key=lambda item: (item["name"].casefold(), item["id"]))[:100]


def _document_details(instance_url: str, token: str, listed: dict[str, Any], collection_id: str) -> dict[str, Any]:
    if isinstance(listed.get("text"), str) and listed["text"].strip():
        return listed
    document_id = listed.get("id")
    if not isinstance(document_id, str) or not document_id:
        return listed
    result = _outline_post(instance_url, token, "documents.info", {"id": document_id})
    detail = result.get("data")
    if not isinstance(detail, dict) or detail.get("collectionId") != collection_id:
        raise OutlineError("Outline returned a document outside the configured collection.")
    return detail


def fetch_business_rules(connection: dict[str, Any], token: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fetch one bounded collection and normalize documents into stable rules."""
    base = normalize_instance_url(connection.get("instance_url"))
    collection_id = connection.get("collection_id")
    result = _outline_post(base, token, "documents.list", {
        "collectionId": collection_id, "limit": MAX_DOCUMENTS, "offset": 0,
        "sort": "updatedAt", "direction": "ASC",
    })
    data = result.get("data")
    if isinstance(data, dict):
        data = data.get("documents")
    if not isinstance(data, list):
        raise OutlineError("Outline returned an invalid document list.")
    documents = sorted((item for item in data if isinstance(item, dict)
                        and item.get("collectionId") == collection_id), key=lambda item: str(item.get("id", "")))
    pagination = result.get("pagination")
    excess = (isinstance(pagination, dict) and bool(pagination.get("nextPath"))) or len(documents) > MAX_DOCUMENTS
    selected = documents[:MAX_DOCUMENTS]
    with ThreadPoolExecutor(max_workers=8) as pool:
        details = list(pool.map(lambda item: _document_details(base, token, item, collection_id), selected))
    rules = []
    oversized_count = 0
    for document in details:
        document_id = document.get("id")
        title = document.get("title")
        text = document.get("text") or document.get("body") or ""
        if (not isinstance(document_id, str) or not document_id or not isinstance(title, str)
                or not title.strip() or len(title.strip()) > 200):
            continue
        if not isinstance(text, str) or not text.strip():
            continue
        text = text.strip()
        if len(text) > MAX_DOCUMENT_CHARS:
            oversized_count += 1
            continue
        updated_at = str(document.get("updatedAt") or "")[:80]
        title = title.strip()
        digest = sha256((title + "\n\n" + text).encode("utf-8")).hexdigest()
        revision = document.get("revision")
        version = revision if type(revision) is int and revision > 0 else 1
        rules.append({"rule_id": document_id, "version": version, "title": title,
                      "statement": text, "category": "business_rule", "enabled": True,
                      "collection_id": collection_id, "updated_at": updated_at,
                      "content_hash": digest})
    summary = {"status": "synchronized", "collection_id": collection_id,
               "document_count": len(documents), "imported_count": len(rules),
               "omitted_count": max(0, len(documents) - len(rules)),
               "oversized_count": oversized_count,
               "limit_exceeded": excess, "max_documents": MAX_DOCUMENTS,
               "synchronized_at": datetime.now(timezone.utc).isoformat()}
    return rules, summary


def fetch_business_rule_documents(connection: dict[str, Any], token: str) -> list[dict[str, str]]:
    """Return bounded, linkable document metadata from the configured collection."""
    base = normalize_instance_url(connection.get("instance_url"))
    collection_id = connection.get("collection_id")
    result = _outline_post(base, token, "documents.list", {
        "collectionId": collection_id, "limit": MAX_DOCUMENTS, "offset": 0,
        "sort": "updatedAt", "direction": "ASC",
    })
    data = result.get("data")
    if isinstance(data, dict):
        data = data.get("documents")
    if not isinstance(data, list):
        raise OutlineError("Outline returned an invalid document list.")
    documents = []
    for item in data:
        if not isinstance(item, dict) or item.get("collectionId") != collection_id:
            continue
        document_id = item.get("id")
        title = item.get("title")
        url_id = item.get("urlId")
        if not isinstance(document_id, str) or not document_id or not isinstance(title, str) or not title.strip():
            continue
        identity = url_id if isinstance(url_id, str) and url_id else document_id
        documents.append({
            "id": document_id,
            "title": title.strip()[:240],
            "url": f"/doc/{quote(identity, safe='')}",
            "updated_at": str(item.get("updatedAt", ""))[:64],
        })
    return sorted(documents, key=lambda item: (item["title"].casefold(), item["id"]))[:MAX_DOCUMENTS]


def discover_collections(instance_url: str, token: str) -> list[dict[str, str]]:
    return list_collections(instance_url, token)
