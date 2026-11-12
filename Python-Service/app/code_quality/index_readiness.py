"""Check the selected revision's persisted index before assessment starts."""
from telemetry.snapshot import Snapshot, source_filter, source_reference


def assessment_index_readiness(project_id, revision_id, registry):
    snapshot = registry.get_by_revision_id(project_id, revision_id)
    result = {"ready": False, "status": "index_required",
              "reason": "Index this project revision in Knowledge Base → Files & indexing before running an assessment."}
    if snapshot is None:
        return result
    if snapshot.get("state") != "ready":
        if snapshot.get("state") in {"queued", "indexing", "storing"}:
            result.update(status="indexing", reason="This revision is still indexing. Wait for indexing to complete in Knowledge Base, then check again.")
        return result
    try:
        collection = Snapshot.from_row(project_id, snapshot).collection
    except (KeyError, ValueError, TypeError):
        return result
    if snapshot.get("collection_name") != collection:
        return result
    if not registry.archive_key(project_id, source_reference(snapshot)):
        return {**result, "status": "source_required",
                "reason": "The source archive is missing. Upload and index this revision again in Knowledge Base before assessment."}
    from index.llamaindex_layer import CodePropertyGraph
    from qdrant_client.models import FieldCondition, Filter, MatchValue

    identity = {"project_id": project_id, "revision_id": revision_id,
                **source_filter(snapshot), "source_archive_sha256": snapshot["source_archive_sha256"],
                "dirty": False, "collection": collection}
    try:
        from index.embedding_rebuild import active_vector_collection

        vector_collection = active_vector_collection(collection)
        rebuild_required = {**result, "status": "embedding_rebuild_required",
                            "reason": "This revision's active Cohere embeddings are missing or incomplete. Use Rebuild Cohere embeddings in Knowledge Base → Files & indexing, then check again."}
        if vector_collection is None:
            return rebuild_required
        graph = CodePropertyGraph()
        client = graph._get_qdrant()
        if not client.collection_exists(vector_collection):
            return rebuild_required if vector_collection != collection else result
        vector_identity = {**identity, "collection": vector_collection}
        conditions = Filter(must=[FieldCondition(key=key, match=MatchValue(value=value)) for key, value in vector_identity.items()])
        vector_count = client.count(collection_name=vector_collection, count_filter=conditions, exact=True).count
        if not vector_count:
            return rebuild_required if vector_collection != collection else result
        store = graph._get_graph_store()
        if store is None:
            raise RuntimeError("Graph store unavailable")
        rows = store.structured_query(
            "MATCH (node:CodeSymbol) WHERE node.collection = $collection "
            "AND node.project_id = $project_id AND node.revision_id = $revision_id "
            + ("AND node.commit_sha = $commit_sha " if snapshot.get("commit_sha") else "AND node.source_ref = $source_ref ") +
            "AND node.source_archive_sha256 = $source_archive_sha256 "
            "AND node.dirty = false RETURN count(node) AS count", param_map=identity,
        )
        graph_count = rows[0]["count"] if rows else 0
        if graph_count != vector_count:
            return {**result, "reason": "The revision's graph index is missing or incomplete. Re-index this revision in Knowledge Base before assessment."}
        return {"ready": True, "status": "ready", "reason": "This revision's source and index are available."}
    except Exception:
        return {**result, "status": "index_unavailable",
                "reason": "The project index could not be checked. Check Qdrant and Neo4j availability, then retry."}
