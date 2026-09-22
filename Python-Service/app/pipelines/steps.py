"""Orchestration step abstraction — the run pipeline as declarative units.

Every phase of a job (extract → route/tool → post-process → doc retrieval →
framework profile → synthesize → persist) is a ``Step``.
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
    structure_profile: dict[str, Any] | None = None
    code_chunks: list[dict[str, Any]] | None = None
    code_language: str | None = None
    code_producer: str | None = None
    code_error: str | None = None
    output: Any = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Step:
    """A pipeline phase: metadata + a run hook."""

    key: str
    label: str
    run: Callable[[Any, Any, SharedState], None]


def _report_progress(
    runner: Any, ctx: Any, key: str, percent: int, label: str | None = None
) -> None:
    """Forward a live step-progress checkpoint when the runner supports it.

    The real Orchestrator owns ``_report_step_progress``; test FakeRunners that
    omit the method are untouched (progress is an optional capability, never a
    step requirement). Emitted right next to the running/done markers so every
    activated step gets at least a 5 start and a 100 end.
    """
    report = getattr(runner, "_report_step_progress", None)
    if report is None:
        return
    report(ctx, key, percent, label)


# --- steps ---------------------------------------------------------------


def _extract(runner: Any, ctx: Any, shared: SharedState) -> None:
    """Unzip the uploaded project into a scratch workspace."""
    runner._update_step(ctx.job_id, "extracting", "running")
    _report_progress(runner, ctx, "extracting", 5, "Extracting project files")
    workspace_path, file_count = runner._prepare_workspace(ctx)
    shared.workspace_path = workspace_path
    shared.file_count = file_count
    runner._update_step(
        ctx.job_id,
        "extracting",
        "done",
        file_count=file_count if file_count is not None else "Skipped",
    )
    _report_progress(runner, ctx, "extracting", 100)


def _route_tool(runner: Any, ctx: Any, shared: SharedState) -> None:
    """Pick the code-analysis tool (or LlamaIndex routing) and run it."""
    route = ctx.route or {}
    tool_name = runner._select_tool(ctx)
    if route.get("tool") in (None, "", "auto") or route.get("llamaindex", True):
        runner._update_step(ctx.job_id, "routing", "done")
        _report_progress(runner, ctx, "routing", 100)
    shared.tool_name = tool_name
    if tool_name:
        runner._update_step(ctx.job_id, "tool", "running")
        tool_label = (
            f"Running {tool_name}"
            if route.get("tool") not in (None, "", "auto")
            else "Running selected tool"
        )
        _report_progress(runner, ctx, "tool", 5, tool_label)
        tool_result = runner._call_php_tool(ctx, tool_name, shared.workspace_path)
        if not tool_result or tool_result.get("success") is False:
            error_message = None if not tool_result else tool_result.get("error")
            raise RuntimeError(error_message or f"PHP tool failed: {tool_name}")
        shared.tool_result = tool_result
        runner._update_step(ctx.job_id, "tool", "done")
        _report_progress(runner, ctx, "tool", 100)
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
        _report_progress(runner, ctx, "doc_retrieval", 5, "Fetching Library Docs")
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
        _report_progress(runner, ctx, "doc_retrieval", 100)


def _framework(runner: Any, ctx: Any, shared: SharedState) -> None:
    """Detect the framework + statically extract project facts."""
    if (ctx.route or {}).get("framework_aware"):
        runner._update_step(ctx.job_id, "framework", "running")
        _report_progress(runner, ctx, "framework", 5, "Analyzing Framework")
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
        _report_progress(runner, ctx, "framework", 100)


def _structure(runner: Any, ctx: Any, shared: SharedState) -> None:
    """Static multi-language structure census when the route opts in."""
    if not (ctx.route or {}).get("structure"):
        return
    runner._update_step(ctx.job_id, "structure", "running")
    _report_progress(runner, ctx, "structure", 5, "Analyzing Structure")
    if shared.workspace_path is None:
        shared.structure_profile = None
        runner._update_step(ctx.job_id, "structure", "done", detail="workspace missing")
        _report_progress(runner, ctx, "structure", 100)
        return
    shared.structure_profile = runner._collect_structure_profile(
        ctx, shared.workspace_path
    )
    profile = shared.structure_profile
    detail = "workspace missing" if profile is None else _structure_detail(profile)
    runner._update_step(ctx.job_id, "structure", "done", detail=detail)
    _report_progress(runner, ctx, "structure", 100)


def _structure_detail(profile: dict[str, Any] | None) -> str | None:
    """One-line census summary ``fw ver · lang · DB db`` (None when nothing)."""
    if not profile:
        return None
    summary = (
        f"{profile.get('framework') or 'no framework'} "
        f"{profile.get('version') or ''} · "
        f"{profile.get('language') or '?'} · "
        f"DB {profile.get('database') or 'not determined'}"
    )
    collapsed = " ".join(summary.split())
    return collapsed or None


def _dispatch_index(runner: Any, ctx: Any, shared: SharedState) -> None:
    """Language-dispatched code-index parse (preview; Save writes stores)."""
    if not (ctx.route or {}).get("language_dispatch"):
        return
    runner._update_step(ctx.job_id, "index", "running")
    _report_progress(runner, ctx, "index", 5, "Indexing codebase")
    if shared.workspace_path is None:
        shared.code_error = "workspace missing"
        runner._update_step(ctx.job_id, "index", "done", detail="workspace missing")
        _report_progress(runner, ctx, "index", 100)
        return
    result = runner._produce_code_chunks(ctx, shared.workspace_path)
    chunks = result.get("chunks") or []
    shared.code_chunks = chunks
    shared.code_language = result.get("language")
    shared.code_producer = result.get("producer")
    shared.code_error = result.get("error")
    error = result.get("error")
    if error:
        runner._update_step(
            ctx.job_id, "index", "done", detail=str(error)[:200]
        )
        _report_progress(runner, ctx, "index", 100)
        return
    producer = shared.code_producer or "?"
    runner._update_step(
        ctx.job_id,
        "index",
        "done",
        detail=f"{producer} · {len(chunks)} chunk(s)",
    )
    _report_progress(runner, ctx, "index", 100)


def _synthesize(runner: Any, ctx: Any, shared: SharedState) -> None:
    """Produce the final job output (LlamaIndex summary preferred).

    A language-dispatch preview (index step ran) degrades to a compact
    ``Indexed N lang chunk(s) with producer.`` summary instead of dumping the
    raw parse output (which can be megabytes of code). An honest one-liner is
    emitted when the dispatch itself failed.
    """
    synthesized_output = runner._synthesize_with_llamaindex(
        ctx=ctx,
        tool_name=shared.tool_name,
        tool_result=shared.tool_result,
        post_process_result=shared.post_process_result,
    )
    if synthesized_output:
        shared.output = synthesized_output
        return
    if shared.code_error:
        shared.output = f"No code chunks indexed: {shared.code_error}"
        return
    chunks = shared.code_chunks or []
    if chunks:
        producer = shared.code_producer or "?"
        language = shared.code_language or ""
        shared.output = (
            f"Indexed {len(chunks)} {language} chunk(s) with {producer}."
        )
        return
    shared.output = (
        synthesized_output or shared.post_process_result or shared.raw_output
    )


def _persist(runner: Any, ctx: Any, shared: SharedState) -> None:
    """Store outputs on the job, write status + the durable result bundle."""
    code_index: dict[str, Any] | None = None
    if shared.code_chunks is not None or shared.code_error:
        code_index = {
            "language": shared.code_language,
            "chunks": len(shared.code_chunks or []),
            "producer": shared.code_producer,
        }
        if shared.code_error:
            code_index["error"] = shared.code_error
    runner._save_outputs(
        ctx.job_id,
        output=shared.output,
        tool_name=shared.tool_name,
        collection_name=shared.collection_name,
        tool_result=shared.tool_result,
        tool_output=shared.post_process_result,
        doc_context=shared.doc_context,
        project_profile=shared.project_profile,
        structure_profile=shared.structure_profile,
        code_index=code_index,
    )
    runner._persist_result_bundle(
        ctx, shared.doc_context, shared.project_profile
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
    Step("structure", "Analyzing Structure", _structure),
    Step("index", "Indexing codebase", _dispatch_index),
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
    if route.get("structure"):
        steps.append({"key": "structure", "label": "Analyzing Structure", "status": "pending"})
    if route.get("language_dispatch"):
        steps.append({"key": "index", "label": "Indexing codebase", "status": "pending"})
    for action in route.get("post_process", []) or []:
        action_name = action.get("action") if isinstance(action, dict) else str(action)
        steps.append(
            {"key": f"post_{action_name}", "label": action_name.replace("_", " ").title(), "status": "pending"}
        )
    return steps
