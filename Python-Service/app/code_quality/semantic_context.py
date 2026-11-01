"""Bounded code-context retrieval pinned to one immutable source revision."""
from __future__ import annotations

from hashlib import sha256
from pathlib import PurePosixPath
from typing import Any
import logging

from telemetry.snapshot import Snapshot, source_filter, validate_project_id, validate_revision_id

RETRIEVER_VERSION = "revision-context-7"
MAX_QUERIES = 10
MAX_RESULTS_PER_QUERY = 1
MAX_RULE_QUERIES = 20
# One slot for each of the two targeted searches across the five categories.
MAX_CONTEXTS = 10
MAX_SNIPPET_CHARS = 1_400
logger = logging.getLogger(__name__)

_CATEGORY_QUERIES = (
    ("security", "authentication authorization middleware access control validation"),
    ("security", "secret handling password storage injection unsafe input"),
    ("clean_code", "SOLID single responsibility open closed substitutability interface segregation dependency inversion class cohesion"),
    ("clean_code", "class responsibilities cohesion dependency direction method delegation"),
    ("maintainability", "module boundaries dependencies coupling framework conventions"),
    ("maintainability", "large methods duplicated logic change impact abstractions"),
    ("reliability", "payment retry idempotency transaction rollback exception recovery"),
    ("reliability", "validation failure handling null safety consistency error paths"),
    ("performance", "database queries inside loops eager loading N plus one pagination"),
    ("performance", "unbounded collection loading repeated queries memory allocation caching"),
)


def _unavailable(
    revision_id: str,
    reason: str,
    failure_code: str,
    *,
    query_count: int = 0,
    searched_collection_count: int = 0,
    candidate_count: int = 0,
    rejected_revision_count: int = 0,
    rejected_source_count: int = 0,
    category_search: dict[str, dict[str, int]] | None = None,
    diagnostic_detail: str | None = None,
) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "revision_id": revision_id,
        "retriever_version": RETRIEVER_VERSION,
        "query_count": query_count,
        "evidence_count": 0,
        "failure_code": failure_code,
        "searched_collection_count": searched_collection_count,
        "candidate_count": candidate_count,
        "rejected_revision_count": rejected_revision_count,
        "rejected_source_count": rejected_source_count,
        "category_search": category_search or {},
        "diagnostic_detail": diagnostic_detail,
        "reason": reason,
    }


_FAILURE_REASONS = {
    "index_not_ready": "The selected revision does not have a ready semantic index.",
    "index_identity_mismatch": "The semantic index identity does not match this source revision.",
    "qdrant_unavailable": "The vector search service could not be reached.",
    "collection_missing": "The selected revision's vector collection is missing; re-index this revision.",
    "embedding_unavailable": "The query embedding provider could not create a search vector.",
    "qdrant_search_failed": "The vector search request failed; check the Qdrant service and API logs.",
    "revision_metadata_mismatch": "Search returned candidates, but none matched this revision's identity metadata.",
    "source_inventory_mismatch": "Search returned candidates, but none matched the assessment's business-source inventory.",
    "retrieval_internal_error": "Semantic retrieval failed unexpectedly; check the API logs for its diagnostic event.",
}


def _valid_location(file: Any, line: Any) -> tuple[str, int] | None:
    if not isinstance(file, str) or not file or len(file) > 300:
        return None
    path = PurePosixPath(file)
    if path.is_absolute() or ".." in path.parts or "\\" in file:
        return None
    if type(line) is not int or line < 1:
        return None
    return path.as_posix(), line


def retrieve_revision_context(
    project_id: str,
    revision_id: str,
    snapshot: dict[str, Any],
    business_files: list[str],
    business_rules: list[dict[str, Any]] | None = None,
    *, include_clean_code: bool = True,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Search one ready snapshot and return bounded, citation-ready excerpts.

    Retrieved source text is transient: callers may put it in model context but
    should persist only the evidence ID and source location.
    """
    validate_project_id(project_id)
    validate_revision_id(revision_id)
    commit_sha = snapshot.get("commit_sha")
    archive_hash = snapshot.get("source_archive_sha256")
    if (snapshot.get("state") != "ready" or snapshot.get("project_id") != project_id
            or snapshot.get("revision_id") != revision_id or not archive_hash):
        return _unavailable(revision_id, _FAILURE_REASONS["index_not_ready"], "index_not_ready"), []

    expected_collection = Snapshot(project_id, commit_sha, archive_hash).collection
    if snapshot.get("collection_name") != expected_collection:
        return _unavailable(revision_id, _FAILURE_REASONS["index_identity_mismatch"], "index_identity_mismatch"), []

    # The semantic index can contain files outside the assessment's source
    # inventory. Keep those files out of evaluator context.
    allowed_files = {
        location[0]
        for file in business_files
        if (location := _valid_location(file, 1)) is not None
    }
    if not allowed_files:
        return ({
            "status": "available_no_matches",
            "revision_id": revision_id,
            "retriever_version": RETRIEVER_VERSION,
            "query_count": 0,
            "evidence_count": 0,
            "failure_code": "no_business_source_files",
            "reason": "The assessment inventory contains no business source files for semantic context.",
        }, [])

    try:
        from index.llamaindex_layer import CodePropertyGraph

        retriever = CodePropertyGraph()
        collected: dict[str, dict[str, Any]] = {}
        searched = set()
        query_count = 0
        candidate_count = 0
        rejected_revision_count = 0
        rejected_source_count = 0
        ordered_rules = sorted(
            (rule for rule in (business_rules or []) if isinstance(rule, dict)
             and rule.get("enabled") is True and isinstance(rule.get("rule_id"), str)
             and (rule.get("category") == "business_rule"
                  or rule.get("category") in {item[0] for item in _CATEGORY_QUERIES})),
            key=lambda rule: (rule["rule_id"],),
        )
        rule_queries = [
            (rule["category"], f"{rule.get('title', '')} {rule.get('statement', '')}"[:800], rule["rule_id"])
            for rule in ordered_rules[:MAX_RULE_QUERIES]
            if isinstance(rule.get("title"), str) and isinstance(rule.get("statement"), str)
        ]
        category_queries = [(category, query, None) for category, query in _CATEGORY_QUERIES[:MAX_QUERIES]
                            if include_clean_code or category != "clean_code"]
        # Always search the five scoring categories before optional business-rule
        # queries so a large rule set cannot starve Reliability or Performance.
        queries = category_queries + rule_queries
        max_contexts = MAX_CONTEXTS + len(rule_queries)
        category_search = {
            category: {
                "attempted_queries": 0,
                "candidate_count": 0,
                "accepted_count": 0,
                "rejected_revision_count": 0,
                "rejected_source_count": 0,
            }
            for category in dict.fromkeys(item[0] for item in category_queries)
        }
        for query_index, (category, query, rule_id) in enumerate(queries):
            if query_index >= len(category_queries) and len(collected) >= max_contexts:
                break
            query_count += 1
            category_stats = category_search.get(category) if rule_id is None else None
            if category_stats is not None:
                category_stats["attempted_queries"] += 1
            result = retriever.hybrid_retrieve(
                query=query,
                collection_name=expected_collection,
                limit=1 if rule_id else MAX_RESULTS_PER_QUERY,
                graph_legs=["calls"],
                join_sibling=False,
                apply_rerank=False,
            )
            searched.update(result.get("searched_collections") or [])
            failure_code = result.get("failure_code")
            if failure_code or result.get("mode") not in {"hybrid", "vector_only"}:
                failure_code = failure_code or "qdrant_unavailable"
                return _unavailable(
                    revision_id,
                    _FAILURE_REASONS.get(failure_code, "The selected revision's semantic search service is unavailable."),
                    failure_code,
                    query_count=query_count,
                    searched_collection_count=len(searched),
                    candidate_count=candidate_count,
                    rejected_revision_count=rejected_revision_count,
                    rejected_source_count=rejected_source_count,
                    category_search=category_search,
                    diagnostic_detail=result.get("failure_detail"),
                ), []
            for hit in result.get("results") or []:
                candidate_count += 1
                if category_stats is not None:
                    category_stats["candidate_count"] += 1
                payload = hit.get("payload") or {}
                hit_collection = hit.get("collection") or payload.get("collection")
                collection_matches = (
                    hit_collection == expected_collection
                    or (isinstance(hit_collection, str)
                        and hit_collection.startswith(expected_collection + "_cohere_"))
                )
                if not collection_matches:
                    rejected_source_count += 1
                    if category_stats is not None:
                        category_stats["rejected_source_count"] += 1
                    continue
                if (payload.get("project_id") != project_id
                        or any(payload.get(key) != value for key, value in source_filter(snapshot).items())
                        or payload.get("source_archive_sha256") != archive_hash
                        or payload.get("dirty") is not False):
                    rejected_revision_count += 1
                    if category_stats is not None:
                        category_stats["rejected_revision_count"] += 1
                    continue
                indexed_revision = payload.get("revision_id")
                if indexed_revision is not None and indexed_revision != revision_id:
                    rejected_revision_count += 1
                    if category_stats is not None:
                        category_stats["rejected_revision_count"] += 1
                    continue
                location = _valid_location(payload.get("file_path") or payload.get("file"),
                                           payload.get("start_line") or payload.get("line"))
                code = payload.get("code")
                if (location is None or location[0] not in allowed_files
                        or not isinstance(code, str) or not code.strip()):
                    rejected_source_count += 1
                    if category_stats is not None:
                        category_stats["rejected_source_count"] += 1
                    continue
                code = code.strip()[:MAX_SNIPPET_CHARS]
                file, line = location
                identity = sha256(f"{file}:{line}:{code}".encode("utf-8")).hexdigest()
                key = f"{category}:{rule_id}:{identity}" if rule_id else f"{category}:{identity}"
                if key in collected:
                    continue
                evidence_id = "ev_" + sha256(
                    (f"{project_id}:{revision_id}:{category}:{rule_id}:{identity}" if rule_id else
                     f"{project_id}:{revision_id}:{category}:{identity}").encode("utf-8")
                ).hexdigest()[:16]
                symbol = str(payload.get("fq_symbol_name") or payload.get("symbol_name") or payload.get("name") or "")[:160]
                source_kind = "graph neighbor" if hit.get("via_graph") else "vector match"
                collected[key] = {
                    "evidence_id": evidence_id,
                    "kind": "semantic_context",
                    "category": category,
                    "rule": RETRIEVER_VERSION if rule_id else "revision-context-2",
                    **({"business_rule_id": rule_id} if rule_id else {}),
                    "file": file,
                    "line": line,
                    "message": f"Revision-matched {source_kind}" + (f" for {symbol}." if symbol else "."),
                    "context_snippet": code,
                }
                if category_stats is not None:
                    category_stats["accepted_count"] += 1
                if len(collected) >= max_contexts:
                    break
            if query_index >= len(category_queries) and len(collected) >= max_contexts:
                break

        allowed_searched = all(
            name == expected_collection or (
                isinstance(name, str) and name.startswith(expected_collection + "_cohere_")
            ) for name in searched
        )
        if not searched or not allowed_searched:
            return _unavailable(
                revision_id,
                _FAILURE_REASONS["collection_missing"] if not searched else _FAILURE_REASONS["index_identity_mismatch"],
                "collection_missing" if not searched else "index_identity_mismatch",
                query_count=query_count,
                searched_collection_count=len(searched),
                candidate_count=candidate_count,
                rejected_revision_count=rejected_revision_count,
                rejected_source_count=rejected_source_count,
                category_search=category_search,
            ), []

        if not collected and rejected_revision_count:
            code = "revision_metadata_mismatch"
            return _unavailable(
                revision_id, _FAILURE_REASONS[code], code,
                query_count=query_count,
                searched_collection_count=len(searched),
                candidate_count=candidate_count,
                rejected_revision_count=rejected_revision_count,
                rejected_source_count=rejected_source_count,
                category_search=category_search,
            ), []

        if not collected and rejected_source_count:
            code = "source_inventory_mismatch"
            return _unavailable(
                revision_id, _FAILURE_REASONS[code], code,
                query_count=query_count,
                searched_collection_count=len(searched),
                candidate_count=candidate_count,
                rejected_revision_count=rejected_revision_count,
                rejected_source_count=rejected_source_count,
                category_search=category_search,
            ), []

        evidence = list(collected.values())
        status = "available" if evidence else "available_no_matches"
        reason = None if evidence else "The revision index was searched, but the bounded queries found no matching code context."
        return ({
            "status": status,
            "revision_id": revision_id,
            "retriever_version": RETRIEVER_VERSION,
            "query_count": query_count,
            "business_rule_query_count": min(len(ordered_rules), MAX_RULE_QUERIES),
            "omitted_business_rule_ids": [rule["rule_id"] for rule in ordered_rules[MAX_RULE_QUERIES:]],
            "evidence_count": len(evidence),
            "failure_code": None if evidence else "no_matching_chunks",
            "searched_collection_count": len(searched),
            "candidate_count": candidate_count,
            "rejected_revision_count": rejected_revision_count,
            "rejected_source_count": rejected_source_count,
            "category_search": category_search,
            "reason": reason,
        }, evidence)
    except Exception as exc:
        logger.warning(
            "Semantic context retrieval failed project=%s revision=%s error_type=%s",
            project_id,
            revision_id,
            type(exc).__name__,
        )
        return _unavailable(
            revision_id,
            _FAILURE_REASONS["retrieval_internal_error"],
            "retrieval_internal_error",
            query_count=locals().get("query_count", 0),
            searched_collection_count=len(locals().get("searched", set())),
            candidate_count=locals().get("candidate_count", 0),
            rejected_revision_count=locals().get("rejected_revision_count", 0),
            rejected_source_count=locals().get("rejected_source_count", 0),
            category_search=locals().get("category_search", {}),
        ), []
