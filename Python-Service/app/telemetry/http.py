"""Project-scoped CI API. Disabled unless dedicated credentials are configured."""
from __future__ import annotations

import hmac
import json
import os
import re

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from telemetry.registry import SnapshotConflict, SnapshotRegistry, default_registry
from telemetry.snapshot import validate_project_id, validate_revision_id, validate_scope
from telemetry.source import MAX_ARCHIVE_BYTES, SourceService

router = APIRouter(prefix="/telemetry/projects", tags=["Telemetry snapshots"])
host_router = APIRouter(prefix="/telemetry/host", tags=["Telemetry deployments"])
runtime_router = APIRouter(prefix="/telemetry/runtime", tags=["Runtime telemetry"])
git_router = APIRouter(prefix="/telemetry/git", tags=["Git source synchronization"])
MAX_HOST_REPORT_BYTES = 2 * 1024 * 1024
MAX_PROFILE_BATCH_BYTES = 2 * 1024 * 1024
MAX_RUNTIME_SETUP_BYTES = 100 * 1024
MAX_GIT_SOURCE_REQUEST_BYTES = 16 * 1024


async def _read_git_source_json(request: Request) -> dict:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_GIT_SOURCE_REQUEST_BYTES:
            raise HTTPException(413, "Git source request exceeds its size limit.")
    try:
        payload = json.loads(body)
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, "Git source request must contain valid JSON.") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "Git source request must contain a JSON object.")
    return payload


def authorize(request: Request, project_id: str) -> None:
    try:
        credentials = json.loads(os.environ.get("TELEMETRY_CI_PROJECT_TOKENS", "{}"))
        expected = credentials.get(project_id) if isinstance(credentials, dict) else None
    except (ValueError, TypeError):
        expected = None
    header = request.headers.get("authorization", "")
    provided = header.removeprefix("Bearer ") if header.startswith("Bearer ") else ""
    if (not isinstance(expected, str) or len(expected) < 32
            or not hmac.compare_digest(provided.encode(), expected.encode())):
        raise HTTPException(401, "Valid project-scoped CI credentials required")


def get_registry() -> SnapshotRegistry:
    return default_registry()


def get_storage():
    from index.storage import storage
    return storage


def authorize_internal(request: Request) -> None:
    expected = os.environ.get("RAG_CALLBACK_SECRET", "")
    provided = request.headers.get("x-internal-secret", "")
    if (not expected or len(expected) < 32
            or not hmac.compare_digest(provided.encode(), expected.encode())):
        raise HTTPException(401, "Internal service credentials required")


def _git_user(request: Request) -> str:
    authorize_internal(request)
    user_id = request.headers.get("x-user-id", "")
    if not user_id or len(user_id) > 512:
        raise HTTPException(401, "Authenticated user identity required")
    return user_id


@git_router.get("/projects/{project_id}")
def git_source_status(project_id: str, request: Request) -> dict:
    from telemetry.git_source import GitSourceService
    user_id = _git_user(request)
    try:
        status = GitSourceService().status(user_id, project_id)
    except ValueError as exc:
        raise HTTPException(422, "Invalid project identity") from exc
    return {"source": status}


@git_router.put("/projects/{project_id}")
async def configure_git_source(project_id: str, request: Request) -> dict:
    from telemetry.git_source import GitSourceService
    user_id = _git_user(request)
    try:
        payload = await _read_git_source_json(request)
        source = GitSourceService().configure(user_id, project_id,
                                              payload.get("repository"), payload.get("branch"))
        return {"source": {key: source.get(key) for key in (
            "project_id", "repository", "branch", "status", "credential_id", "updated_at")}}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(503, "Git source configuration could not be saved.") from exc


@git_router.post("/projects/{project_id}/sync")
async def synchronize_git_source(project_id: str, request: Request) -> dict:
    from telemetry.git_source import GitSourceService, GitSyncError
    user_id = _git_user(request)
    try:
        payload = await _read_git_source_json(request)
        result = await run_in_threadpool(GitSourceService().synchronize, user_id, project_id, payload.get("token"))
        return result
    except GitSyncError as exc:
        raise HTTPException(422 if exc.code in {"git_source_missing", "git_credential_invalid"} else 503,
                            str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, "Invalid Git source identity") from exc


@git_router.get("/webhook-target")
def git_webhook_target(repository: str, request: Request) -> dict:
    authorize_internal(request)
    from telemetry.git_source import GitSourceService
    try:
        source = GitSourceService().source_for_repository(repository)
    except ValueError as exc:
        raise HTTPException(422, "Invalid repository identity") from exc
    if source is None:
        raise HTTPException(404, "No configured project uses this repository.")
    return {"project_id": source["project_id"], "user_id": source["user_id"],
            "repository": source["repository"], "branch": source["branch"],
            "credential_id": source["credential_id"]}


@runtime_router.get("/projects")
def runtime_projects(request: Request) -> list[dict]:
    authorize_internal(request)
    return get_registry().runtime_projects()


@runtime_router.post("/profiles")
async def ingest_runtime_profiles(request: Request) -> dict:
    authorize_internal(request)
    raw = await request.body()
    if len(raw) > MAX_PROFILE_BATCH_BYTES:
        raise HTTPException(413, "Profile batch exceeds the configured size limit")
    try:
        payload = json.loads(raw)
        profiles = payload.get("profiles")
        if not isinstance(profiles, list) or not profiles or not isinstance(profiles[0], dict):
            raise ValueError("Profile batch is missing entries")
        project_id = profiles[0].get("project_id")
        revision_id = profiles[0].get("revision_id")
        from telemetry.snapshot import validate_project_id, validate_revision_id
        validate_project_id(project_id)
        validate_revision_id(revision_id)
        snapshot = get_registry().get_by_revision_id(project_id, revision_id)
        if snapshot is None:
            raise HTTPException(404, "Indexed revision was not found")
        commit_sha = snapshot.get("commit_sha")
        if not isinstance(commit_sha, str):
            raise HTTPException(404, "Indexed revision source was not found")
        from telemetry.profiles import store_profiles
        from telemetry.runtime import ClickHouseRuntime
        accepted = await run_in_threadpool(
            store_profiles, ClickHouseRuntime(), project_id, revision_id, commit_sha, profiles,
        )
    except HTTPException:
        raise
    except (ValueError, TypeError, AttributeError, json.JSONDecodeError) as exc:
        raise HTTPException(422, "Profile payload is invalid") from exc
    except Exception as exc:
        raise HTTPException(503, "Profile storage is unavailable") from exc
    from telemetry.events import publish_runtime_update
    publish_runtime_update()
    return {"accepted": accepted}


@runtime_router.get("/projects/{project_id}/snapshots/{commit_sha}")
def runtime_snapshot(project_id: str, commit_sha: str, request: Request) -> dict:
    authorize_internal(request)
    try:
        validate_scope(project_id, commit_sha)
        from telemetry.runtime import project_runtime
        return project_runtime(project_id, commit_sha, get_registry())
    except ValueError as exc:
        raise HTTPException(422, "Invalid project snapshot identity") from exc


@runtime_router.get("/projects/{project_id}/revisions/{revision_id}")
def runtime_revision(project_id: str, revision_id: str, request: Request) -> dict:
    authorize_internal(request)
    try:
        validate_project_id(project_id)
        validate_revision_id(revision_id)
        from telemetry.runtime import project_runtime_by_revision
        return project_runtime_by_revision(project_id, revision_id, get_registry())
    except ValueError as exc:
        raise HTTPException(422, "Invalid indexed revision identity") from exc
    except KeyError as exc:
        raise HTTPException(404, "Indexed revision was not found") from exc


@runtime_router.get("/projects/{project_id}/revisions/{revision_id}/requests")
def runtime_requests(project_id: str, revision_id: str, request: Request, outcome: str = "all") -> dict:
    authorize_internal(request)
    from telemetry.runtime import REQUEST_OUTCOMES, ClickHouseRuntime
    if outcome not in REQUEST_OUTCOMES:
        raise HTTPException(422, "Request outcome filter must be all, errors, or slow")
    try:
        validate_project_id(project_id)
        validate_revision_id(revision_id)
        if get_registry().get_by_revision_id(project_id, revision_id) is None:
            raise HTTPException(404, "Indexed revision was not found")
        return ClickHouseRuntime().requests(project_id, revision_id, outcome)
    except ValueError as exc:
        raise HTTPException(422, "Invalid indexed revision identity") from exc


@runtime_router.get("/projects/{project_id}/revisions/{revision_id}/requests/{trace_id}")
def runtime_request_detail(project_id: str, revision_id: str, trace_id: str, request: Request) -> dict:
    authorize_internal(request)
    try:
        validate_project_id(project_id)
        validate_revision_id(revision_id)
        if not re.fullmatch(r"[0-9a-fA-F]{32}", trace_id):
            raise ValueError("Invalid trace ID")
        if get_registry().get_by_revision_id(project_id, revision_id) is None:
            raise HTTPException(404, "Indexed revision was not found")
        from telemetry.runtime import ClickHouseRuntime
        return ClickHouseRuntime().request_detail(project_id, revision_id, trace_id)
    except ValueError as exc:
        raise HTTPException(422, "Invalid request trace identity") from exc


@runtime_router.post("/projects/{project_id}/revisions/{revision_id}/requests/{trace_id}/analysis")
async def runtime_request_analysis(
    project_id: str, revision_id: str, trace_id: str, request: Request, payload: dict = Body(...),
) -> dict:
    authorize_internal(request)
    try:
        validate_project_id(project_id)
        validate_revision_id(revision_id)
        if not re.fullmatch(r"[0-9a-fA-F]{32}", trace_id):
            raise ValueError("Invalid trace ID")
        registry = get_registry()
        snapshot = registry.get_by_revision_id(project_id, revision_id)
        if snapshot is None:
            raise HTTPException(404, "Indexed revision was not found")
        provider = payload.get("provider") if isinstance(payload, dict) else None
        if provider not in {"local", "remote"}:
            raise HTTPException(422, "Choose a valid configured chat provider")
        from telemetry.runtime import ClickHouseRuntime, snapshot_definitions
        from telemetry.analysis import AnalysisError, analyze_request
        detail = await run_in_threadpool(ClickHouseRuntime().request_detail, project_id, revision_id, trace_id)
        revision = {**snapshot, "project_id": project_id, "archive_key": registry.archive_key(project_id, snapshot["commit_sha"])}
        if not revision.get("archive_key"):
            # Partial telemetry analysis remains available without source.
            revision.pop("archive_key", None)
        result = await run_in_threadpool(
            analyze_request, project_id, revision, detail, provider,
            definitions_loader=snapshot_definitions,
            archive_loader=get_storage().get_snapshot_archive,
        )
        return result
    except HTTPException:
        raise
    except ValueError as exc:
        from telemetry.analysis import AnalysisError
        if isinstance(exc, AnalysisError):
            raise HTTPException(502, str(exc)) from exc
        raise HTTPException(422, "Invalid request analysis identity") from exc
    except Exception as exc:
        raise HTTPException(503, "Request analysis is temporarily unavailable. Retry the request.") from exc


@runtime_router.post("/projects/{project_id}/revisions/{revision_id}/requests/{trace_id}/analysis/diff")
async def runtime_request_diff_preview(
    project_id: str, revision_id: str, trace_id: str, request: Request, payload: dict = Body(...),
) -> dict:
    authorize_internal(request)
    try:
        validate_project_id(project_id)
        validate_revision_id(revision_id)
        if not re.fullmatch(r"[0-9a-fA-F]{32}", trace_id):
            raise ValueError("Invalid trace ID")
        if len(json.dumps(payload, ensure_ascii=False)) > 100_000:
            raise HTTPException(413, "Selected finding exceeds the request limit")
        registry = get_registry()
        snapshot = registry.get_by_revision_id(project_id, revision_id)
        if snapshot is None:
            raise HTTPException(404, "Indexed revision was not found")
        from telemetry.analysis import preview_diff
        from telemetry.runtime import ClickHouseRuntime
        detail = await run_in_threadpool(ClickHouseRuntime().request_detail, project_id, revision_id, trace_id)
        revision = {**snapshot, "project_id": project_id,
                    "archive_key": registry.archive_key(project_id, snapshot["commit_sha"])}
        result = await run_in_threadpool(
            preview_diff, revision, payload.get("finding"), payload.get("analysis") or {}, detail,
            archive_loader=get_storage().get_snapshot_archive,
        )
        return result
    except HTTPException:
        raise
    except ValueError as exc:
        from telemetry.analysis import AnalysisError
        if isinstance(exc, AnalysisError):
            raise HTTPException(422, str(exc)) from exc
        raise HTTPException(422, "Invalid request analysis identity") from exc
    except Exception as exc:
        raise HTTPException(503, "Diff preview is temporarily unavailable. Retry the request.") from exc


@runtime_router.delete("/projects/{project_id}/revisions/{revision_id}/requests")
def runtime_delete_requests(
    project_id: str,
    revision_id: str,
    request: Request,
    payload: dict = Body(...),
) -> dict:
    authorize_internal(request)
    try:
        validate_project_id(project_id)
        validate_revision_id(revision_id)
        if get_registry().get_by_revision_id(project_id, revision_id) is None:
            raise HTTPException(404, "Indexed revision was not found")
        trace_ids = payload.get("trace_ids") if isinstance(payload, dict) else None
        if not isinstance(trace_ids, list) or not 1 <= len(trace_ids) <= 50:
            raise ValueError("Select between 1 and 50 request traces")
        from telemetry.runtime import ClickHouseRuntime
        result = ClickHouseRuntime().delete_requests(project_id, revision_id, trace_ids)
        from telemetry.events import publish_runtime_update
        publish_runtime_update()
        return result
    except ValueError as exc:
        raise HTTPException(422, "Invalid request trace selection") from exc
    except KeyError as exc:
        raise HTTPException(404, "Indexed revision was not found") from exc
    except RuntimeError as exc:
        raise HTTPException(503, "Request telemetry could not be deleted") from exc


@runtime_router.get("/settings")
def runtime_settings(request: Request) -> dict:
    authorize_internal(request)
    from telemetry.runtime import telemetry_settings
    return telemetry_settings(get_registry())


@runtime_router.put("/settings")
async def update_runtime_settings(request: Request) -> dict:
    authorize_internal(request)
    try:
        payload = await request.json()
        from telemetry.runtime import telemetry_settings, update_runtime_sampling_policy
        update_runtime_sampling_policy(payload, get_registry())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    try:
        from telemetry.policy import refresh_sampling_policy
        policy = await run_in_threadpool(refresh_sampling_policy, get_registry())
    except Exception:
        policy = {"pending": True}
    return {**telemetry_settings(get_registry()), "policy_refresh": policy}


async def _runtime_setup_payload(request: Request) -> dict:
    raw = await request.body()
    if len(raw) > MAX_RUNTIME_SETUP_BYTES:
        raise HTTPException(413, "Remote setup request is too large")
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, "Remote setup request is invalid") from exc
    if not isinstance(payload, dict):
        raise HTTPException(422, "Remote setup request must be an object")
    return payload


@runtime_router.get("/settings/storage")
def runtime_storage_settings(request: Request) -> dict:
    authorize_internal(request)
    from telemetry.runtime_storage import storage_status
    return storage_status(get_registry())


@runtime_router.get("/host-metrics")
def runtime_host_metrics(request: Request) -> dict:
    authorize_internal(request)
    try:
        from telemetry.host_metrics import collect_host_metrics
        return collect_host_metrics(get_registry())
    except Exception as exc:
        raise HTTPException(503, "Runtime host metrics are unavailable") from exc


@runtime_router.put("/settings/storage/mode")
async def update_runtime_storage_mode(request: Request) -> dict:
    authorize_internal(request)
    payload = await _runtime_setup_payload(request)
    try:
        from telemetry.runtime_storage import set_storage_mode
        target = await run_in_threadpool(set_storage_mode, payload.get("mode"), get_registry())
        restart_requested = target.pop("collector_restart_requested", False)
        return {"target": target, "collector_restart_requested": restart_requested}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        mode = payload.get("mode")
        if mode in {"disabled", "local", "remote"}:
            detail = (
                "Runtime storage mode could not be applied. Start and verify its services with "
                f"`just runtime-storage {mode}`, then retry."
            )
        else:
            detail = "Runtime storage mode could not be applied."
        raise HTTPException(503, detail) from exc


@runtime_router.post("/settings/storage/host-key")
async def runtime_storage_host_key(request: Request) -> dict:
    authorize_internal(request)
    payload = await _runtime_setup_payload(request)
    try:
        from telemetry.runtime_provisioning import scan_host_key
        return await scan_host_key(payload)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, "SSH host fingerprint could not be retrieved") from exc


@runtime_router.post("/settings/storage/test")
async def runtime_storage_test(request: Request) -> dict:
    authorize_internal(request)
    payload = await _runtime_setup_payload(request)
    try:
        from telemetry.runtime_provisioning import HostKeyMismatch, test_ssh_and_sudo
        return await test_ssh_and_sudo(payload)
    except HostKeyMismatch as exc:
        raise HTTPException(409, "SSH host fingerprint changed. Retrieve and confirm the new fingerprint before continuing.") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc


@runtime_router.post("/settings/storage/install")
async def runtime_storage_install(request: Request) -> dict:
    authorize_internal(request)
    payload = await _runtime_setup_payload(request)
    try:
        from telemetry.runtime_provisioning import HostKeyMismatch, install_clickhouse
        return await install_clickhouse(payload, get_registry())
    except HostKeyMismatch as exc:
        raise HTTPException(409, "SSH host fingerprint changed. Retrieve and confirm the new fingerprint before continuing.") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(503, "Remote ClickHouse provisioning failed; the previous Runtime target remains active") from exc


@runtime_router.get("/events")
async def runtime_update_events(request: Request):
    authorize_internal(request)
    from telemetry.events import runtime_events
    return StreamingResponse(
        runtime_events(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def check_request(request: Request, project_id: str, commit_sha: str) -> None:
    authorize(request, project_id)
    try:
        validate_scope(project_id, commit_sha)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/{project_id}/snapshots/{commit_sha}")
def status(project_id: str, commit_sha: str, request: Request) -> dict:
    check_request(request, project_id, commit_sha)
    return get_registry().get(project_id, commit_sha)


@router.put("/{project_id}/snapshots/{commit_sha}/source")
async def upload(project_id: str, commit_sha: str, request: Request) -> dict:
    check_request(request, project_id, commit_sha)
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/zip":
        raise HTTPException(415, "Send repository-root ZIP bytes as application/zip")
    data = bytearray()
    async for block in request.stream():
        if len(data) + len(block) > MAX_ARCHIVE_BYTES:
            raise HTTPException(413, "Source archive exceeds limit")
        data.extend(block)
    service = SourceService(get_registry(), get_storage())
    try:
        result = await run_in_threadpool(service.upload, project_id, commit_sha, bytes(data))
    except SnapshotConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return result


def _host_scopes() -> dict:
    try:
        value = json.loads(os.environ.get("TELEMETRY_HOST_SCOPES", "{}"))
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def _host_identity(request: Request) -> tuple[str, dict]:
    # Ingress must remove this header from client requests and set it only from
    # the verified mTLS peer certificate.
    fingerprint = request.headers.get("x-verified-client-cert-sha256", "").lower().replace(":", "")
    scopes = _host_scopes()
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint) or fingerprint not in scopes:
        raise HTTPException(401, "Verified host certificate required")
    configured = scopes[fingerprint]
    if not isinstance(configured, dict):
        raise HTTPException(403, "Host has no deployment scope")
    return fingerprint, configured


@host_router.post("/deployments")
async def report_deployments(request: Request) -> dict:
    host_id, scope = _host_identity(request)
    payload = bytearray()
    async for block in request.stream():
        if len(payload) + len(block) > MAX_HOST_REPORT_BYTES:
            raise HTTPException(413, "Deployment report exceeds limit")
        payload.extend(block)
    try:
        body = json.loads(payload)
        containers = body["containers"]
        inventory = body.get("inventory", False)
    except (ValueError, KeyError, TypeError):
        raise HTTPException(422, "Invalid deployment report")
    if not isinstance(containers, list) or len(containers) > 2000 or not isinstance(inventory, bool):
        raise HTTPException(422, "Invalid deployment report")
    allowed = scope.get("projects", {})
    if not isinstance(allowed, dict):
        raise HTTPException(403, "Host has no deployment scope")
    clean = []
    for item in containers:
        if not isinstance(item, dict):
            raise HTTPException(422, "Invalid deployment record")
        # Deliberately project only the approved fields; never persist raw Docker labels.
        record = {name: item.get(name) for name in
                  ("container_id", "project_id", "environment", "service", "commit_sha", "image_id", "state")}
        try:
            if (not isinstance(record["container_id"], str) or not re.fullmatch(r"[0-9a-f]{12,64}", record["container_id"])
                    or not isinstance(record["image_id"], str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", record["image_id"])
                    or record["state"] not in {"running", "stopped"}):
                raise ValueError
            validate_scope(record["project_id"], record["commit_sha"])
            if not isinstance(record["environment"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", record["environment"]):
                raise ValueError
            if not isinstance(record["service"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", record["service"]):
                raise ValueError
        except (ValueError, TypeError):
            raise HTTPException(422, "Invalid deployment record")
        envs = allowed.get(record["project_id"], [])
        if not isinstance(envs, list) or not all(isinstance(value, str) for value in envs):
            raise HTTPException(403, "Host has no deployment scope")
        if record["environment"] not in envs:
            raise HTTPException(403, "Deployment is outside host scope")
        clean.append(record)
    event_id = body.get("event_id")
    registry = get_registry()
    if event_id is not None:
        if not isinstance(event_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", event_id):
            raise HTTPException(422, "Invalid event ID")
        if registry.has_event(host_id, event_id):
            return {"accepted": True, "duplicate": True}
    pairs = registry.report_deployments(host_id, clean, inventory=inventory)
    from telemetry.events import publish_runtime_update
    publish_runtime_update()
    queued = sum(registry.queue_if_deployable(project, commit) for project, commit in pairs)
    if event_id is not None:
        registry.mark_event(host_id, event_id)
    return {"accepted": True, "containers": len(clean), "queued": queued}
