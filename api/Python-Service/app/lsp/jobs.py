"""LSP indexing jobs for one revision: on the analyzer worker or in the API process.

Where it runs (external-analyzer spec 32a-32c; decision in
@.archcore/agent-tools/language-servers-feed-the-symbol-graph-at-index-time.adr.md):
the analyzer worker when the project has one with source-transfer consent, else the
API process; when both fail, the parser's export stands.

`run_lsp` is the worker's entry point (it extracts a source bundle and indexes it);
`index_directory` indexes an already-extracted tree; `merge_into` combines an LSP
export with the parser (and SCIP) export that snapshot indexing already built.
"""

from __future__ import annotations

import io
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

ORIGIN = "lsp"


def _registry(registry: Any = None) -> Any:
    if registry is not None:
        return registry
    from lsp import registry as live

    return live


def language_extensions(registry: Any = None) -> Dict[str, List[str]]:
    """Index-enabled languages and the file suffixes their adapters read."""
    reg = _registry(registry)
    found: Dict[str, List[str]] = {}
    for language in reg.index_languages():
        try:
            extensions = list(getattr(reg.adapter(language), "extensions", []) or [])
        except Exception:  # noqa: BLE001 - a broken adapter is reported by index_workspace
            extensions = []
        if extensions:
            found[language] = [e.lower() for e in extensions]
    return found


def index_directory(root: str, languages: Optional[List[str]] = None, registry: Any = None) -> Dict[str, Any]:
    """Index an extracted tree for every index-enabled language. Never raises.

    Returns ``{name, origin, status, languages: {lang: {status, reason?, ...}},
    definitions, references, truncated}``; status is complete, partial or unavailable.
    """
    reg = _registry(registry)
    wanted = languages if languages is not None else reg.index_languages()
    result: Dict[str, Any] = {"name": "lsp", "origin": ORIGIN, "languages": {}, "definitions": [],
                              "references": [], "truncated": False}
    for language in wanted:
        try:
            out = reg.index_workspace(language, root)
        except Exception as exc:  # noqa: BLE001 - index_workspace should not raise; stay safe
            out = {"status": "unavailable", "reason": f"LSP indexing failed: {exc}"[:300],
                   "definitions": [], "references": []}
        entry = {"status": out.get("status", "unavailable")}
        if out.get("reason"):
            entry["reason"] = str(out["reason"])[:300]
        if out.get("limits_reached"):
            entry["limits_reached"] = list(out["limits_reached"])[:10]
        entry["definitions"] = len(out.get("definitions") or [])
        entry["references"] = len(out.get("references") or [])
        result["languages"][language] = entry
        result["definitions"] += out.get("definitions") or []
        result["references"] += out.get("references") or []
        result["truncated"] = result["truncated"] or bool(out.get("truncated"))
    result["status"] = overall_status(result["languages"])
    return result


def overall_status(languages: Dict[str, Dict[str, Any]]) -> str:
    statuses = {entry.get("status") for entry in languages.values()}
    if not statuses or statuses == {"unavailable"}:
        return "unavailable"
    return "complete" if statuses == {"complete"} else "partial"


def run_lsp(archive_data: bytes, files: List[str], languages: Dict[str, int], registry: Any = None) -> Dict[str, Any]:
    """Worker entry point: extract the selected revision files and index them. Never raises."""
    from code_quality.service import MAX_FILE_BYTES, _safe_archive_path

    selected = set(files)
    wanted = [lang for lang, count in languages.items() if count and lang in language_extensions(registry)]
    if not wanted:
        return {"name": "lsp", "origin": ORIGIN, "status": "unavailable", "languages": {},
                "definitions": [], "references": [], "truncated": False,
                "reason": "No requested language has LSP indexing enabled on this worker."}
    with tempfile.TemporaryDirectory(prefix="lsp-source-") as root:
        try:
            with zipfile.ZipFile(io.BytesIO(archive_data)) as archive:
                for member in archive.infolist():
                    relative = _safe_archive_path(member.filename)
                    if relative not in selected or member.is_dir() or member.file_size > MAX_FILE_BYTES:
                        continue
                    target = Path(root, relative)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.read(member))
        except (zipfile.BadZipFile, OSError, ValueError) as exc:
            return {"name": "lsp", "origin": ORIGIN, "status": "unavailable", "languages": {},
                    "definitions": [], "references": [], "truncated": False,
                    "reason": f"Source archive could not be read: {exc}"[:300]}
        return index_directory(root, wanted, registry)


def record_producer(index: Dict[str, Any], name: str, export: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Note a precise producer's outcome on the export so the graph keeps it (change impact F8).

    ``producers[name] = {status, languages}``; the input export is left unchanged.
    """
    if not export:
        return index
    languages = export.get("languages") or {}
    names = sorted(languages) if isinstance(languages, (dict, list, tuple, set)) else []
    producers = {**(index.get("producers") or {}),
                 name: {"status": str(export.get("status") or "unavailable"), "languages": names}}
    return {**index, "producers": producers}


def merge_into(index: Dict[str, Any], export: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Combine an LSP export with the parser (and SCIP) export.

    Definitions: LSP's, plus earlier ones LSP does not have. References: when LSP
    covered its languages completely, it replaces the PHP parser's references (the
    Phase 0/1 checks found every linked parser edge among LSP's); SCIP and other
    producers' references stay. On partial coverage both are kept, LSP last, so the
    graph writer stores LSP's origin on edges both report.
    """
    if not export or export.get("status") not in ("complete", "partial") or not export.get("definitions"):
        return index
    lsp_definitions = list(export.get("definitions") or [])
    lsp_keys = {(d.get("kind"), d.get("fqn")) for d in lsp_definitions}
    earlier = [d for d in index.get("definitions") or [] if (d.get("kind"), d.get("fqn")) not in lsp_keys]
    references = list(index.get("references") or [])
    if export.get("status") == "complete":
        references = [r for r in references if r.get("origin") != "php-parser"]
    return {**index, "definitions": [*earlier, *lsp_definitions],
            "references": [*references, *(export.get("references") or [])]}


def _workspace_zip(root: str, extensions: Dict[str, List[str]]) -> bytes:
    """The workspace's files for the LSP languages as ZIP bytes (worker transfer input)."""
    wanted = {suffix for suffixes in extensions.values() for suffix in suffixes}
    buffer = io.BytesIO()
    base = Path(root)
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(base.rglob("*")):
            if path.is_file() and not path.is_symlink() and path.suffix.lower() in wanted:
                bundle.write(path, path.relative_to(base).as_posix())
    return buffer.getvalue()


def _on_worker(zip_data: bytes, extensions: Dict[str, List[str]], identity: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The worker's LSP export, or None when the project has no consented worker or it failed."""
    import logging

    try:
        from code_quality.external_analyzers import ExternalAnalyzerSettingsStore, effective_worker_target
        from code_quality.remote_worker import lsp_inventory, run_lsp_job

        files, languages = lsp_inventory(zip_data, extensions)
        if not files:
            return None
        target = effective_worker_target(ExternalAnalyzerSettingsStore().get(identity["project_id"]))
        if not target:
            return None
        export = run_lsp_job(target, zip_data, files, languages, identity["revision_id"], identity.get("commit_sha"))
        export["where"] = "worker"
        return export
    except Exception as exc:  # noqa: BLE001 - fall back to the API process
        logging.getLogger(__name__).warning("LSP on the analyzer worker failed for %s; indexing in the API: %s",
                                            identity.get("project_id"), exc)
        return None


def enrich_export(index: Dict[str, Any], workdir: str, zip_data: Optional[bytes] = None,
                  identity: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Add language-server definitions and references to a symbol export. Best-effort, never raises.

    The consented analyzer worker first (needs ``identity`` with a revision id), then
    this process on ``workdir``; when both are unavailable the export stays as it was
    (external-analyzer spec 32a-32c). ``zip_data`` is the revision archive; without it
    the workspace is zipped for the worker.
    """
    import logging

    log = logging.getLogger(__name__)
    try:
        extensions = language_extensions()
        if not extensions:
            return index
        export = None
        if identity and identity.get("revision_id"):
            archive = zip_data if zip_data is not None else _workspace_zip(workdir, extensions)
            export = _on_worker(archive, extensions, identity)
        if export is None or export.get("status") == "unavailable":
            from projects.structure_profile import _SKIP_DIR_NAMES

            skip = {d.lower() for d in _SKIP_DIR_NAMES}
            present = {suffix for path in Path(workdir).rglob("*")
                       if path.is_file() and not skip.intersection(p.lower() for p in path.relative_to(workdir).parts)
                       for suffix in [path.suffix.lower()]}
            wanted = [lang for lang, suffixes in extensions.items() if present.intersection(suffixes)]
            if not wanted:
                return index
            export = index_directory(workdir, wanted)
            export["where"] = "api"
    except Exception as exc:  # noqa: BLE001 - precise references are an enhancement
        log.warning("LSP indexing skipped for %s: %s", (identity or {}).get("project_id"), exc)
        return index
    log.info("LSP (%s) %s: %s definitions, %s references for %s (%s)", export.get("where"), export.get("status"),
             len(export.get("definitions") or []), len(export.get("references") or []),
             (identity or {}).get("project_id"), export.get("languages"))
    return record_producer(merge_into(index, export), "lsp", export)
