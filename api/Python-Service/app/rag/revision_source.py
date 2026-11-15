"""Resolve a chat request's project revision once: "latest" or a pinned revision ID.

A request in revision mode carries ``{"project_id": ..., "revision": "latest" | "rev_…"}``
instead of an uploaded file. It is resolved to one ready revision, and everything the
answer reads follows from that row: the Qdrant collection (pinned here, never taken from
the client), and ``revision_id``/``commit_sha`` for the code tools, which read the
revision's snapshot archive. "Latest" is the project's newest ready revision of any kind,
working-tree revisions included (business rule "Chat follows the latest revision").
"""

from __future__ import annotations

from typing import Any

# Client fields that revision mode replaces with values from the resolved registry row.
_REPLACED_FIELDS = ("revision", "filename", "upload_session_id", "collection", "commit_sha",
                    "revision_id", "source_ref")


class RevisionUnavailable(ValueError):
    """The requested revision does not exist or is not ready for answers."""


def revision_label(row: dict[str, Any], requested: str) -> dict[str, Any]:
    """What the chat shows about the revision an answer came from."""
    return {"project_id": row.get("project_id"), "revision_id": row.get("revision_id"), "source_kind": row.get("source_kind"),
            "commit_sha": row.get("commit_sha"), "base_commit_sha": row.get("base_commit_sha"),
            "created_at": row.get("created_at"), "requested": "latest" if requested == "latest" else "pinned"}


def resolve_revision(body: dict[str, Any], registry: Any = None) -> dict[str, Any]:
    """Return the body bound to one ready revision; a body without ``revision`` is returned unchanged."""
    requested = body.get("revision")
    if requested is None:
        return body
    from telemetry.snapshot import validate_project_id, validate_revision_id

    project_id = body.get("project_id")
    try:
        validate_project_id(project_id if isinstance(project_id, str) else "")
    except ValueError as exc:
        raise RevisionUnavailable("A project is required to answer from a revision.") from exc
    if registry is None:
        from telemetry.registry import default_registry
        registry = default_registry()
    if requested == "latest":
        row = registry.latest_ready(project_id)
        if row is None:
            raise RevisionUnavailable("This project has no indexed revision yet. Index it in Knowledge Base.")
    else:
        try:
            validate_revision_id(requested if isinstance(requested, str) else "")
        except ValueError as exc:
            raise RevisionUnavailable("Invalid revision.") from exc
        row = registry.get_by_revision_id(project_id, requested)
        if row is None:
            raise RevisionUnavailable("This revision does not exist for the project.")
    if row.get("state") != "ready" or not row.get("collection_name"):
        raise RevisionUnavailable("This revision is not indexed and ready yet.")
    from index.embedding_rebuild import active_vector_collection

    if active_vector_collection(row["collection_name"]) is None:
        raise RevisionUnavailable("This revision needs a Cohere embedding rebuild before it can answer.")
    resolved = {key: value for key, value in body.items() if key not in _REPLACED_FIELDS}
    resolved.update(project_id=project_id, revision_id=row["revision_id"], commit_sha=row.get("commit_sha"),
                    collection=row["collection_name"], resolved_revision=revision_label(row, requested))
    return resolved
