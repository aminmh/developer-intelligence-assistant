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
        self, download_id: str, status: str, total: int, indexed: int, collection_name: str = ""
    ) -> None:
        try:
            status_path = Path(settings.download_dir) / f"{download_id}.status.json"
            status_path.write_text(json.dumps({
                "status": status,
                "total": total,
                "indexed": indexed,
                "collection_name": collection_name,
            }))
        except Exception:  # noqa: BLE001 — progress is best-effort
            pass

    def index_chunks_background(
        self, download_id: str, filename: str, chunks: list[dict[str, Any]], collection_name: str
    ) -> None:
        logger.info("Background indexing started: collection=%s count=%s", collection_name, len(chunks))
        self.write_status(download_id, "embedding", len(chunks), 0, collection_name)
        try:
            from index.llamaindex_layer import CodePropertyGraph

            result = CodePropertyGraph().ingest(chunks, collection_name)
            ingested = int(result.get("ingested", 0) or 0)
            logger.info(
                "Background indexing complete: collection=%s ingested=%s graph_nodes=%s relations=%s",
                collection_name, ingested, result.get("graph_nodes", 0), result.get("graph_relations", 0),
            )
            self.write_status(download_id, "done", len(chunks), ingested, collection_name)
        except Exception as exc:  # noqa: BLE001 — job failure is reported in status
            logger.error("Background indexing failed for '%s': %s", collection_name, exc, exc_info=True)
            self.write_status(download_id, "error", len(chunks), 0, collection_name)

    def parse_and_index(
        self, download_id: str, filename: str, zip_data: bytes, collection_name: str
    ) -> None:
        workdir = Path(settings.download_dir) / f"parse_{download_id}"
        try:
            self.write_status(download_id, "embedding", 0, 0, collection_name)
            file_count = extract_zip_safely(zip_data, str(workdir))
            logger.info("Parse extracted %s files: %s", file_count, collection_name)
            chunks = run_code_parse(str(workdir))
            if not chunks:
                logger.warning("No PHP chunks produced: %s", collection_name)
                self.write_status(download_id, "error", 0, 0, collection_name)
                return
            self.index_chunks_background(download_id, filename, chunks, collection_name)
        except Exception as exc:  # noqa: BLE001 — job failure is reported in status
            logger.error("Parse failed for '%s': %s", collection_name, exc, exc_info=True)
            self.write_status(download_id, "error", 0, 0, collection_name)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


indexing_service = IndexingService()

