"""Application service for background PHP chunk indexing jobs."""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

from infra.php_tools import extract_zip_safely, run_code_parse
from infra.settings import settings

logger = logging.getLogger("download_jobs")


class IndexingService:
    """Own status-file updates, PHP parsing, and graph/vector ingestion."""

    def write_status(
        self,
        download_id: str,
        status: str,
        total: int,
        indexed: int,
        collection_name: str = "",
        symbol_index: dict[str, Any] | None = None,
        error: str | None = None,
        graph_nodes: int = 0,
        graph_relations: int = 0,
        graph_index_complete: bool = False,
        graph_status: str = "pending",
    ) -> None:
        try:
            status_path = Path(settings.download_dir) / f"{download_id}.status.json"
            status_path.write_text(json.dumps({
                "status": status,
                "total": total,
                "indexed": indexed,
                "collection_name": collection_name,
                "symbol_index": symbol_index,
                "error": error,
                "graph_nodes": graph_nodes,
                "graph_relations": graph_relations,
                "graph_index_complete": graph_index_complete,
                "graph_status": graph_status,
            }))
        except Exception:  # noqa: BLE001 — progress is best-effort
            pass

    def index_chunks_background(
        self,
        download_id: str,
        filename: str,
        chunks: list[dict[str, Any]],
        collection_name: str,
        symbol_definitions: list[dict[str, Any]] | None = None,
        symbol_references: list[dict[str, Any]] | None = None,
        workspace_path: str | None = None,
        snapshot: Any = None,
        vector_collection_name: str | None = None,
        force_reindex: bool = False,
    ) -> None:
        logger.info("Background indexing started: collection=%s count=%s", collection_name, len(chunks))
        self.write_status(download_id, "embedding", len(chunks), 0, collection_name)
        try:
            from index.llamaindex_layer import CodePropertyGraph

            def report_progress(stage: str, completed: int, total: int) -> None:
                self.write_status(
                    download_id, stage, total, completed, collection_name,
                )

            def ingest_snapshot() -> dict[str, Any]:
                self.write_status(
                    download_id, "graph_indexing", len(chunks), 0,
                    collection_name, graph_status="checking",
                )
                ingest_kwargs: dict[str, Any] = {}
                if symbol_definitions or symbol_references:
                    ingest_kwargs = {
                        "symbol_definitions": symbol_definitions,
                        "symbol_references": symbol_references,
                    }
                if force_reindex:
                    ingest_kwargs["force_reindex"] = True
                result = CodePropertyGraph().ingest(
                    chunks, collection_name, snapshot=snapshot,
                    progress_callback=report_progress,
                    vector_collection_name=vector_collection_name,
                    **ingest_kwargs,
                )
                result["symbol_index"] = self._index_symbol_graph(
                    workspace_path,
                    filename,
                    symbol_definitions,
                    symbol_references,
                    snapshot,
                )
                return result

            if snapshot is not None and not vector_collection_name:
                from telemetry.source import index_uploaded_snapshot
                result = index_uploaded_snapshot(
                    snapshot,
                    ingest_snapshot,
                    force_reindex=force_reindex,
                )
                if force_reindex and result.get("already_indexed"):
                    raise RuntimeError("Forced snapshot reindex was skipped by the registry")
            elif snapshot is not None:
                from telemetry.registry import default_registry
                registry = default_registry()
                with registry.lock(snapshot.project_id, snapshot.reference):
                    current = registry.get(snapshot.project_id, snapshot.reference)
                    if (not current.get("revision_id") or current.get("state") == "deleting"
                            or (snapshot.revision_id and current.get("revision_id") != snapshot.revision_id)):
                        raise RuntimeError("The uploaded revision is no longer available for indexing")
                    result = ingest_snapshot()
                    if current.get("state") != "ready":
                        registry.transition(snapshot, "indexing")
                        registry.finish_job(snapshot.project_id, snapshot.reference, success=True)
                    from index.embedding_rebuild import activate_generation
                    activate_generation(collection_name, vector_collection_name)
            else:
                result = ingest_snapshot()
            symbol_index = result.get("symbol_index")
            ingested = int(result.get("ingested", 0) or 0)
            logger.info(
                "Background indexing complete: collection=%s ingested=%s graph_nodes=%s relations=%s",
                collection_name, ingested, result.get("graph_nodes", 0), result.get("graph_relations", 0),
            )
            self.write_status(
                download_id,
                "done",
                len(chunks),
                ingested,
                collection_name,
                symbol_index,
                graph_nodes=int(result.get("graph_nodes", 0) or 0),
                graph_relations=int(result.get("graph_relations", 0) or 0),
                graph_index_complete=(
                    int(result.get("graph_nodes", 0) or 0)
                    == int(result.get("chunk_count", len(chunks)) or 0)
                    and int(result.get("chunk_count", len(chunks)) or 0) > 0
                ),
                graph_status="ready",
            )
        except Exception as exc:  # noqa: BLE001 — job failure is reported in status
            logger.error("Background indexing failed for '%s': %s", collection_name, exc, exc_info=True)
            from index.embeddings import EmbeddingProviderError
            error = (
                str(exc) if isinstance(exc, EmbeddingProviderError)
                else f"Project indexing failed: {type(exc).__name__}. Check Neo4j/Qdrant readiness and retry."
            )
            self.write_status(
                download_id,
                "error",
                len(chunks),
                0,
                collection_name,
                error=error,
                graph_status="unverified",
            )

    def parse_and_index(
        self, download_id: str, filename: str, zip_data: bytes, collection_name: str,
        identity: dict[str, str] | None = None,
        vector_collection_name: str | None = None,
        force_reindex: bool = False,
    ) -> None:
        workdir = Path(settings.download_dir) / f"parse_{download_id}"
        try:
            self.write_status(download_id, "embedding", 0, 0, collection_name)
            file_count = extract_zip_safely(zip_data, str(workdir))
            logger.info("Parse extracted %s files: %s", file_count, collection_name)
            chunks, index, parse_error = self._parse_source(str(workdir), filename)
            snapshot = None
            if identity:
                from telemetry.snapshot import Snapshot
                from telemetry.symbols import from_chunk

                snapshot = Snapshot(
                    identity["project_id"], identity["commit_sha"], identity["source_archive_sha256"],
                    revision_id=identity.get("revision_id"),
                )
                for chunk in chunks:
                    chunk.update(snapshot.metadata())
                    chunk["symbol_id"] = from_chunk(chunk)
            if chunks and identity:
                self._store_repo_map(identity.get("source_archive_sha256"), chunks)
            if not chunks:
                logger.warning("No chunks produced: %s (%s)", collection_name, parse_error)
                self.write_status(
                    download_id, "error", 0, 0, collection_name,
                    error=parse_error or (
                        "No indexable source was found. Upload a ZIP containing project source "
                        "outside dependency and generated directories."),
                )
                return
            definitions = index.get("definitions", [])
            references = index.get("references", [])
            if definitions or references:
                self.index_chunks_background(
                    download_id,
                    filename,
                    chunks,
                    collection_name,
                    symbol_definitions=definitions,
                    symbol_references=references,
                    workspace_path=str(workdir),
                    snapshot=snapshot,
                    vector_collection_name=vector_collection_name,
                    force_reindex=force_reindex,
                )
            else:
                self.index_chunks_background(
                    download_id, filename, chunks, collection_name,
                    snapshot=snapshot,
                    vector_collection_name=vector_collection_name,
                    force_reindex=force_reindex,
                )
        except Exception as exc:  # noqa: BLE001 — job failure is reported in status
            logger.error("Parse failed for '%s': %s", collection_name, exc, exc_info=True)
            from infra.php_cli import PhpCliError
            error = (
                f"PHP source parsing failed: {str(exc)[:300]}"
                if isinstance(exc, PhpCliError)
                else f"Project parsing failed: {type(exc).__name__}. Check the API download-job logs and retry."
            )
            self.write_status(download_id, "error", 0, 0, collection_name, error=error)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    @staticmethod
    def _store_repo_map(archive_sha256: str | None, chunks: list[dict[str, Any]]) -> None:
        """Rank this revision's files and symbols for ``code_repo_map``. Best-effort."""
        try:
            from agent_tools.repo_map import build_repo_map, store_repo_map

            store_repo_map(archive_sha256, build_repo_map(chunks))
        except Exception as exc:  # noqa: BLE001 — the map is rebuilt from source on first use
            logger.warning("Repository map not stored: %s", exc)

    @staticmethod
    def _parse_source(workdir: str, filename: str) -> tuple[list[dict[str, Any]], dict[str, Any], str | None]:
        """Chunks, symbol export, and an error message for the project's primary language.

        Projects with any PHP keep the PHP CLI parser and inspector. Go, Java, C#, Ruby, and Rust use the
        Tree-sitter producer, which also returns the symbol export; Python and JS/TS
        index chunks without a symbol graph.
        """
        from projects.structure_profile import language_census

        census = language_census(workdir)
        primary = census.get("primary")
        has_php = any(lang.get("language") == "php" and lang.get("files") for lang in census.get("languages") or [])
        # Any PHP keeps the PHP parser, as before: a Laravel app with a large frontend stays PHP.
        if primary is None or has_php:
            chunks = run_code_parse(workdir)
            try:
                from inspection.cli import inspect_index

                index = inspect_index(workdir)
            except Exception as exc:  # noqa: BLE001 — symbol graph is best-effort
                logger.warning("Symbol export failed for %s: %s", filename, exc)
                index = {"definitions": [], "references": []}
            error = None if chunks else (
                "No indexable PHP source was found. Upload a ZIP containing project PHP source "
                "outside dependency and generated directories.")
            return chunks, index, error
        from projects.lang_index import produce_chunks

        result = produce_chunks(workdir, primary)
        index = result.get("symbol_index") or {"definitions": [], "references": []}
        logger.info("Indexed %s with %s: %s chunks, coverage %s", filename, result.get("producer"),
                    len(result.get("chunks") or []), result.get("coverage"))
        error = result.get("error") and f"Project indexing failed for {primary}: {result['error']}"
        return list(result.get("chunks") or []), index, error

    def _index_symbol_graph(
        self,
        workspace_path: str | None,
        filename: str,
        definitions: list[dict[str, Any]] | None,
        references: list[dict[str, Any]] | None,
        snapshot: Any = None,
    ) -> dict[str, Any]:
        """Best-effort precise symbol graph indexing alongside vectors."""
        if not workspace_path or not definitions and not references:
            return {"indexed": False, "definitions": len(definitions or []), "references": len(references or [])}
        try:
            from inspection.service import inspection_service

            stem = Path(filename).stem or "unnamed"
            return inspection_service().index(
                workspace_path,
                stem,
                exported={
                    "definitions": definitions or [],
                    "references": references or [],
                },
                snapshot=snapshot,
            )
        except Exception as exc:  # noqa: BLE001 — symbol graph is best-effort
            if snapshot is not None:
                raise
            logger.warning("Symbol graph indexing failed for %s: %s", filename, exc)
            return {
                "indexed": False,
                "definitions": len(definitions or []),
                "references": len(references or []),
                "error": str(exc)[:300],
            }


indexing_service = IndexingService()
