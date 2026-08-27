"""Hybrid orchestration with LlamaIndex routing.

This module keeps the existing job lifecycle, status persistence, and
post-processing pipeline, but adds a LlamaIndex-backed routing and synthesis
layer so orchestration can evolve without rewriting the runtime again. Code
tools (`chunk_code`, `export_structure`) are executed by the in-image pure-PHP
CLI instead of a remote MCP server.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import requests
import yaml
from settings import settings
from exception_logger import get_exception_logger

try:
    from llama_index.core import Settings
    Settings.llm = None  # No local LLM — server.ts handles final LLM call
except Exception:  # pragma: no cover
    Settings = None  # type: ignore[assignment]

try:
    from llama_index.core.prompts import PromptTemplate
except Exception:  # pragma: no cover
    PromptTemplate = None  # type: ignore[assignment]


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
        steps: list[dict[str, Any]] = [
            {"key": "extracting", "label": "Extracting project files", "status": "pending"},
        ]
        tool_name = route.get("tool")
        if tool_name in (None, "", "auto") or route.get("llamaindex", True):
            steps.append({"key": "routing", "label": "Routing with LlamaIndex", "status": "pending"})
        if tool_name not in (None, "", "auto"):
            steps.append({"key": "tool", "label": f"Running {tool_name}", "status": "pending"})
        elif route.get("llamaindex", True):
            steps.append({"key": "tool", "label": "Running selected tool", "status": "pending"})
        for action in route.get("post_process", []) or []:
            action_name = action.get("action") if isinstance(action, dict) else str(action)
            steps.append({"key": f"post_{action_name}", "label": action_name.replace("_", " ").title(), "status": "pending"})
        return steps

    def _run_job(self, ctx: JobContext) -> None:
        try:
            self._update_step(ctx.job_id, "extracting", "running")
            ws = self._call(
                f"{DEFAULT_PYTHON_API_URL.rstrip('/')}/api/workspace/prepare",
                method="POST",
                json_body={"filename": ctx.filename},
            ) or {}
            workspace_path = ws.get("extracted_path") if isinstance(ws, dict) else None
            file_count = ws.get("file_count") if isinstance(ws, dict) else None
            self._update_step(ctx.job_id, "extracting", "done", file_count=file_count if file_count is not None else "Skipped")

            tool_name = self._select_tool(ctx)
            self._update_step(ctx.job_id, "routing", "done")
            tool_result: dict[str, Any] | None = None
            if tool_name:
                self._update_step(ctx.job_id, "tool", "running")
                tool_result = self._call_php_tool(tool_name, workspace_path)
                if not tool_result or tool_result.get("success") is False:
                    error_message = None if not tool_result else tool_result.get("error")
                    raise RuntimeError(error_message or f"PHP tool failed: {tool_name}")
                self._update_step(ctx.job_id, "tool", "done")

            raw_output = self._extract_tool_output(tool_result) if tool_result else ctx.prompt
            post_process_result, collection_name = self._run_post_process_pipeline(
                ctx,
                raw_output,
                tool_name=tool_name,
                tool_result=tool_result,
            )
            synthesized_output = self._synthesize_with_llamaindex(
                ctx=ctx,
                tool_name=tool_name,
                tool_result=tool_result,
                post_process_result=post_process_result,
            )
            final_output = synthesized_output or post_process_result or raw_output

            self._save_outputs(
                ctx.job_id,
                output=final_output,
                tool_name=tool_name,
                collection_name=collection_name,
                tool_result=tool_result,
                tool_output=post_process_result,
            )
        except Exception as exc:
            self._save_error(ctx.job_id, str(exc))
            get_exception_logger().error(
                "Orchestration job %s failed (route=%s): %s",
                ctx.job_id,
                ctx.route_name,
                exc,
                exc_info=exc,
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
        template_text = (
            "You are answering a developer query from tool output."

            "Write a concise final answer using the provided context."

            "If the context already contains a direct answer, preserve it."

            f"User prompt: {prompt}"

            f"Context: {json.dumps(context, ensure_ascii=False, default=str)}"
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
            handler = self._action_handlers.get(action_name)
            if handler is None:
                self._update_step(ctx.job_id, f"post_{action_name}", "done")
                continue
            result = handler(result, action_args, tool_name=tool_name, job_id=ctx.job_id, php_base_url=DEFAULT_PYTHON_API_URL)
            if isinstance(result, dict) and result.get("collection_name"):
                collection_name = result.get("collection_name")
            self._update_step(ctx.job_id, f"post_{action_name}", "done")
        return result, collection_name

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

        from llamaindex_layer import CodePropertyGraph

        return CodePropertyGraph().ingest(result, collection_name)

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

    def _call_php_tool(self, tool_name: str, workspace_path: str | None) -> dict[str, Any] | None:
        try:
            tool_config = (self.config.get("tools") or {}).get(tool_name, {})
            args: dict[str, Any] = {}
            for key, value in (tool_config.get("args") or {}).items():
                args[key] = self._resolve_env(value)

            if tool_name == "chunk_code":
                return self._php_code_parse(workspace_path, args)
            if tool_name == "export_structure":
                return self._php_export_structure(workspace_path, args)
            return {"success": False, "error": f"Unknown tool: {tool_name}"}
        except Exception as exc:
            get_exception_logger().error(
                "PHP tool call '%s' failed: %s", tool_name, exc, exc_info=exc
            )
            return None

    def _php_code_parse(self, workspace_path: str | None, args: dict[str, Any]) -> dict[str, Any]:
        if not workspace_path or not os.path.isdir(workspace_path):
            return {"success": False, "error": f"Workspace not found: {workspace_path}"}

        from php_tools import run_code_parse

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

        from php_tools import cap_structure, run_export_structure

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
                "output": job.get("output"),
                "tool_name": job.get("tool_name"),
                "collection_name": job.get("collection_name"),
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
            job["updated_at"] = time.time()
        self._write_status(job_id)

    def _save_error(self, job_id: str, error: str) -> None:
        with self._lock:
            job = self._jobs.setdefault(job_id, {"steps": []})
            job["status"] = "error"
            job["error"] = error
            job["updated_at"] = time.time()
        self._write_status(job_id)


_config_path = Path(__file__).parent / "config" / "orchestrator.yml"
orchestrator = Orchestrator(str(_config_path))
