"""Who owns a piece of stored data. Pure functions over a registry snapshot, so they are cheap to test.

Owner states:
- ``revision``: belongs to one registered revision; removed only through revision or project removal.
- ``project``: belongs to a registered project but no single revision.
- ``shared``: holds rows of many projects (or global data); project purge filters it by project id.
- ``legacy``: written by an older, filename-keyed flow that no registry row tracks.
- ``orphan``: names a revision or project the registry no longer knows.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

SHARED_COLLECTIONS = ("domain_concepts", "route_feedback", "runtime_exceptions")
LEGACY_PREFIXES = ("chunk_code_", "proj_knowledge_", "sym_")
_GENERATION = re.compile(r"^(?P<base>.+?)_cohere_(?:v5|[0-9a-f]{32})$")


@dataclass
class Owners:
    """Registry view: collection/archive/checksum -> owning project and revision."""

    projects: set[str] = field(default_factory=set)
    revisions: dict[str, str] = field(default_factory=dict)  # revision_id -> project_id
    collections: dict[str, tuple[str, str]] = field(default_factory=dict)  # snapshot collection -> (project, revision)
    archive_keys: dict[str, tuple[str, str]] = field(default_factory=dict)
    checksums: set[str] = field(default_factory=set)
    rule_collections: dict[str, str] = field(default_factory=dict)  # business_rules_<sha1> -> project


def load_owners(registry: Any) -> Owners:
    """Build the ownership view from every snapshot row, whatever its state."""
    from code_quality.rule_search import collection_name as rule_collection
    from telemetry.snapshot import Snapshot

    owners = Owners()
    with registry._connect() as db:
        rows = db.execute("SELECT project_id, revision_id, metadata, archive_key, checksum FROM snapshots").fetchall()
    import json

    for row in rows:
        project_id, revision_id = row["project_id"], row["revision_id"]
        owners.projects.add(project_id)
        owners.revisions[revision_id] = project_id
        if row["checksum"]:
            owners.checksums.add(row["checksum"])
        if row["archive_key"]:
            owners.archive_keys[row["archive_key"]] = (project_id, revision_id)
        try:
            metadata = json.loads(row["metadata"] or "{}")
            snapshot = Snapshot.from_row(project_id, {**metadata, "source_archive_sha256": metadata.get("source_archive_sha256") or row["checksum"]})
            owners.collections[snapshot.collection] = (project_id, revision_id)
        except (KeyError, ValueError, TypeError):
            continue
    for project_id in owners.projects:
        owners.rule_collections[rule_collection(project_id)] = project_id
    return owners


def classify_collection(name: str, owners: Owners) -> dict[str, Any]:
    """Owner of a Qdrant collection or a Neo4j ``collection`` property value."""
    base = name[4:] if name.startswith("sym_snapshot_") else name
    generation = _GENERATION.match(base)
    if generation and not base.startswith(SHARED_COLLECTIONS):
        base = generation.group("base")
    if base in owners.collections:
        project_id, revision_id = owners.collections[base]
        return _owner("revision", project_id, revision_id, "Indexed data of a registered revision.")
    if name in owners.rule_collections:
        return _owner("project", owners.rule_collections[name], None, "Business-rule index copy of a project.")
    if name.startswith(SHARED_COLLECTIONS):
        return _owner("shared", None, None, "Shared collection; points carry a project id.")
    if name.startswith("snapshot_") or name.startswith("sym_snapshot_"):
        return _owner("orphan", None, None, "Revision collection whose revision is no longer registered.")
    if name.startswith("business_rules_"):
        return _owner("orphan", None, None, "Business-rule index of a project with no registered revisions.")
    if name.startswith(LEGACY_PREFIXES):
        return _owner("legacy", None, None, "Filename-keyed data from the legacy upload flow.")
    return _owner("legacy", None, None, "Collection not created by the revision pipeline.")


def classify_revision_ref(project_id: str | None, revision_id: str | None, owners: Owners) -> dict[str, Any]:
    """Owner of data tagged with an explicit project id and optional revision id."""
    if not project_id:
        return _owner("legacy", None, None, "Untagged data from the legacy upload flow.")
    if revision_id:
        if owners.revisions.get(revision_id) == project_id:
            return _owner("revision", project_id, revision_id, "Tagged with a registered revision.")
        return _owner("orphan", project_id, revision_id, "Tagged with a revision that is no longer registered.")
    if project_id in owners.projects:
        return _owner("project", project_id, None, "Tagged with a registered project.")
    return _owner("orphan", project_id, None, "Tagged with a project that has no registered revisions.")


def _owner(state: str, project_id: str | None, revision_id: str | None, reason: str) -> dict[str, Any]:
    return {"owner": state, "project_id": project_id, "revision_id": revision_id, "reason": reason}
