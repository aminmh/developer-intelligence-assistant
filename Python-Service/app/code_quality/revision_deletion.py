"""Remove the data owned by one immutable source revision."""
from __future__ import annotations

import fcntl
import re
import threading
from urllib.error import HTTPError, URLError

from telemetry.registry import default_registry
from telemetry.snapshot import Snapshot, source_reference


class RevisionBusy(RuntimeError):
    pass


class RevisionStorageUnavailable(RuntimeError):
    pass


def remove_revision(project_id: str, revision_id: str) -> bool:
    from code_quality.service import ReportStore
    from index.embedding_rebuild import _locks, _locks_guard
    from index.llamaindex_layer import CodePropertyGraph
    from index.storage import RAG_UPLOADS_PREFIX, storage
    from qdrant_client import models

    registry = default_registry()
    item = registry.get_by_revision_id(project_id, revision_id)
    if item is None:
        return False
    reference = source_reference(item)
    snapshot = Snapshot(project_id, item.get("commit_sha"), item["source_archive_sha256"])
    try:
        with registry.lock(project_id, reference, blocking=False):
            with (registry.root / "index-worker.lock").open("a") as worker_lock:
                try:
                    fcntl.flock(worker_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise RevisionBusy("An indexing job is running. Retry when it completes.") from exc
                with _locks_guard:
                    rebuild_lock = _locks.setdefault(snapshot.collection, threading.Lock())
                if not rebuild_lock.acquire(blocking=False):
                    raise RevisionBusy("An embedding rebuild is running. Retry when it completes.")
                try:
                    current = registry.get_by_revision_id(project_id, revision_id)
                    if current is None:
                        return False
                    if registry.has_deployment(project_id, reference):
                        raise RevisionBusy("Stop the running deployment for this revision before removing it.")
                    with registry._connect() as db:
                        db.execute("UPDATE snapshots SET state='deleting', updated_at=CURRENT_TIMESTAMP WHERE project_id=? AND revision_id=?", (project_id, revision_id))
                        db.execute("DELETE FROM index_jobs WHERE project_id=? AND commit_sha=?", (project_id, reference))

                    graph = CodePropertyGraph()
                    qdrant = graph._get_qdrant()
                    names = [entry.name for entry in qdrant.get_collections().collections]
                    vector_filter = models.Filter(must=[
                        models.FieldCondition(key="project_id", match=models.MatchValue(value=project_id)),
                        models.FieldCondition(key="revision_id", match=models.MatchValue(value=revision_id)),
                    ])
                    for name in names:
                        if name == snapshot.collection or re.fullmatch(re.escape(snapshot.collection) + r"_cohere_[0-9a-f]{32}", name):
                            qdrant.delete_collection(name)
                        else:
                            qdrant.delete(collection_name=name, points_selector=models.FilterSelector(filter=vector_filter), wait=True)

                    store = graph._get_graph_store()
                    if store is None:
                        raise RuntimeError("Neo4j is unavailable")
                    store.structured_query(
                        "MATCH (n) WHERE n.collection IN $collections OR (n.project_id = $project AND n.revision_id = $revision) DETACH DELETE n",
                        param_map={"collections": [snapshot.collection, "sym_" + snapshot.collection], "project": project_id, "revision": revision_id},
                    )

                    try:
                        _remove_runtime(project_id, revision_id)
                    except HTTPError as exc:
                        raise RevisionStorageUnavailable(
                            f"ClickHouse rejected runtime cleanup (HTTP {exc.code}). Check its health, credentials and permissions, then retry removal."
                        ) from exc
                    except (URLError, TimeoutError, OSError) as exc:
                        raise RevisionStorageUnavailable(
                            "ClickHouse is unavailable. Start or reconnect the configured runtime storage, then retry removal."
                        ) from exc

                    client = storage._get_client()
                    for obj in client.list_objects(storage.tus_bucket, prefix=RAG_UPLOADS_PREFIX + "/", recursive=True):
                        tags = dict(client.get_object_tags(storage.tus_bucket, obj.object_name) or {})
                        same_source = tags.get("source-ref") == reference or tags.get("commit-sha") == reference
                        if (tags.get("project-id") == project_id
                                and (tags.get("revision-id") == revision_id
                                     or (not tags.get("revision-id") and same_source
                                         and tags.get("archive-sha256") == snapshot.source_archive_sha256))):
                            client.remove_object(storage.tus_bucket, obj.object_name)
                    archive_key = registry.archive_key(project_id, reference)
                    if archive_key:
                        storage.delete_snapshot_archive(archive_key)
                    report_store = ReportStore()
                    with report_store._connect() as db:
                        db.execute("DELETE FROM assessment_reports WHERE project_id=? AND revision_id=?", (project_id, revision_id))
                    saved = registry.get_runtime_setting("cohere_embedding_generations")
                    if saved and isinstance(saved.get("value"), dict):
                        state = saved["value"]
                        for key in ("active", "retained"):
                            if isinstance(state.get(key), dict):
                                state[key].pop(snapshot.collection, None)
                        registry.set_runtime_setting("cohere_embedding_generations", state)
                    with registry._connect() as db:
                        db.execute("UPDATE git_sources SET last_revision_id=NULL,last_commit_sha=NULL WHERE project_id=? AND last_revision_id=?", (project_id, revision_id))
                        db.execute("DELETE FROM deployments WHERE project_id=? AND commit_sha=? AND state!='running'", (project_id, reference))
                        db.execute("DELETE FROM snapshots WHERE project_id=? AND revision_id=? AND state='deleting'", (project_id, revision_id))
                    return True
                finally:
                    rebuild_lock.release()
    except BlockingIOError as exc:
        raise RevisionBusy("This revision is being indexed or assessed. Retry when it completes.") from exc


def _remove_runtime(project_id: str, revision_id: str) -> None:
    from telemetry.runtime import ClickHouseRuntime

    runtime = ClickHouseRuntime()
    if not runtime.configured:
        return
    from telemetry.rollups import ROLLUP_TABLE

    names = {runtime.table, runtime.trace_table, runtime.profile_table, "unresolved_symbols", ROLLUP_TABLE}
    parameters = {"mutations_sync": "1", "param_project_id": project_id, "param_revision_id": revision_id}
    for name in names:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", name):
            raise ValueError("Invalid runtime table configuration")
        columns = runtime.rows(
            "SELECT name FROM system.columns WHERE database={database:String} AND table={table_name:String}",
            project_id, revision_id, {"param_database": runtime.database, "param_table_name": name},
        )
        fields = {row["name"] for row in columns}
        if not fields:
            continue
        if {"project_id", "revision_id"}.issubset(fields):
            scope = "project_id={project_id:String} AND revision_id={revision_id:String}"
        elif "ResourceAttributes" in fields:
            scope = "ResourceAttributes['indexer.project_id']={project_id:String} AND ResourceAttributes['indexer.revision_id']={revision_id:String}"
        else:
            raise RuntimeError("A runtime table cannot be scoped to the selected revision")
        runtime.execute(f"ALTER TABLE `{runtime.database}`.`{name}` DELETE WHERE {scope}", extra_parameters=parameters, timeout=120)
