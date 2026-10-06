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
    ) -> None:
        logger.info("Background indexing started: collection=%s count=%s", collection_name, len(chunks))
        self.write_status(download_id, "embedding", len(chunks), 0, collection_name)
        try:
            from index.llamaindex_layer import CodePropertyGraph

            def report_progress(stage: str, completed: int, total: int) -> None:
                self.write_status(
                    download_id, stage, total, completed, collection_name
                )

            def ingest_snapshot() -> dict[str, Any]:
                ingest_kwargs: dict[str, Any] = {}
                if symbol_definitions or symbol_references:
                    ingest_kwargs = {
                        "symbol_definitions": symbol_definitions,
                        "symbol_references": symbol_references,
                    }
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
                result = index_uploaded_snapshot(snapshot, ingest_snapshot)
            else:
                result = ingest_snapshot()
            if snapshot is not None and vector_collection_name:
                from telemetry.registry import default_registry
                registry = default_registry()
                current = registry.get(snapshot.project_id, snapshot.commit_sha)
                if current.get("state") != "ready":
                    registry.register(snapshot)
                    registry.transition(snapshot, "indexing")
                    registry.finish_job(snapshot.project_id, snapshot.commit_sha, success=True)
                from index.embedding_rebuild import activate_generation
                activate_generation(collection_name, vector_collection_name)
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
            )
        except Exception as exc:  # noqa: BLE001 — job failure is reported in status
            logger.error("Background indexing failed for '%s': %s", collection_name, exc, exc_info=True)
            self.write_status(download_id, "error", len(chunks), 0, collection_name, error="Embedding failed. Check API logs and retry.")

    def parse_and_index(
        self, download_id: str, filename: str, zip_data: bytes, collection_name: str,
        identity: dict[str, str] | None = None,
        vector_collection_name: str | None = None,
    ) -> None:
        workdir = Path(settings.download_dir) / f"parse_{download_id}"
        try:
            self.write_status(download_id, "embedding", 0, 0, collection_name)
            file_count = extract_zip_safely(zip_data, str(workdir))
            logger.info("Parse extracted %s files: %s", file_count, collection_name)
            chunks = run_code_parse(str(workdir))
            snapshot = None
            if identity:
                from telemetry.snapshot import Snapshot
                from telemetry.symbols import from_chunk

                snapshot = Snapshot(
                    identity["project_id"], identity["commit_sha"], identity["source_archive_sha256"]
                )
                for chunk in chunks:
                    chunk.update(snapshot.metadata())
                    chunk["symbol_id"] = from_chunk(chunk)
            try:
                from inspection.cli import inspect_index

                index = inspect_index(str(workdir))
            except Exception as exc:  # noqa: BLE001 — symbol graph is best-effort
                logger.warning("Symbol export failed for %s: %s", filename, exc)
                index = {"definitions": [], "references": []}
            if not chunks:
                logger.warning("No PHP chunks produced: %s", collection_name)
                self.write_status(download_id, "error", 0, 0, collection_name)
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
                )
            else:
                self.index_chunks_background(
                    download_id, filename, chunks, collection_name,
                    snapshot=snapshot, vector_collection_name=vector_collection_name,
                )
        except Exception as exc:  # noqa: BLE001 — job failure is reported in status
            logger.error("Parse failed for '%s': %s", collection_name, exc, exc_info=True)
            self.write_status(download_id, "error", 0, 0, collection_name)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

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
