"""Documentation retrieval — Context7 primary, GitMCP(HTTP) fallback.

Fetches version-aware package documentation to ground code-generation /
tool-job answers. Two providers, strict ordering:

1. Context7   — resolve library id, then fetch relevant doc chunks.
2. GitMCP     — plain HTTP fetch of official repo ``llms.txt`` /
   ``llms-full.txt`` / docs tree / README (no MCP server needed).
3. Internal model knowledge — only when both providers fail (with a warning
   in the result unless strict mode raises).

Never dumps raw docs into final answers: consumers receive a bounded chunk
list plus provenance records.
"""

from __future__ import annotations

import os
import re
import urllib.error as error
import urllib.parse as parse
import urllib.request
from typing import Any

from infra.settings import settings

CONTEXT7_SEARCH_URL = "https://context7.com/api/v2/libs/search"
CONTEXT7_DOCS_URL = "https://context7.com/api/v2/context"

# Best-effort official repository map for GitMCP fallback (packages whose docs
# are not published through Context7, or when Context7 is down). Only official
# sources, per spec rule "prefer official docs + official repo".
_OFFICIAL_REPOS: dict[str, str] = {
    "laravel/framework": "laravel/framework",
    "symfony/http-foundation": "symfony/http-foundation",
    "symfony/console": "symfony/console",
    "guzzlehttp/guzzle": "guzzle/guzzle",
    "monolog/monolog": "Seldaek/monolog",
    "vlucas/phpdotenv": "vlucas/phpdotenv",
    "stripe/stripe-php": "stripe/stripe-php",
    "mollie/mollie-api-php": "mollie/mollie-api-php",
    "react": "facebook/react",
    "vue": "vuejs/core",
    "express": "expressjs/express",
    "fastapi": "fastapi/fastapi",
    "flask": "pallets/flask",
    "django": "django/django",
    "requests": "psf/requests",
    "sqlalchemy": "sqlalchemy/sqlalchemy",
    "pydantic": "pydantic/pydantic",
    "redis": "redis/redis-py",
    "celery": "celery/celery",
    "qdrant-client": "qdrant/qdrant-client",
}

_DOC_TOKEN_BUDGET = 6000  # rough upper bound of tokens worth injecting


class DocRetrievalError(RuntimeError):
    """Raised in strict mode when no provider could verify documentation."""


def _http_get(url: str, timeout: float, api_key: str = "") -> str | None:
    headers = {"User-Agent": "developer-assistant/1.0"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 — any transport error means "provider down"
        return None


def _resolve_library(name: str, timeout: float) -> dict[str, Any] | None:
    """Resolve a package name to a Context7 library id (+versions).

    Returns ``{"id": ..., "versions": [...]}`` or None when unresolvable.
    """
    q = urllib.parse.quote(name)
    api_key = os.getenv(settings.context7_api_key_env or "CONTEXT7_API_KEY", "") or ""
    raw = _http_get(f"{CONTEXT7_SEARCH_URL}?query={q}", timeout, api_key)
    if not raw:
        return None
    try:
        payload = _http_json(raw)
    except Exception:  # noqa: BLE001 — malformed reply counts as unresolvable
        return None
    results = payload.get("results") or []
    if not results:
        return None
    first = results[0]
    return {
        "id": str(first.get("id") or ""),
        "versions": first.get("versions") or [],
    }


def _http_json(raw: str) -> dict[str, Any]:
    import json

    data = json.loads(raw)
    return data if isinstance(data, dict) else {}


def _match_version(versions: list[str], resolved: str | None) -> str | None:
    """Find the closest Context7 version selector for the resolved version.

    Context7 version lists look like ``["__branch__10.x", "__branch__13.x"]``
    or ``["v11.6.1", ...]``. Matches by major version when possible; returns
    None when nothing compatible exists (caller queries versionless).
    """
    if not resolved or not versions:
        return None
    major = re.match(r"^v?(\d+)", resolved)
    if not major:
        return None
    want = int(major.group(1))

    def _compat(v: str) -> int | None:
        v = v.replace("__branch__", "").replace("__tag__", "")
        m = re.match(r"^v?(\d+)(?:\.(\d+))?", v)
        return int(m.group(1)) if m else None

    # exact major first, prefer the smallest difference above/below — but never
    # silently a NEWER major than installed (spec rule 8). Prefer same major,
    # else the highest lower major.
    candidates = [(v, _compat(v)) for v in versions]
    candidates = [(v, maj) for v, maj in candidates if maj is not None]
    same = [v for v, maj in candidates if maj == want]
    if same:
        return same[0]
    lower = [v for v, maj in candidates if maj < want]
    if lower:
        return max(lower, key=lambda v: _compat(v))
    return None


def _query_context7(
    library_id: str, query: str, version: str | None, timeout: float
) -> list[str]:
    """Fetch doc chunks from Context7, returns list of markdown sections."""
    params = {
        "libraryId": library_id,
        "query": query[:500],
        "max_tokens": str(_DOC_TOKEN_BUDGET),
    }
    if version:
        params["version"] = version
    url = f"{CONTEXT7_DOCS_URL}?{urllib.parse.urlencode(params)}"
    api_key = os.getenv(settings.context7_api_key_env or "CONTEXT7_API_KEY", "") or ""
    raw = _http_get(url, timeout, api_key)
    if not raw:
        return []
    # response is plain text, sections separated by `---`
    sections = [s.strip() for s in re.split(r"\n-{3,}\n", raw) if s.strip()]
    return sections[:20]


def _gitmcp_fetch(package: str, resolved: str | None, query: str, timeout: float) -> list[str]:
    """HTTP fallback: fetch official repo docs (llms.txt preferred)."""
    repo = _OFFICIAL_REPOS.get(package)
    if not repo:
        # heuristic: convert "vendor/name" or "name" to owner/name is too
        # speculative — skip rather than fetch a wrong repo (spec rule 10).
        return []
    branch = _git_branch_for(resolved)
    candidates = [
        f"https://raw.githubusercontent.com/{repo}/{branch}/llms.txt",
        f"https://raw.githubusercontent.com/{repo}/{branch}/llms-full.txt",
    ]
    for url in candidates:
        raw = _http_get(url, timeout)
        if raw and len(raw) > 200:
            return _chunk_llms(raw, query)
    return []


def _git_branch_for(resolved: str | None) -> str:
    if not resolved:
        return "main"
    major = re.match(r"^v?(\d+)\.(\d+)", resolved)
    if major:
        return f"{major.group(1)}.{major.group(2)}"
    return "main"


def _chunk_llms(raw: str, query: str) -> list[str]:
    """Split an llms.txt body into sections; keep ones relevant to query."""
    # filter to lines that look like doc entries, cap length
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if len(lines) > 2000:
        lines = lines[:2000]
    # try to match sections containing query terms
    terms = [t for t in re.split(r"\W+", query.lower()) if len(t) > 3][:6]
    if terms:
        matched = [ln for ln in lines if any(t in ln.lower() for t in terms)]
        if matched:
            return matched[:15]
    return lines[:15]


def retrieve_documentation(
    packages: dict[str, Any],
    query: str,
    enabled: bool | None = None,
    strict: bool | None = None,
) -> dict[str, Any]:
    """Resolve documentation for ``packages`` relevant to ``query``.

    Returns:
        ``{"ok": True, "chunks": [...], "provider": ..., "provenance": [...],
          "warning": optional-string}`` or ``{"ok": False, ...}`` with a
          warning when both providers failed and strict mode is off.

    Raises ``DocRetrievalError`` in strict mode when nothing was retrieved.
    """
    if enabled is None:
        enabled = settings.doc_retrieval_enabled
    if strict is None:
        strict = settings.doc_strict_mode
    if not enabled or not packages:
        return {"ok": True, "chunks": [], "provider": None, "provenance": [], "warning": None}

    context7_on = settings.doc_context7_enabled
    gitmcp_on = settings.doc_gitmcp_enabled
    c7_timeout = settings.doc_context7_timeout
    gm_timeout = settings.doc_gitmcp_timeout

    # Pick the packages most likely relevant to the user's question:
    #   1) direct/manifest-declared dependencies first (they carry a requested
    #      constraint and are what the codebase actually imports),
    #   2) installed over manifest-only,
    #   3) otherwise alphabetical.
    # Transitive deps are numerous (a Laravel lock has 70+); querying all of
    # them is slow and mostly empty, so we cap the batch.
    def _rank(kv: tuple[str, dict[str, Any]]) -> tuple:
        meta = kv[1]
        return (
            meta.get("requested_version") is None,  # direct deps first
            meta.get("installed") is not False,      # installed before manifest-only
            kv[0].lower(),
        )

    ranked = sorted(packages.items(), key=_rank)
    provenance: list[dict[str, Any]] = []
    all_chunks: list[str] = []
    provider_used: str | None = None
    warnings: list[str] = []

    # Transitive dep lists are large (a Laravel lock has 70+ packages) and each
    # Context7 probe costs ~2s, so probe candidates concurrently (bounded) and
    # stop early once enough grounding chunks are collected.
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _probe(name: str, meta: dict[str, Any]) -> dict[str, Any]:
        resolved = meta.get("resolved_version")
        out = {
            "name": name,
            "record": {
                "package": name,
                "installed_version": resolved,
                "provider": None,
                "library_id": None,
                "repo": None,
                "exact_version_found": False,
            },
            "chunks": [],
            "warning": None,
        }
        try:
            if context7_on:
                lib = _resolve_library(name, c7_timeout)
                if lib and lib.get("id"):
                    version = _match_version(lib.get("versions") or [], resolved)
                    out["record"]["library_id"] = lib["id"]
                    out["record"]["exact_version_found"] = version is not None
                    chunks = _query_context7(lib["id"], query, version, c7_timeout)
                    if chunks:
                        out["record"]["provider"] = "context7"
                        out["chunks"] = chunks
                        return out
            if gitmcp_on:
                chunks = _gitmcp_fetch(name, resolved, query, gm_timeout)
                if chunks:
                    out["record"]["provider"] = "gitmcp"
                    out["record"]["repo"] = _OFFICIAL_REPOS.get(name)
                    out["chunks"] = chunks
                    return out
            out["warning"] = (
                f"Documentation lookup failed for dependency '{name}'. The answer "
                "is based on model knowledge and should be verified against the "
                "project's installed version."
            )
        except Exception:  # noqa: BLE001 — never let one bad package kill the batch
            out["warning"] = (
                f"Documentation lookup failed for dependency '{name}'. The answer "
                "is based on model knowledge and should be verified against the "
                "project's installed version."
            )
        return out

    max_attempts = min(10, len(ranked))
    candidates = []
    for attempted, (name, meta) in enumerate(ranked):
        if attempted >= max_attempts:
            break
        candidates.append((name, meta))

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_probe, name, meta): name for name, meta in candidates}
        for fut in as_completed(futures):
            result = fut.result()
            provenance.append(result["record"])
            if result["chunks"]:
                all_chunks.extend(result["chunks"])
                if provider_used is None:
                    provider_used = result["record"]["provider"]
            elif result["warning"]:
                warnings.append(result["warning"])
            if len(all_chunks) >= 40:  # enough grounding gathered
                for pending in futures:
                    pending.cancel()
                break

    # keep provenance deterministic (input rank order)
    provenance.sort(key=lambda r: [n for n, _ in ranked].index(r["package"]))

    if all_chunks:
        return {
            "ok": True,
            "chunks": all_chunks,
            "provider": provider_used,
            "provenance": provenance,
            "warning": None,
        }
    if strict:
        raise DocRetrievalError(
            "No current documentation could be verified for the project's "
            "dependencies (Context7 and GitMCP both unavailable)."
        )
    return {
        "ok": False,
        "chunks": [],
        "provider": None,
        "provenance": provenance,
        "warning": " ".join(warnings) if warnings else None,
    }
