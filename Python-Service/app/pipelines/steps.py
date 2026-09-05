"""Orchestration step abstraction — the run pipeline as declarative units.

Every phase of a job (extract → route/tool → post-process → doc retrieval →
framework profile → sandbox run → synthesize → persist) is a ``Step``.
The runner (``Orchestrator._run_job``) only iterates the plan; individual
steps own their progress markers, guards and artifact writes, reading/writing
a shared ``SharedState``.

Design rules:
  * Steps are stateless descriptors — one instance per job run.
  * They never import the runner module (no cycle): the runner passes itself
    as the capability provider (duck-typed ``Any``).
  * A step runs even when its route flag is off only to the extent the old
    inline pipeline did — guarded helpers return None cheaply and UI markers
    are only emitted when the route opts in (exact parity preserved).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SharedState:
    """Cross-step artifacts accumulated while a job runs."""

    workspace_path: str | None = None
    file_count: int | None = None
    tool_name: str | None = None
    tool_result: dict[str, Any] | None = None
    raw_output: Any = None
    post_process_result: Any = None
    collection_name: str | None = None
    doc_context: dict[str, Any] | None = None
    project_profile: dict[str, Any] | None = None
    sandbox_result: dict[str, Any] | None = None
    output: Any = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Step:
    """A pipeline phase: metadata + a run hook."""

    key: str
    label: str
    run: Callable[[Any, Any, SharedState], None]


# --- steps ---------------------------------------------------------------


def _extract(runner: Any, ctx: Any, shared: SharedState) -> None:
    """Unzip the uploaded project into a scratch workspace."""
    runner._update_step(ctx.job_id, "extracting", "running")
    workspace_path, file_count = runner._prepare_workspace(ctx)
    shared.workspace_path = workspace_path
    shared.file_count = file_count
    runner._update_step(
        ctx.job_id,
        "extracting",
        "done",
        file_count=file_count if file_count is not None else "Skipped",
    )


def _route_tool(runner: Any, ctx: Any, shared: SharedState) -> None:
    """Pick the code-analysis tool (or LlamaIndex routing) and run it."""
    tool_name = runner._select_tool(ctx)
    runner._update_step(ctx.job_id, "routing", "done")
    shared.tool_name = tool_name
    if tool_name:
        runner._update_step(ctx.job_id, "tool", "running")
        tool_result = runner._call_php_tool(tool_name, shared.workspace_path)
        if not tool_result or tool_result.get("success") is False:
            error_message = None if not tool_result else tool_result.get("error")
            raise RuntimeError(error_message or f"PHP tool failed: {tool_name}")
        shared.tool_result = tool_result
        runner._update_step(ctx.job_id, "tool", "done")
    shared.raw_output = (
        runner._extract_tool_output(shared.tool_result) if shared.tool_result else ctx.prompt
    )


def _post_process(runner: Any, ctx: Any, shared: SharedState) -> None:
    """Run the route's post_process actions (send_to_qdrant, ingest, …)."""
    post_process_result, collection_name = runner._run_post_process_pipeline(
        ctx,
        shared.raw_output,
        tool_name=shared.tool_name,
        tool_result=shared.tool_result,
    )
    shared.post_process_result = post_process_result
    shared.collection_name = collection_name


def _doc_retrieval(runner: Any, ctx: Any, shared: SharedState) -> None:
    """Version-aware library docs (Context7 → GitMCP) when route opts in."""
    if (ctx.route or {}).get("doc_retrieval"):
        runner._update_step(ctx.job_id, "doc_retrieval", "running")
    shared.doc_context = runner._collect_doc_context(
        ctx, shared.workspace_path, shared.post_process_result
    )
    if (ctx.route or {}).get("doc_retrieval"):
        detail = (
            "docs fetched"
            if shared.doc_context and shared.doc_context.get("chunks")
            else ("unavailable" if shared.doc_context else "skipped")
        )
        runner._update_step(ctx.job_id, "doc_retrieval", "done", detail=detail)


def _framework(runner: Any, ctx: Any, shared: SharedState) -> None:
    """Detect the framework + statically extract project facts."""
    if (ctx.route or {}).get("framework_aware"):
        runner._update_step(ctx.job_id, "framework", "running")
    shared.project_profile = runner._collect_project_profile(ctx, shared.workspace_path)
    if (ctx.route or {}).get("framework_aware"):
        detail = (
            (
                f"{shared.project_profile['framework']} "
                f"{shared.project_profile['version'] or ''} · "
                f"{len(shared.project_profile.get('routes', []))} route(s)"
            )
            if shared.project_profile
            else "not detected"
        )
        runner._update_step(ctx.job_id, "framework", "done", detail=detail)


def _sandbox(runner: Any, ctx: Any, shared: SharedState) -> None:
    """Stage + install + probe the project in the nested sandbox backend."""
    if (ctx.route or {}).get("sandbox"):
        runner._update_step(ctx.job_id, "sandbox", "running")
    shared.sandbox_result = runner._run_sandbox_analysis(
        ctx, shared.workspace_path, shared.project_profile
    )
    if shared.sandbox_result and shared.project_profile:
        # Runtime route:list (when the routes probe succeeded) supersedes the
        # static parser's route table — real verbs, middleware, package routes.
        shared.project_profile = runner._merge_runtime_routes(
            shared.project_profile, shared.sandbox_result
        )
    if (ctx.route or {}).get("sandbox"):
        detail = "run complete"
        if (
            shared.sandbox_result
            and shared.sandbox_result.get("install", {}).get("status") == "unavailable"
        ):
            detail = "runtime image unavailable"
        elif shared.sandbox_result is None:
            detail = "sandbox unavailable"
        runner._update_step(ctx.job_id, "sandbox", "done", detail=detail)


def _synthesize(runner: Any, ctx: Any, shared: SharedState) -> None:
    """Produce the final job output (LlamaIndex summary preferred)."""
    synthesized_output = runner._synthesize_with_llamaindex(
        ctx=ctx,
        tool_name=shared.tool_name,
        tool_result=shared.tool_result,
        post_process_result=shared.post_process_result,
    )
    shared.output = (
        synthesized_output or shared.post_process_result or shared.raw_output
    )


def _persist(runner: Any, ctx: Any, shared: SharedState) -> None:
    """Store outputs on the job, write status + the durable result bundle."""
    runner._save_outputs(
        ctx.job_id,
        output=shared.output,
        tool_name=shared.tool_name,
        collection_name=shared.collection_name,
        tool_result=shared.tool_result,
        tool_output=shared.post_process_result,
        doc_context=shared.doc_context,
        project_profile=shared.project_profile,
        sandbox=shared.sandbox_result,
    )
    runner._persist_result_bundle(
        ctx, shared.doc_context, shared.project_profile, shared.sandbox_result
    )


#: Execution order (matches the pre-abstraction inline pipeline). The routing
#: step both selects the tool and runs it (single inline block historically);
#: the UI plan (build_steps) lists routing + tool as two entries.
STEPS: tuple[Step, ...] = (
    Step("extracting", "Extracting project files", _extract),
    Step("routing", "Routing with LlamaIndex", _route_tool),
    Step("post_process", "Post-processing output", _post_process),
    Step("doc_retrieval", "Fetching Library Docs", _doc_retrieval),
    Step("framework", "Analyzing Framework", _framework),
    Step("sandbox", "Running sandbox analysis", _sandbox),
    Step("synthesize", "Synthesizing answer", _synthesize),
    Step("persist", "Saving job outputs", _persist),
)


def run_plan(
    runner: Any, ctx: Any, shared: SharedState
) -> SharedState:
    """Execute every step in order. Steps run unconditionally (their guarded
    helpers no-op off-route) so progress/state behavior matches the original
    inline pipeline exactly."""
    for step in STEPS:
        step.run(runner, ctx, shared)
    return shared


def build_steps(route: dict[str, Any]) -> list[dict[str, Any]]:
    """UI plan list — order/labels/conditions identical to the historical
    ``_build_steps`` so frontends see no change."""
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
    if route.get("doc_retrieval"):
        steps.append({"key": "doc_retrieval", "label": "Fetching Library Docs", "status": "pending"})
    if route.get("framework_aware"):
        steps.append({"key": "framework", "label": "Analyzing Framework", "status": "pending"})
    if route.get("sandbox"):
        steps.append({"key": "sandbox", "label": "Running sandbox analysis", "status": "pending"})
    for action in route.get("post_process", []) or []:
        action_name = action.get("action") if isinstance(action, dict) else str(action)
        steps.append(
            {"key": f"post_{action_name}", "label": action_name.replace("_", " ").title(), "status": "pending"}
        )
    return steps
