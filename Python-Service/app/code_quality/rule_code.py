"""Code-side helpers for business rules: indexed revisions, symbol hashes, link suggestions, diff mapping."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
import logging
import re
import zipfile
from typing import Any, Callable

from code_quality.rule_index import RuleIndex, RuleIndexError

MAX_SUGGESTIONS_PER_RULE = 8
MAX_SUGGESTION_RULES = 50
# Below this cosine similarity a hit is usually unrelated (a rule with no implementation on Pizza
# peaked at 0.50 while real implementations scored above 0.6).
MIN_SUGGESTION_SCORE = 0.55
_TEST_PATH = re.compile(r"(^|/)(tests?|spec)/|Test\.php$|Spec\.php$", re.I)
logger = logging.getLogger(__name__)
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class RevisionUnavailable(RuleIndexError):
    """The indexed revision or its source cannot be used."""


@dataclass
class Revision:
    snapshot: dict[str, Any]
    definitions: dict[str, dict]
    archive_loader: Callable[[], bytes]

    @property
    def revision_id(self) -> str:
        return self.snapshot["revision_id"]


def load_revision(project_id: str, revision_id: str, registry=None) -> Revision:
    from telemetry.analysis import _definition_map
    from telemetry.runtime import snapshot_definitions
    from telemetry.snapshot import source_reference

    if registry is None:
        from telemetry.registry import default_registry
        registry = default_registry()
    snapshot = registry.get_by_revision_id(project_id, revision_id)
    if snapshot is None or snapshot.get("state") != "ready" or not snapshot.get("commit_sha"):
        raise RevisionUnavailable("The selected revision is not indexed. Index it and retry.")
    definitions = _definition_map(snapshot_definitions(project_id, snapshot["commit_sha"]))
    archive_key = registry.archive_key(project_id, source_reference(snapshot))
    cache: dict[str, bytes] = {}

    def archive_loader() -> bytes:
        if "data" not in cache:
            if not archive_key:
                raise RevisionUnavailable("The source archive for this revision is no longer available.")
            from index.storage import storage
            cache["data"] = storage.get_snapshot_archive(archive_key)
        return cache["data"]

    return Revision({**snapshot, "project_id": project_id, "archive_key": archive_key}, definitions, archive_loader)


def symbol_hashes(archive: bytes, definitions: dict[str, dict], symbol_ids: list[str]) -> dict[str, str]:
    """Hash each symbol's own source lines so a later revision can tell whether it changed."""
    wanted = [symbol for symbol in symbol_ids if symbol in definitions]
    files: dict[str, list[str]] = {}
    result: dict[str, str] = {}
    try:
        with zipfile.ZipFile(BytesIO(archive)) as bundle:
            for symbol_id in wanted:
                definition = definitions[symbol_id]
                path = definition["file"]
                if path not in files:
                    try:
                        files[path] = bundle.read(path).decode("utf-8", errors="replace").splitlines()
                    except KeyError:
                        files[path] = []
                lines = files[path]
                if not lines:
                    continue
                start, end = definition["line"], max(definition["line"], definition["end_line"])
                body = "\n".join(line.rstrip() for line in lines[start - 1:end])
                result[symbol_id] = sha256(body.encode("utf-8")).hexdigest()
    except (zipfile.BadZipFile, OSError) as exc:
        raise RevisionUnavailable("Indexed source is unavailable for this revision.") from exc
    return result


def refresh_code_hashes(project_id: str, revision: Revision, index: RuleIndex | None = None) -> int:
    """Record the latest source hash of every reviewed link so code drift is visible."""
    index = index or RuleIndex()
    symbols = [symbol for symbol in index.reviewed_symbols(project_id) if symbol in revision.definitions]
    if not symbols:
        return 0
    return index.record_code_hashes(project_id, revision.revision_id,
                                    symbol_hashes(revision.archive_loader(), revision.definitions, symbols))


def suggest_links(project_id: str, revision: Revision, doc_ids: list[str] | None = None, *,
                  index: RuleIndex | None = None, retrieve: Callable[..., dict] | None = None) -> dict[str, Any]:
    """Search the revision's code index with each rule and store hits that map to a canonical symbol."""
    from telemetry.snapshot import Snapshot

    index = index or RuleIndex()
    documents = index.documents(project_id, include_text=True)
    if doc_ids is not None:
        wanted = set(doc_ids)
        documents = [item for item in documents if item["doc_id"] in wanted]
    documents = documents[:MAX_SUGGESTION_RULES]
    snapshot = revision.snapshot
    collection = Snapshot(project_id, snapshot["commit_sha"], snapshot.get("source_archive_sha256")).collection
    if retrieve is None:
        from index.llamaindex_layer import CodePropertyGraph
        retrieve = CodePropertyGraph().hybrid_retrieve
    added, rules = 0, 0
    for document in documents:
        result = retrieve(query=f"{document['title']} {document['text']}"[:800], collection_name=collection,
                          limit=MAX_SUGGESTIONS_PER_RULE * 3, graph_legs=["calls"], join_sibling=False,
                          apply_rerank=False)
        if result.get("failure_code"):
            logger.warning("business_rule_link_search_failed project=%s failure_code=%s detail=%s",
                           project_id, result.get("failure_code"), str(result.get("failure_detail") or "")[:200])
            from code_quality.semantic_context import _FAILURE_REASONS
            raise RevisionUnavailable(_FAILURE_REASONS.get(result.get("failure_code"),
                                      "Code search is unavailable for this revision.") + " Retry in a moment.")
        items = []
        for hit in result.get("results") or []:
            payload = hit.get("payload") or {}
            symbol_id = payload.get("symbol_id")
            if not isinstance(symbol_id, str) or not symbol_id or payload.get("project_id") != project_id:
                continue
            definition = revision.definitions.get(symbol_id, {})
            file = definition.get("file") or payload.get("file_path") or ""
            score = hit.get("score")
            # Tests show a rule is checked (the Tested badge), not where it is implemented.
            if _TEST_PATH.search(file) or symbol_id.startswith("closure@tests/"):
                continue
            if isinstance(score, (int, float)) and score < MIN_SUGGESTION_SCORE:
                continue
            if len(items) >= MAX_SUGGESTIONS_PER_RULE:
                break
            items.append({"symbol_id": symbol_id, "score": score,
                          "file": definition.get("file") or payload.get("file_path"),
                          "line": definition.get("line") or payload.get("start_line"),
                          "revision_id": revision.revision_id})
        if items:
            rules += 1
            added += index.add_suggestions(project_id, document["doc_id"], items)
    return {"rules_searched": len(documents), "rules_with_matches": rules, "suggestions_written": added}


def changed_symbols(diff: str, definitions: dict[str, dict]) -> tuple[list[dict[str, Any]], list[str]]:
    """Map unified-diff hunks (old side) to the symbols whose line ranges they touch."""
    hunks: list[dict[str, Any]] = []
    path = None
    for line in diff.splitlines():
        if line.startswith("--- "):
            candidate = line[4:].split("\t")[0].strip()
            path = None if candidate == "/dev/null" else re.sub(r"^a/", "", candidate)
        elif line.startswith("+++ ") and path is None:
            candidate = line[4:].split("\t")[0].strip()
            path = None if candidate == "/dev/null" else re.sub(r"^b/", "", candidate)
        elif (match := _HUNK.match(line)) and path:
            start, count = int(match.group(1)), int(match.group(2) or 1)
            hunks.append({"id": f"h{len(hunks) + 1}", "file": path, "start": start,
                          "end": start + max(count, 1) - 1, "text": []})
        elif hunks and line[:1] in {"+", "-", " "}:
            hunks[-1]["text"].append(line)
    by_file: dict[str, list[dict]] = {}
    for definition in definitions.values():
        by_file.setdefault(definition["file"], []).append(definition)
    touched: list[str] = []
    for hunk in hunks:
        hunk["text"] = "\n".join(hunk["text"])[:4000]
        hunk["symbol_ids"] = []
        candidates = [item for item in by_file.get(hunk["file"], [])
                      if item["line"] <= hunk["end"] and max(item["line"], item["end_line"]) >= hunk["start"]]
        # Prefer the innermost definitions (methods over their class).
        candidates.sort(key=lambda item: max(item["line"], item["end_line"]) - item["line"])
        innermost = [item for item in candidates
                     if not any(other is not item and other["line"] >= item["line"]
                                and max(other["line"], other["end_line"]) <= max(item["line"], item["end_line"])
                                for other in candidates)]
        for item in innermost:
            hunk["symbol_ids"].append(item["symbol_id"])
            if item["symbol_id"] not in touched:
                touched.append(item["symbol_id"])
    return hunks, touched
