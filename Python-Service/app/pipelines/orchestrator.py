"""Hybrid orchestration with LlamaIndex routing.

This module keeps the existing job lifecycle, status persistence, and
post-processing pipeline, but adds a LlamaIndex-backed routing and synthesis
layer so orchestration can evolve without rewriting the runtime again. Code
tools (`chunk_code`, `export_structure`) are executed by the in-image pure-PHP
CLI instead of a remote MCP server.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.parse as parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import requests
import yaml
from infra.settings import settings
from infra.exception_logger import get_exception_logger

try:
    from llama_index.core import Settings
    Settings.llm = None  # No local LLM — server.ts handles final LLM call
except Exception:  # pragma: no cover
    Settings = None  # type: ignore[assignment]

try:
    from llama_index.core.prompts import PromptTemplate
except Exception:  # pragma: no cover
    PromptTemplate = None  # type: ignore[assignment]


logger = logging.getLogger(__name__)

DEFAULT_STATUS_DIR = Path(os.environ.get("DOWNLOAD_DIR", "/tmp/orchestrator"))
DEFAULT_PYTHON_API_URL = os.environ.get("PYTHON_API_URL", "http://python-api:9002")
DEFAULT_INTERNAL_SECRET = os.environ.get("INTERNAL_SECRET", "")


def project_collection_name(filename: str) -> str:
    """Per-project Qdrant collection name, e.g. 'Archive.zip' -> 'chunk_code_Archive'."""
    stem = Path(filename).stem
    name = re.sub(r"[^a-zA-Z0-9_]", "_", stem)
    name = re.sub(r"_+", "_", name).strip("_")
    return f"chunk_code_{name or 'unnamed'}"


@dataclass
class JobContext:
    job_id: str
    route_name: str
    filename: str
    prompt: str
    route: dict[str, Any]
    workspace_path: str | None = None
    skip_index: bool = False


class Orchestrator:
    def __init__(self, config_path: str):
        self.config_path = Path(config_path)
        self.config = self._load_config(self.config_path)
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._status_dir = Path(self._status_dir_from_config())
        self._status_dir.mkdir(parents=True, exist_ok=True)
        self._action_handlers: dict[str, Callable[..., Any]] = {
            "extract_json": self._act_extract_json,
            "send_to_qdrant": self._act_send_to_qdrant,
            "poll_embedding": self._act_poll_embedding,
            "wrap_code_block": self._act_wrap_code_block,
            "truncate": self._act_truncate,
            "llamaindex_route": self._act_llamaindex_route,
            "llamaindex_summarize": self._act_llamaindex_summarize,
            "llamaindex_ingest": self._act_llamaindex_ingest,
        }

    def _status_dir_from_config(self) -> str:
        return (
            self.config.get("status_dir")
            or self.config.get("download_dir")
            or str(DEFAULT_STATUS_DIR)
        )

    def _load_config(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        return data if isinstance(data, dict) else {}

    def _resolve_env(self, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        if value.startswith("${") and value.endswith("}"):
            return os.environ.get(value[2:-1], "")
        return value

    def get_route(self, route_name: str) -> dict[str, Any] | None:
        routes = self.config.get("routes", {})
        route = routes.get(route_name)
        return route if isinstance(route, dict) else None

    def start_job(
        self,
        job_id: str,
        route_name: str,
        filename: str,
        prompt: str,
        skip_index: bool = False,
        entities: dict[str, Any] | None = None,
    ) -> None:
        route = self.get_route(route_name) or {"tool": "auto", "prompt_template": "chat"}
        steps = self._build_steps(route)
        job = {
            "job_id": job_id,
            "route_name": route_name,
            "filename": filename,
            "prompt": prompt,
            "route": route,
            "status": "starting",
            "steps": steps,
            "started_at": time.time(),
            "updated_at": time.time(),
            "skip_index": skip_index,
            "entities": entities or {},
        }
        with self._lock:
            self._jobs[job_id] = job
        self._write_status(job_id)
        thread = threading.Thread(
            target=self._run_job,
            args=(JobContext(job_id, route_name, filename, prompt, route, skip_index),),
            daemon=True,
        )
        thread.start()

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._jobs.get(job_id)

    def _build_steps(self, route: dict[str, Any]) -> list[dict[str, Any]]:
        # UI plan listing (conditions/labels identical to historical output);
        # actual execution order lives in pipelines.steps.STEPS.
        from pipelines.steps import build_steps

        return build_steps(route)

    def _run_job(self, ctx: JobContext) -> None:
        workspace_path: str | None = None
        try:
            # The pipeline is an explicit step plan (pipelines.steps.STEPS) —
            # this method only executes the plan against a fresh shared state.
            # Each step owns its progress markers, guards and artifact writes.
            from pipelines.steps import SharedState, run_plan

            shared = run_plan(self, ctx, SharedState())
            workspace_path = shared.workspace_path
        except Exception as exc:
            self._save_error(ctx.job_id, str(exc))
            get_exception_logger().error(
                "Orchestration job %s failed (route=%s): %s",
                ctx.job_id,
                ctx.route_name,
                exc,
                exc_info=exc,
            )
        finally:
            # Remove the extracted workspace footprint. The extracted files are
            # only needed while the PHP tool + doc retrieval run; the final LLM
            # answer reads from the Qdrant collection, never from this dir.
            # Set WORKSPACE_KEEP_EXTRACTED=1 to retain for debugging.
            keep = os.environ.get("WORKSPACE_KEEP_EXTRACTED", "").strip().lower() in ("1", "true", "yes")
            # F17: sandbox routes retain their workspace (bounded LRU) so the
            # /api/inspect Tier-2/3 file reads keep working after the job.
            # Code-index routes opt in via retain_workspace so a later
            # POST /analysis persist can re-produce + ingest the chunks.
            route_retain = bool((ctx.route or {}).get("sandbox")) or bool(
                (ctx.route or {}).get("retain_workspace")
            )
            retain = keep or route_retain
            if retain and workspace_path and os.path.isdir(workspace_path):
                try:
                    from projects.workspace_registry import register

                    register(workspace_path, ctx.filename)
                except Exception:  # noqa: BLE001 — retention is best-effort
                    self._cleanup_workspace(workspace_path)
            elif not keep:
                self._cleanup_workspace(workspace_path)

    def _prepare_workspace(self, ctx: JobContext) -> tuple[str | None, int | None]:
        """Extract the uploaded zip into a scratch workspace via the HTTP API.

        Returns ``(workspace_path, file_count)``; the HTTP error propagates so
        a failed extraction fails the job exactly as the inline pipeline did."""
        self._report_step_progress(ctx, "extracting", 10, "Extracting project files")
        ws = self._call(
            f"{DEFAULT_PYTHON_API_URL.rstrip('/')}/api/workspace/prepare",
            method="POST",
            json_body={"filename": ctx.filename},
        ) or {}
        workspace_path = ws.get("extracted_path") if isinstance(ws, dict) else None
        file_count = ws.get("file_count") if isinstance(ws, dict) else None
        self._report_step_progress(ctx, "extracting", 100)
        return workspace_path, file_count

    def _cleanup_workspace(self, workspace_path: str | None) -> None:
        """Delete an extracted workspace dir — only if it lives safely inside
        the configured workspace root (never the root itself)."""
        if not workspace_path or not os.path.isdir(workspace_path):
            return
        try:
            root = os.path.realpath(settings.workspace_root)
            target = os.path.realpath(workspace_path)
            if target == root or not target.startswith(root.rstrip("/") + os.sep):
                return  # safety: never remove the workspace root or outside it
            from infra.php_tools import safe_rmtree

            safe_rmtree(target)
        except Exception as exc:  # noqa: BLE001
            get_exception_logger().warning(
                "Workspace cleanup skipped for %s: %s", workspace_path, exc
            )

    def _select_tool(self, ctx: JobContext) -> str | None:
        route_tool = ctx.route.get("tool")
        if route_tool and route_tool != "auto":
            return str(route_tool)
        if not ctx.route.get("llamaindex", True):
            return None
        tool_names = list((self.config.get("tools") or {}).keys())
        if not tool_names:
            return None
        routed = self._llamaindex_route(ctx.prompt, ctx.filename, ctx.route, tool_names)
        if routed in tool_names:
            return routed
        return tool_names[0]

    def _llamaindex_route(
        self,
        prompt: str,
        filename: str,
        route: dict[str, Any],
        tool_names: list[str],
    ) -> str | None:
        descriptions = []
        for tool_name in tool_names:
            tool_config = (self.config.get("tools") or {}).get(tool_name, {})
            descriptions.append(f"- {tool_name}: {tool_config.get('description', tool_name)}")

        template_text = (
            "You are a routing assistant. Pick exactly one tool name from the allowed list.\n"
            "Return only JSON like {\"tool_name\": \"...\"}.\n\n"
            f"Filename: {filename}\n\n"
            f"Prompt: {prompt}\n\n"
            f"Route: {json.dumps(route, ensure_ascii=False)}\n\n"
            "Allowed tools:\n\n"
            f"{chr(10).join(descriptions)}"
        )

        parsed = self._llamaindex_complete_json(template_text)
        if isinstance(parsed, dict):
            tool_name = parsed.get("tool_name") or parsed.get("tool")
            if isinstance(tool_name, str) and tool_name in tool_names:
                return tool_name
        return None

    def _llamaindex_summarize(
        self,
        prompt: str,
        filename: str,
        tool_name: str | None,
        tool_result: dict[str, Any] | None,
        post_process_result: Any,
    ) -> str | None:
        if not prompt:
            return None
        context = {
            "filename": filename,
            "tool_name": tool_name,
            "tool_result": tool_result,
            "post_process_result": post_process_result,
        }
        # Raw tool output can be huge (full symbol JSON) — cap it so the
        # summarization prompt never blows the model's context window.
        context_json = json.dumps(context, ensure_ascii=False, default=str)
        max_chars = getattr(self, "_context_cap_chars", 8000)
        if len(context_json) > max_chars:
            context_json = context_json[:max_chars] + "\n… (truncated)"
        template_text = (
            "You are answering a developer query from tool output."

            "Write a concise final answer using the provided context."

            "If the context already contains a direct answer, preserve it."

            f"User prompt: {prompt}"

            f"Context: {context_json}"
        )
        result = self._llamaindex_complete_text(template_text)
        return result.strip() if isinstance(result, str) and result.strip() else None

    def _synthesize_with_llamaindex(
        self,
        ctx: JobContext,
        tool_name: str | None,
        tool_result: dict[str, Any] | None,
        post_process_result: Any,
    ) -> str | None:
        return self._llamaindex_summarize(
            prompt=ctx.prompt,
            filename=ctx.filename,
            tool_name=tool_name,
            tool_result=tool_result,
            post_process_result=post_process_result,
        )

    def _llamaindex_complete_text(self, prompt_text: str) -> str:
        llm = getattr(Settings, "llm", None) if Settings is not None else None
        if llm is None:
            return ""
        if PromptTemplate is not None:
            prompt = PromptTemplate(prompt_text)
            formatted = prompt.format()
        else:
            formatted = prompt_text
        try:
            if hasattr(llm, "complete"):
                response = llm.complete(formatted)
                return getattr(response, "text", str(response))
            if hasattr(llm, "predict"):
                response = llm.predict(formatted)
                return getattr(response, "text", str(response))
        except Exception:
            return ""
        return ""

    def _llamaindex_complete_json(self, prompt_text: str) -> dict[str, Any] | None:
        text = self._llamaindex_complete_text(prompt_text)
        if not text:
            return None
        try:
            return json.loads(text)
        except Exception:
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                try:
                    return json.loads(text[start : end + 1])
                except Exception:
                    return None
        return None

    def _run_post_process_pipeline(
        self,
        ctx: JobContext,
        result: Any,
        tool_name: str | None,
        tool_result: dict[str, Any] | None,
    ) -> tuple[Any, str | None]:
        collection_name = None
        for action in ctx.route.get("post_process", []) or []:
            if isinstance(action, str):
                action_name = action
                action_args = {}
            else:
                action_name = action.get("action")
                action_args = action.get("args", {}) or {}
            self._update_step(ctx.job_id, f"post_{action_name}", "running")
            self._report_step_progress(
                ctx, f"post_{action_name}", 5, action_name.replace("_", " ").title()
            )
            handler = self._action_handlers.get(action_name)
            if handler is None:
                self._update_step(ctx.job_id, f"post_{action_name}", "done")
                self._report_step_progress(ctx, f"post_{action_name}", 100)
                continue
            result = handler(result, action_args, tool_name=tool_name, job_id=ctx.job_id, php_base_url=DEFAULT_PYTHON_API_URL)
            if isinstance(result, dict) and result.get("collection_name"):
                collection_name = result.get("collection_name")
            self._update_step(ctx.job_id, f"post_{action_name}", "done")
            self._report_step_progress(ctx, f"post_{action_name}", 100)
        return result, collection_name

    def _collect_doc_context(
        self,
        ctx: JobContext,
        workspace_path: str | None,
        post_process_result: Any,
    ) -> dict[str, Any] | None:
        """Fetch version-aware dependency docs for routes that opt in.

        Returns ``None`` (skipped) unless:
          * the route declares ``doc_retrieval: true``,
          * doc retrieval is globally enabled,
          * a real workspace path with dependency files is available.

        Result shape: ``{"chunks": [...], "provenance": [...], "provider": ...,
        "warning": ...}`` — stored on the job so the answer step can ground on
        it without blowing the context window (chunks already bounded).
        """
        if not (ctx.route or {}).get("doc_retrieval"):
            return None
        if not workspace_path or not os.path.isdir(workspace_path):
            return None
        try:
            from projects.dependencies import scan_dependencies
            from rag.doc_retrieval import retrieve_documentation

            dep_map = scan_dependencies(workspace_path)
            packages = dep_map.get("packages") or {}
            if not packages:
                return None
            self._report_step_progress(ctx, "doc_retrieval", 20, "Fetching Library Docs")
            result = retrieve_documentation(packages, ctx.prompt)
            self._report_step_progress(ctx, "doc_retrieval", 100)
            if result.get("ok") and result.get("chunks"):
                return {
                    "chunks": result["chunks"],
                    "provenance": result.get("provenance", []),
                    "provider": result.get("provider"),
                    "warning": result.get("warning"),
                }
            if result.get("warning"):
                # record the degradation so the UI can surface it
                return {
                    "chunks": [],
                    "provenance": result.get("provenance", []),
                    "provider": None,
                    "warning": result["warning"],
                }
        except Exception:
            get_exception_logger().error(
                "Doc retrieval failed for job %s (route=%s)",
                ctx.job_id,
                ctx.route_name,
                exc_info=True,
            )
        return None

    def _collect_project_profile(
        self, ctx: JobContext, workspace_path: str | None
    ) -> dict[str, Any] | None:
        """Framework-aware project profile (routes/models/controllers).

        Returns None unless the route opts in via ``framework_aware: true`` and
        a framework is confidently detected. Best-effort — failures degrade to
        None and never fail the job.
        """
        if not (ctx.route or {}).get("framework_aware"):
            return None
        if not workspace_path or not os.path.isdir(workspace_path):
            return None
        # Profile cache is keyed by normalized filename only — safe because
        # uploads are analyzed once per filename and later questions reuse that
        # profile (knowledge_cache contract); sandbox runs bypass the cache so
        # runtime probes always reflect the freshly staged source. Re-uploading
        # a new project under the same filename serves the cached profile (same
        # trade as the answer auto-inject path).
        route_sandbox = bool((ctx.route or {}).get("sandbox"))
        try:
            from projects.knowledge_cache import get as cache_get
            from projects.knowledge_cache import put as cache_put
            from projects.project_profile import build_project_profile

            existing = None
            if not route_sandbox:
                try:
                    existing = cache_get(ctx.filename)
                except Exception:  # noqa: BLE001 — a cache read error must never block the job
                    existing = None
            if isinstance(existing, dict) and existing.get("framework"):
                logger.info(
                    "Framework profile cached for job %s (route=%s)",
                    ctx.job_id,
                    ctx.route_name,
                )
                self._report_step_progress(ctx, "framework", 100)
                return existing

            self._report_step_progress(ctx, "framework", 20, "Building framework profile")
            profile = build_project_profile(workspace_path)
            if profile:
                cache_put(ctx.filename, profile)
            self._report_step_progress(ctx, "framework", 100)
            return profile
        except Exception:
            get_exception_logger().error(
                "Framework profile failed for job %s (route=%s)",
                ctx.job_id,
                ctx.route_name,
                exc_info=True,
            )
        return None

    def _collect_structure_profile(
        self, ctx: JobContext, workspace_path: str | None
    ) -> dict[str, Any] | None:
        """Static multi-language structure census (framework/lang/DB/arch).

        Same cache contract as ``_collect_project_profile`` but scoped to the
        route's ``structure: true`` opt-in and driven by the pure deterministic
        ``projects.structure_profile`` module. Best-effort — a cache read or
        build failure degrades to None and never fails the job.
        """
        if not (ctx.route or {}).get("structure"):
            return None
        if not workspace_path or not os.path.isdir(workspace_path):
            return None
        try:
            from projects.knowledge_cache import get as cache_get
            from projects.knowledge_cache import put as cache_put
            from projects.structure_profile import build_structure_profile

            existing = None
            if not bool((ctx.route or {}).get("sandbox")):
                try:
                    existing = cache_get(ctx.filename)
                except Exception:  # noqa: BLE001 — a cache read error must never block the job
                    existing = None
            if isinstance(existing, dict) and existing.get("framework"):
                logger.info(
                    "Structure profile cached for job %s (route=%s)",
                    ctx.job_id,
                    ctx.route_name,
                )
                return existing

            profile = build_structure_profile(
                workspace_path,
                progress=lambda pct, label: self._report_step_progress(
                    ctx, "structure", pct, label
                ),
            )
            if profile:
                cache_put(ctx.filename, profile)
            return profile
        except Exception:  # noqa: BLE001 — structure profile is best-effort
            get_exception_logger().error(
                "Structure profile failed for job %s (route=%s)",
                ctx.job_id,
                ctx.route_name,
                exc_info=True,
            )
        return None

    def _produce_code_chunks(
        self, ctx: JobContext, workspace_path: str | None
    ) -> dict[str, Any]:
        """Language-dispatched code-index parse (index_code route preview).

        Pure producer wrapper: workspace guard → ``projects.lang_index
        .produce_chunks`` (which derives the primary language from the census
        and dispatches php/python/other). Never raises — failures degrade to a
        truthful ``{"chunks": [], "error": ...}`` dict. Full chunk bodies are
        returned to the step for the preview; the JOB record only ever stores
        the compact ``code_index`` summary.
        """
        empty = {"language": None, "chunks": [], "files": 0, "producer": None}
        if not workspace_path or not os.path.isdir(workspace_path):
            return {**empty, "error": f"workspace not found: {workspace_path}"}
        try:
            from projects.lang_index import produce_chunks

            self._report_step_progress(ctx, "index", 20, "Indexing codebase")
            result = produce_chunks(
                workspace_path,
                progress=lambda pct, label: self._report_step_progress(
                    ctx, "index", pct, label
                ),
            )
            self._report_step_progress(ctx, "index", 100)
            return result
        except Exception as exc:  # noqa: BLE001 — produce is best-effort
            get_exception_logger().error(
                "Code index failed for job %s (route=%s): %s",
                getattr(ctx, "job_id", "?"),
                getattr(ctx, "route_name", "?"),
                exc,
                exc_info=exc,
            )
            return {**empty, "error": str(exc)[:300]}

    def _safe_project_id(self, filename: str) -> str:
        """Filesystem-safe project id (must also be safe as a DinD path seg)."""
        stem = Path(filename).stem if filename else "project"
        cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", stem)
        return cleaned.strip(".") or "project"

    def _run_sandbox_analysis(
        self,
        ctx: JobContext,
        workspace_path: str | None,
        project_profile: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Stage + install + probe the project inside the nested sandbox.

        Best-effort: every failure is captured as a structured record (honest
        status/reason) — a missing backend, image or egress never fails the job.
        """
        if not (ctx.route or {}).get("sandbox"):
            return None
        if not workspace_path or not os.path.isdir(workspace_path):
            return {"status": "failed", "error": "workspace missing"}
        try:
            from sandbox.executor import default_executor

            executor = default_executor()
            if executor is None:
                return {"status": "skipped", "reason": "sandbox backend unavailable"}
            framework = (project_profile or {}).get("framework")
            probes = ctx.route.get("sandbox_probes") or None
            return executor.analyze(
                workspace_path,
                project_id=self._safe_project_id(ctx.filename),
                framework=str(framework) if framework else "php",
                probes=list(probes) if isinstance(probes, list) else None,
                progress=lambda pct, label: self._report_step_progress(
                    ctx, "sandbox", pct, label
                ),
            )
        except Exception as exc:
            get_exception_logger().error(
                "Sandbox analysis failed for job %s: %s", ctx.job_id, exc, exc_info=exc
            )
            return {"status": "failed", "error": str(exc)[:500]}

    def _merge_runtime_routes(
        self,
        project_profile: dict[str, Any] | None,
        sandbox_result: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Promote the sandbox routes probe into the profile (runtime truth)."""
        try:
            from projects.project_profile import merge_runtime_routes

            return merge_runtime_routes(project_profile, sandbox_result)
        except Exception as exc:
            get_exception_logger().error(
                "Runtime route merge failed: %s", exc, exc_info=exc
            )
            return project_profile

    def _persist_result_bundle(
        self,
        ctx: JobContext,
        doc_context: dict[str, Any] | None,
        project_profile: dict[str, Any] | None,
        sandbox_result: dict[str, Any] | None,
    ) -> None:
        """Write the DURABLE per-job result bundle.

        Orchestrator status files live in an ephemeral dir and are never
        re-read — the bundle under ANALYSES_DIR/jobs/<job_id>/ survives restarts
        and is the source a later "Save analysis" rehydrates from. Best-effort.
        """
        try:
            from analysis.analysis_service import bundle_path, write_bundle

            job = self.get_job(ctx.job_id) or {}
            write_bundle(
                ctx.job_id,
                {
                    "job_id": ctx.job_id,
                    "route_name": ctx.route_name,
                    "filename": ctx.filename,
                    "prompt": ctx.prompt,
                    "collection_name": job.get("collection_name"),
                    "status": job.get("status"),
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "output": job.get("output"),
                    "doc_context": doc_context,
                    "project_profile": project_profile,
                    "structure_profile": job.get("structure_profile"),
                    "code_index": job.get("code_index"),
                    "sandbox": sandbox_result,
                    # F15: compact run manifest — everything a downstream
                    # consumer needs without re-reading the heavy payloads.
                    "manifest": self._bundle_manifest(
                        ctx, job, doc_context, project_profile, sandbox_result
                    ),
                },
            )
        except Exception as exc:
            get_exception_logger().error(
                "Result bundle write failed for job %s: %s", ctx.job_id, exc
            )

    @staticmethod
    def _bundle_manifest(
        ctx: JobContext,
        job: dict[str, Any],
        doc_context: dict[str, Any] | None,
        project_profile: dict[str, Any] | None,
        sandbox_result: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Compact counts for the durable bundle (F15) — pure derivation."""
        profile = project_profile or {}
        sandbox = sandbox_result or {}
        probes = [
            {
                "name": (p or {}).get("name"),
                "status": (p or {}).get("status"),
                "exit_code": (p or {}).get("exit_code"),
            }
            for p in (sandbox.get("probes") or [])
        ]
        output = job.get("output")
        return {
            "filename": ctx.filename,
            "route": ctx.route_name,
            "status": job.get("status"),
            "output_chars": len(output) if isinstance(output, str) else None,
            "collection": job.get("collection_name"),
            "doc_chunks": len((doc_context or {}).get("chunks") or []),
            "doc_provider": (doc_context or {}).get("provider"),
            "routes": len(profile.get("routes") or []),
            "routes_source": profile.get("routes_source"),
            "models": len(profile.get("models") or []),
            "tables": len(profile.get("schema") or []),
            "controllers": len(profile.get("controllers") or []),
            "sandbox_image": sandbox.get("runtime_image"),
            "install": (sandbox.get("install") or {}).get("status"),
            "probes": probes,
        }

    def _act_extract_json(
        self,
        result: Any,
        args: dict[str, Any],
        tool_name: str | None,
        job_id: str,
        php_base_url: str,
    ) -> Any:
        if isinstance(result, (dict, list)):
            return result
        if not isinstance(result, str):
            return result
        stripped = result.strip()
        try:
            return json.loads(stripped)
        except Exception:
            pass
        start = stripped.find("[")
        end = stripped.rfind("]")
        if start >= 0 and end > start:
            try:
                return json.loads(stripped[start : end + 1])
            except Exception:
                return result
        return result

    def _act_send_to_qdrant(
        self,
        result: Any,
        args: dict[str, Any],
        tool_name: str | None,
        job_id: str,
        php_base_url: str,
    ) -> dict[str, Any]:
        chunks = []
        if isinstance(result, dict):
            chunks = result.get("chunks") or result.get("chunk") or []
        elif isinstance(result, list):
            chunks = result
        else:
            chunks = [{"text": str(result)}]

        request_body = {
            "job_id": job_id,
            "tool_name": tool_name,
            "chunks": chunks,
            "filename": args.get("filename"),
            "text": self._chunks_to_text(chunks),
        }
        response = self._call(
            f"{DEFAULT_PYTHON_API_URL.rstrip('/')}/download-jobs",
            method="POST",
            json_body=request_body,
            headers={"X-Internal-Secret": self._internal_secret()},
        ) or {}
        return {
            "collection_name": response.get("collection_name") or args.get("collection_name"),
            "chunk_count": response.get("chunk_count") or len(chunks),
            "download_id": response.get("download_id"),
            "raw_chunks": chunks,
        }

    def _act_poll_embedding(
        self,
        result: Any,
        args: dict[str, Any],
        tool_name: str | None,
        job_id: str,
        php_base_url: str,
    ) -> dict[str, Any]:
        download_id = args.get("download_id")
        if not download_id and isinstance(result, dict):
            download_id = result.get("download_id")
        if not download_id:
            return {"success": False, "error": "download_id is required"}
        max_wait = int(args.get("max_wait", 300))
        interval = float(args.get("interval", 2.0))
        started_at = time.time()
        while time.time() - started_at < max_wait:
            status = self._call(
                f"{DEFAULT_PYTHON_API_URL.rstrip('/')}/download-jobs/{urllib.parse.quote(str(download_id))}/status",
                method="GET",
                headers={"X-Internal-Secret": self._internal_secret()},
            ) or {}
            if status.get("status") in {"done", "unknown"}:
                return {
                    "indexed": status.get("indexed"),
                    "total": status.get("total"),
                    "collection_name": status.get("collection_name"),
                    "download_id": download_id,
                }
            if status.get("status") == "error":
                return {"success": False, "error": status.get("error") or "Embedding failed", "download_id": download_id}
            time.sleep(interval)
        return {"success": False, "error": "Embedding polling timed out", "download_id": download_id}

    def _act_wrap_code_block(
        self,
        result: Any,
        args: dict[str, Any],
        tool_name: str | None,
        job_id: str,
        php_base_url: str,
    ) -> str:
        language = args.get("language", "text")
        prefix = args.get("prefix") or args.get("prepend") or ""
        text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
        return f"{prefix}```{language} {text}```"

    def _act_truncate(
        self,
        result: Any,
        args: dict[str, Any],
        tool_name: str | None,
        job_id: str,
        php_base_url: str,
    ) -> str:
        max_bytes = int(args.get("max_bytes", 8000))
        text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
        encoded = text.encode("utf-8")
        if len(encoded) <= max_bytes:
            return text
        truncated = encoded[:max_bytes]
        cut = truncated.rfind(b" ")
        if cut > 0:
            truncated = truncated[:cut]
        output = truncated.decode("utf-8", errors="ignore")
        percent = round((len(truncated) / max(len(encoded), 1)) * 100, 2)
        return f"{output} [truncated to {percent}% of {len(encoded)} bytes]"

    def _act_llamaindex_ingest(
        self,
        result: Any,
        args: dict[str, Any],
        tool_name: str | None,
        job_id: str,
        php_base_url: str,
    ) -> Any:
        """Ingest chunks into Neo4j property graph + Qdrant vectors."""
        # Unwrap MCP tool response dict if needed
        if isinstance(result, dict) and "stdout" in result:
            result = result["stdout"]

        # Extract JSON from Artisan output if needed
        if isinstance(result, str):
            import re
            try:
                result = json.loads(result)
            except json.JSONDecodeError:
                for m in re.finditer(r'\[\s*\n\s*\{', result):
                    idx = m.start()
                    depth = 0; end = -1
                    for i in range(idx, len(result)):
                        if result[i] == '[': depth += 1
                        elif result[i] == ']':
                            depth -= 1
                            if depth == 0: end = i; break
                    if end > idx:
                        try: result = json.loads(result[idx:end + 1]); break
                        except json.JSONDecodeError: pass

        if not isinstance(result, list) or len(result) == 0:
            return {"error": "No chunk list to ingest"}

        job = self.get_job(job_id) or {}
        collection_name = args.get("collection_name") or project_collection_name(job.get("filename", ""))

        # Session-scoped dedup: if this project was already indexed, skip the
        # (expensive) re-embedding. Still return the collection name so retrieval
        # can search the existing vectors.
        if job.get("skip_index"):
            return {
                "collection_name": collection_name,
                "already_indexed": True,
                "ingested": 0,
                "chunk_count": len(result),
            }

        from index.llamaindex_layer import CodePropertyGraph

        key = "post_llamaindex_ingest"
        # Coarse, honest phase hints only — ingest batches inside llamaindex
        # internals, so 20/60 are the only externally observable checkpoints.
        self._report_job_progress(job_id, key, 20, "Recreating vector collection")
        ingested = CodePropertyGraph().ingest(result, collection_name)
        self._report_job_progress(job_id, key, 60, "Embedding chunks")
        self._report_job_progress(job_id, key, 100)
        return ingested

    def _act_llamaindex_route(
        self,
        result: Any,
        args: dict[str, Any],
        tool_name: str | None,
        job_id: str,
        php_base_url: str,
    ) -> Any:
        prompt = args.get("prompt") or ""
        text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
        return self._llamaindex_summarize(
            prompt=prompt or text,
            filename=args.get("filename", ""),
            tool_name=tool_name,
            tool_result={"stdout": text},
            post_process_result=result,
        ) or result

    def _act_llamaindex_summarize(
        self,
        result: Any,
        args: dict[str, Any],
        tool_name: str | None,
        job_id: str,
        php_base_url: str,
    ) -> Any:
        prompt = args.get("prompt") or ""
        text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
        return self._llamaindex_summarize(
            prompt=prompt or text,
            filename=args.get("filename", ""),
            tool_name=tool_name,
            tool_result={"stdout": text},
            post_process_result=result,
        ) or result

    def _extract_tool_output(self, tool_result: dict[str, Any] | None) -> Any:
        if not tool_result:
            return ""
        if "stdout" in tool_result:
            return tool_result["stdout"]
        if "result" in tool_result:
            return tool_result["result"]
        return tool_result

    def _chunks_to_text(self, chunks: list[Any]) -> str:
        lines: list[str] = []
        for chunk in chunks:
            if isinstance(chunk, dict):
                lines.append(str(chunk.get("code") or chunk.get("text") or chunk.get("name") or chunk))
            else:
                lines.append(str(chunk))
        return "".join(lines)

    def _internal_secret(self) -> str:
        return str(self.config.get("internal_secret") or DEFAULT_INTERNAL_SECRET)

    def _call_php_tool(
        self, ctx: JobContext, tool_name: str, workspace_path: str | None
    ) -> dict[str, Any] | None:
        try:
            tool_config = (self.config.get("tools") or {}).get(tool_name, {})
            args: dict[str, Any] = {}
            for key, value in (tool_config.get("args") or {}).items():
                args[key] = self._resolve_env(value)

            if tool_name == "chunk_code":
                self._report_step_progress(ctx, "tool", 20, "Parsing code chunks")
                result = self._php_code_parse(workspace_path, args)
            elif tool_name == "export_structure":
                self._report_step_progress(
                    ctx, "tool", 20, "Exporting project structure"
                )
                result = self._php_export_structure(workspace_path, args)
            else:
                return {"success": False, "error": f"Unknown tool: {tool_name}"}
            if isinstance(result, dict) and result.get("success") is not False:
                self._report_step_progress(ctx, "tool", 100)
            return result
        except Exception as exc:
            get_exception_logger().error(
                "PHP tool call '%s' failed: %s", tool_name, exc, exc_info=exc
            )
            return None

    def _php_code_parse(self, workspace_path: str | None, args: dict[str, Any]) -> dict[str, Any]:
        if not workspace_path or not os.path.isdir(workspace_path):
            return {"success": False, "error": f"Workspace not found: {workspace_path}"}

        from infra.php_tools import run_code_parse

        chunks = run_code_parse(workspace_path)
        return {
            "success": True,
            "status": "done",
            "exit_code": 0,
            "stdout": json.dumps(chunks, ensure_ascii=False),
        }

    def _php_export_structure(self, workspace_path: str | None, args: dict[str, Any]) -> dict[str, Any]:
        if not workspace_path or not os.path.isdir(workspace_path):
            return {"success": False, "error": f"Workspace not found: {workspace_path}"}

        from infra.php_tools import cap_structure, run_export_structure

        depth = int(args.get("depth", 4) or 4)
        markdown = run_export_structure(workspace_path, depth=depth)
        return {
            "success": True,
            "status": "done",
            "exit_code": 0,
            "stdout": cap_structure(markdown),
        }

    def _call(
        self,
        url: str,
        method: str = "GET",
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        try:
            request_headers = headers.copy() if headers else {}
            if json_body is not None:
                request_headers.setdefault("Content-Type", "application/json")
            response = requests.request(
                method=method.upper(),
                url=url,
                json=json_body,
                headers=request_headers,
                timeout=120,
            )
            if response.status_code >= 400:
                try:
                    return response.json()
                except Exception:
                    return {"error": response.text, "status_code": response.status_code}
            if not response.text.strip():
                return {}
            try:
                payload = response.json()
            except Exception:
                return {"text": response.text, "status_code": response.status_code}
            if isinstance(payload, dict):
                payload.setdefault("headers", dict(response.headers))
            return payload if isinstance(payload, dict) else {"result": payload}
        except Exception as exc:
            get_exception_logger().error(
                "HTTP request failed: %s %s: %s", method.upper(), url, exc
            )
            return None

    def _report_step_progress(
        self,
        ctx: Any,
        key: str,
        percent: int,
        label: str | None = None,
    ) -> None:
        """Record live per-step progress on the in-memory job (0..100 + label).

        Memory-only by contract: progress history is never persisted, so a
        finished run simply reports 100s. Never raises — a missing job (or a
        bare ``__new__`` runner in tests) is a silent no-op. The status file is
        NOT rewritten here; later ``_update_step``/``_save_outputs`` calls copy
        the latest ``steps_progress`` through ``_write_status``.
        """
        self._report_job_progress(getattr(ctx, "job_id", None), key, percent, label)

    def _report_job_progress(
        self,
        job_id: str | None,
        key: str,
        percent: int,
        label: str | None = None,
    ) -> None:
        if not job_id:
            return
        jobs = getattr(self, "_jobs", None)
        if jobs is None:
            return
        percent = max(0, min(100, int(percent)))
        with self._lock:
            job = jobs.get(job_id)
            if not job:
                return
            entry = {"percent": percent}
            if label is not None:
                entry["label"] = label
            job.setdefault("steps_progress", {})[key] = entry

    def _update_step(self, job_id: str, key: str, status: str, **extra: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            for step in job.get("steps", []):
                if step.get("key") == key:
                    step["status"] = status
                    step.update(extra)
                    break
            job["updated_at"] = time.time()
        self._write_status(job_id)

    def _write_status(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            status_path = self._status_dir / f"{job_id}.orchestrator.json"
            payload = {
                "job_id": job_id,
                "status": job.get("status"),
                "route_name": job.get("route_name"),
                "filename": job.get("filename"),
                "prompt": job.get("prompt"),
                "route": job.get("route"),
                "started_at": job.get("started_at"),
                "updated_at": job.get("updated_at"),
                "steps": job.get("steps", []),
                "steps_progress": job.get("steps_progress"),
                "output": job.get("output"),
                "tool_name": job.get("tool_name"),
                "collection_name": job.get("collection_name"),
                "tool_output": job.get("tool_output"),
                "doc_context": job.get("doc_context"),
                "project_profile": job.get("project_profile"),
                "structure_profile": job.get("structure_profile"),
                "sandbox": job.get("sandbox"),
                "code_index": job.get("code_index"),
                "entities": job.get("entities"),
                "error": job.get("error"),
            }
        status_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _save_outputs(
        self,
        job_id: str,
        output: Any,
        tool_name: str | None = None,
        collection_name: str | None = None,
        tool_result: dict[str, Any] | None = None,
        tool_output: Any = None,
        doc_context: dict[str, Any] | None = None,
        project_profile: dict[str, Any] | None = None,
        structure_profile: dict[str, Any] | None = None,
        sandbox: dict[str, Any] | None = None,
        code_index: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job["status"] = "done"
            job["tool_name"] = tool_name
            job["collection_name"] = collection_name
            job["tool_result"] = tool_result
            job["tool_output"] = tool_output
            job["output"] = output
            job["doc_context"] = doc_context
            job["project_profile"] = project_profile
            job["structure_profile"] = structure_profile
            job["sandbox"] = sandbox
            job["code_index"] = code_index
            job["updated_at"] = time.time()
        self._write_status(job_id)

    def _save_error(self, job_id: str, error: str) -> None:
        with self._lock:
            job = self._jobs.setdefault(job_id, {"steps": []})
            job["status"] = "error"
            job["error"] = error
            job["updated_at"] = time.time()
        self._write_status(job_id)


_config_path = Path(__file__).parent.parent / "config" / "orchestrator.yml"
orchestrator = Orchestrator(str(_config_path))
