"""Orchestrator — tool execution with user-facing step monitoring.

Coordinates PHP parse-code, Python embedding, and MCP tool calls
with step-by-step progress tracking via status files.
"""

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from datetime import datetime, timezone

import yaml

from settings import settings


class Orchestrator:
    def __init__(self, config_path: str):
        with open(config_path) as f:
            self.config = yaml.safe_load(f)
        self._jobs: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._status_dir = Path(settings.download_dir)

    def get_route(self, route_name: str) -> dict | None:
        return self.config.get("routes", {}).get(route_name)

    def get_template(self, name: str) -> str:
        p = Path(__file__).parent / "config" / "templates" / f"{name}.md"
        return p.read_text(encoding="utf-8") if p.is_file() else ""

    def start_job(self, job_id, route_name, filename, prompt,
                  php_base_url="http://php-ast:9003"):
        route = self.get_route(route_name) or {"tool": None, "prompt_template": "chat"}
        tool_name = route.get("tool")
        template_name = route.get("prompt_template", "chat")

        steps = self._build_steps(route)
        with self._lock:
            self._jobs[job_id] = {
                "status": "starting", "route": route_name,
                "template_name": template_name, "steps": steps,
                "current_step": 0, "outputs": {}, "error": None,
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
        threading.Thread(target=self._run_job, args=(job_id, route, filename, prompt, php_base_url), daemon=True).start()

    def get_job(self, job_id):
        with self._lock:
            return self._jobs.get(job_id)

    def _build_steps(self, route):
        tool_name = route.get("tool")
        steps = []
        # Every route starts with extraction
        steps.append({"id": "extracting", "label": "Extracting project files",
                      "status": "pending", "detail": None, "progress": None})
        if tool_name:
            steps.append({"id": "tool", "label": self._tool_label(tool_name),
                          "status": "pending", "detail": None, "progress": None})
        # chunk_code also needs embedding
        if tool_name == "chunk_code":
            steps.append({"id": "embedding", "label": "Embedding into Qdrant",
                          "status": "pending", "detail": None, "progress": None})
        # All routes end with generation
        steps.append({"id": "generating", "label": "Answering with LLM",
                      "status": "pending", "detail": None, "progress": None})
        return steps

    def _tool_label(self, tool_name):
        labels = {
            "chunk_code": "PHP code chunking",
            "export_structure": "Project structure export",
        }
        return labels.get(tool_name, tool_name or "tool")

    def _run_job(self, job_id, route, filename, prompt, php_base_url):
        try:
            steps = self._build_steps(route)
            tool_name = route.get("tool")
            template_name = route.get("prompt_template", "chat")
            collection_name = None

            # Step 1: Extract workspace
            self._update_step(job_id, steps, "extracting", "running")
            ws = _call(php_base_url + "/api/workspace/prepare",
                       {"filename": filename})
            if ws and ws.get("workspace_id"):
                self._update_step(job_id, steps, "extracting", "done",
                                  detail=f"{ws.get('file_count', 0)} files")
            else:
                self._update_step(job_id, steps, "extracting", "done",
                                  detail="Skipped (non-ZIP or stored)")

            # Step 2: Run configured tool via MCP (if any)
            tool_output = None
            if tool_name:
                self._update_step(job_id, steps, "tool", "running",
                                  progress={"current": 0, "total": 0, "percent": 0})

                mcp_result = self._call_mcp_tool(tool_name, filename, prompt)

                if mcp_result and mcp_result.get("success"):
                    tool_output = mcp_result.get("stdout") or mcp_result.get("result")
                    self._update_step(job_id, steps, "tool", "done",
                                      detail=f"Tool {tool_name} completed")
                    if tool_name == "chunk_code":
                        collection_name = mcp_result.get("collection_name")
                elif mcp_result and mcp_result.get("error"):
                    self._update_step(job_id, steps, "tool", "error",
                                      detail=mcp_result["error"])
                    return
                else:
                    # Fallback: REST
                    rest_result = _call(php_base_url + "/api/tools/execute",
                                        {"filename": filename, "tool_names": [tool_name]})
                    if rest_result and rest_result.get("results"):
                        for r in rest_result["results"]:
                            if r.get("tool") == tool_name:
                                if r.get("exit_code") != 0:
                                    self._update_step(job_id, steps, "tool", "error",
                                                      detail=r.get("stderr", "Tool failed"))
                                    return
                                tool_output = r.get("stdout")
                                if tool_name == "chunk_code":
                                    try:
                                        d = json.loads(tool_output)
                                        collection_name = d.get("collection_name")
                                    except Exception: pass
                                break
                    if not tool_output:
                        self._update_step(job_id, steps, "tool", "done",
                                          detail="No output")

            # Step 3: Embedding (chunk_code only)
            if tool_name == "chunk_code":
                self._update_step(job_id, steps, "embedding", "done",
                                  detail="Indexed in Qdrant" if collection_name else "No collection")

            # Step 4: Done
            self._update_step(job_id, steps, "generating", "running")
            self._update_step(job_id, steps, "generating", "done",
                              detail="Ready")
            self._save_outputs(job_id, steps, template_name, collection_name, prompt, tool_output)
        except Exception as e:
            self._save_error(job_id, str(e))

    def _update_step(self, job_id, steps, step_id, status, detail=None,
                     progress=None):
        for s in steps:
            if s["id"] == step_id:
                s["status"] = status
                s["detail"] = detail
                if progress is not None:
                    s["progress"] = progress
                break
        # Sync to in-memory job dict
        with self._lock:
            if job_id in self._jobs:
                self._jobs[job_id]["steps"] = steps
                self._jobs[job_id]["status"] = "running"
        self._write_status(job_id, steps)

    def _write_status(self, job_id, steps):
        try:
            self._status_dir.mkdir(parents=True, exist_ok=True)
            p = self._status_dir / f"{job_id}.orchestrator.json"
            all_done = all(s["status"] == "done" for s in steps)
            with self._lock:
                j = self._jobs.get(job_id, {})
            payload = {
                "status": "done" if all_done else "running",
                "route": j.get("route", ""),
                "template_name": j.get("template_name", "chat"),
                "collection_name": j.get("collection_name"),
                "tool_output": j.get("tool_output"),
                "prompt": j.get("prompt"),
                "started_at": j.get("started_at"),
                "steps": steps,
                "current_step": sum(1 for s in steps if s["status"] == "done"),
                "total_steps": len(steps),
                "outputs": j.get("outputs", {}),
                "error": j.get("error"),
            }
            p.write_text(json.dumps(payload))
        except Exception:
            pass

    def _save_outputs(self, job_id, steps, template_name, collection_name=None, prompt="", tool_output=None):
        with self._lock:
            if job_id in self._jobs:
                self._jobs[job_id]["outputs"]["done"] = {"template_name": template_name}
                if collection_name:
                    self._jobs[job_id]["collection_name"] = collection_name
                if prompt:
                    self._jobs[job_id]["prompt"] = prompt
                if tool_output:
                    self._jobs[job_id]["tool_output"] = tool_output
                self._jobs[job_id]["status"] = "done"
        self._write_status(job_id, steps)

    def _save_error(self, job_id, error_msg):
        with self._lock:
            if job_id in self._jobs:
                self._jobs[job_id]["status"] = "error"
                self._jobs[job_id]["error"] = error_msg
        try:
            self._status_dir.mkdir(parents=True, exist_ok=True)
            (self._status_dir / f"{job_id}.orchestrator.json").write_text(
                json.dumps({"status": "error", "error": error_msg}))
        except Exception:
            pass


    # ── MCP client ──────────────────────────────────────────────────────────

    def _get_mcp_schema(self, tool_name: str, session_id: str, mcp_url: str) -> set:
        """Query MCP tools/list to get accepted parameter names for a tool."""
        try:
            list_body = json.dumps({
                "jsonrpc": "2.0", "method": "tools/list", "id": "list-1",
            }).encode("utf-8")
            list_req = urllib.request.Request(
                mcp_url, data=list_body,
                headers={"Content-Type": "application/json",
                         "Accept": "application/json",
                         "Mcp-Session-Id": session_id},
                method="POST")
            with urllib.request.urlopen(list_req, timeout=15) as resp:
                data = json.load(resp)
            tools = data.get("result", {}).get("tools", [])
            for t in tools:
                if t.get("name") == tool_name:
                    return set(t.get("inputSchema", {}).get("properties", {}).keys())
        except Exception:
            pass
        return set()

    def _call_mcp_tool(self, tool_name: str, filename: str, prompt: str = "") -> dict | None:
        """Call MCP tool via JSON-RPC 2.0. Returns {success, ...} or None."""
        mcp_url = self._get_mcp_url()
        try:
            # Phase 1: Initialize
            init_body = json.dumps({
                "jsonrpc": "2.0", "method": "initialize",
                "params": {"protocolVersion": "2024-11-05",
                           "capabilities": {"tools": {"listChanged": False}},
                           "clientInfo": {"name": "orchestrator", "version": "1.0"}},
                "id": "init-1",
            }).encode("utf-8")
            init_req = urllib.request.Request(mcp_url, data=init_body,
                                              headers={"Content-Type": "application/json", "Accept": "application/json"}, method="POST")
            with urllib.request.urlopen(init_req, timeout=30) as resp:
                session_id = resp.headers.get("Mcp-Session-Id", "")

            if not session_id:
                return None

            # Phase 2: Send initialized notification
            notif_body = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode("utf-8")
            notif_req = urllib.request.Request(mcp_url, data=notif_body,
                                               headers={"Content-Type": "application/json", "Accept": "application/json", "Mcp-Session-Id": session_id}, method="POST")
            try: urllib.request.urlopen(notif_req, timeout=10)
            except Exception: pass

            # Phase 2.5: Get tool schema to filter args
            accepted = self._get_mcp_schema(tool_name, session_id, mcp_url)

            # Phase 3: Call tool — only pass accepted params
            base_args = dict(self.config.get("tools", {}).get(tool_name, {}).get("args", {}))
            arguments = {}
            # filename + prompt: pass only if tool accepts them
            for key, val in [("filename", filename), ("prompt", prompt)]:
                if key in accepted: arguments[key] = val
            # Config args: pass only if accepted
            for k, v in base_args.items():
                if k in accepted: arguments[k] = v

            call_body = json.dumps({
                "jsonrpc": "2.0", "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
                "id": "call-1",
            }).encode("utf-8")
            call_req = urllib.request.Request(mcp_url, data=call_body,
                                              headers={"Content-Type": "application/json", "Accept": "application/json", "Mcp-Session-Id": session_id}, method="POST")
            with urllib.request.urlopen(call_req, timeout=300) as resp:
                rpc_resp = json.load(resp)

            result = rpc_resp.get("result", {})
            content_items = result.get("content", [])
            for item in content_items:
                if item.get("type") == "text":
                    try: return json.loads(item["text"])
                    except (json.JSONDecodeError, KeyError):
                        return {"success": True, "stdout": item.get("text", "")}

            if "error" in rpc_resp:
                return {"success": False, "error": rpc_resp["error"].get("message", "MCP error")}
            return {"success": True, "result": result}
        except Exception:
            return None

    def _get_mcp_url(self) -> str:
        servers = self.config.get("mcp_servers", {})
        laravel = servers.get("laravel", {})
        url = laravel.get("url", "http://php-ast:9003/mcp")
        import os
        if url.startswith("${") and url.endswith("}"):
            url = os.getenv(url[2:-1], "http://php-ast:9003/mcp")
        return url


def _call(url, body=None, timeout=300):
    try:
        data = json.dumps(body).encode() if body else None
        headers = {"Content-Type": "application/json"} if body else {}
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST" if body else "GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r) if 200 <= r.status < 300 else None
    except urllib.error.HTTPError as e:
        # Parse error body for diagnostic info
        try:
            return json.load(e)
        except Exception:
            return None
    except Exception:
        return None


_config_path = Path(__file__).parent / "config" / "orchestrator.yml"
orchestrator = Orchestrator(str(_config_path))
