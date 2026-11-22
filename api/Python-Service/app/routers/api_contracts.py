"""HTTP API of the contract store.

Spec: @.archcore/frontend-assist/api-contract-sources-and-versions.spec.md.

Called only by the web server, which checks the signed-in user's project access
before every call; the shared internal secret keeps other callers out. Routes
under ``/projects/{project_id}`` also verify that the source or attachment
belongs to that project, so a caller cannot reach another project's rows by ID.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse

from api_contracts.model import MAX_DOCUMENT_BYTES
from api_contracts.service import ContractInputError, contract_service
from api_contracts.store import ContractConflict, ContractNotFound
from infra.settings import settings
from telemetry.snapshot import validate_project_id

router = APIRouter(prefix="/contracts", tags=["API contracts"])

Secret = Annotated[str | None, Header(alias="X-Internal-Secret")]


def _authorize(secret: str | None) -> None:
    if not settings.internal_secret or secret != settings.internal_secret:
        raise HTTPException(status_code=401, detail="Unauthorized")


def _project(project_id: str) -> str:
    try:
        validate_project_id(project_id)
    except ValueError as exc:
        raise HTTPException(400, "A valid project ID is required.") from exc
    return project_id


def _guard(call: Any) -> Any:
    """Run a store or service call, mapping its errors to HTTP statuses."""
    try:
        return call()
    except ContractNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except ContractConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except (ContractInputError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


def _owned_source(project_id: str, source_id: str) -> dict[str, Any]:
    source = _guard(lambda: contract_service.store.get_source(source_id))
    if source["project_id"] != project_id:
        raise HTTPException(404, "No such contract source in this project.")
    return source


def _owned_attachment(project_id: str, attachment_id: str) -> dict[str, Any]:
    attachment = _guard(lambda: contract_service.store.get_attachment(attachment_id))
    if attachment["project_id"] != project_id:
        raise HTTPException(404, "No such contract attachment in this project.")
    return attachment


def _operation_rows(model: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"operation": key, "method": op["method"], "path": op["path"], "operation_id": op["operation_id"],
             "summary": op["summary"], "tags": op["tags"], "deprecated": op["deprecated"]}
            for key, op in sorted(model["operations"].items(), key=lambda item: (item[1]["path"], item[1]["method"]))]


# ── lookups the web server uses to decide access ─────────────────────────────

@router.get("/sources", summary="Every contract source, without versions")
def list_all_sources(x_internal_secret: Secret = None) -> dict[str, Any]:
    _authorize(x_internal_secret)
    return {"sources": [{key: source[key] for key in ("source_id", "project_id", "kind", "name", "status")}
                        for source in contract_service.store.list_sources()]}


@router.get("/sources/{source_id}", summary="One contract source")
def read_source(source_id: str, x_internal_secret: Secret = None) -> dict[str, Any]:
    _authorize(x_internal_secret)
    return {"source": _guard(lambda: contract_service.describe_source(contract_service.store.get_source(source_id)))}


@router.get("/attachments/{attachment_id}", summary="One contract attachment")
def read_attachment(attachment_id: str, x_internal_secret: Secret = None) -> dict[str, Any]:
    _authorize(x_internal_secret)
    return {"attachment": _guard(lambda: contract_service.describe_attachment(
        contract_service.store.get_attachment(attachment_id)))}


# ── versions (the web server checks access to the source's project) ──────────

@router.get("/sources/{source_id}/versions", summary="Versions and failed attempts of a source, newest first")
def list_versions(source_id: str, x_internal_secret: Secret = None) -> dict[str, Any]:
    _authorize(x_internal_secret)
    _guard(lambda: contract_service.store.get_source(source_id))
    return {"versions": contract_service.store.list_versions(source_id)}


@router.get("/sources/{source_id}/versions/{version_id}", summary="One version with its operations and schema names")
def read_version(source_id: str, version_id: str, x_internal_secret: Secret = None) -> dict[str, Any]:
    _authorize(x_internal_secret)
    version = _guard(lambda: contract_service.store.get_version(version_id))
    if version["source_id"] != source_id:
        raise HTTPException(404, "No such contract version.")
    model = version.pop("model", None)
    if model:
        version["base_paths"] = model["base_paths"]
        version["operations"] = _operation_rows(model)
        version["schemas"] = sorted(model["schemas"])
    return {"version": version}


@router.post("/sources/{source_id}/versions/{version_id}/generate",
             summary="Generate typed client code or mocks from a contract version")
def generate_code(source_id: str, version_id: str, body: dict, x_internal_secret: Secret = None) -> dict[str, Any]:
    _authorize(x_internal_secret)
    return _guard(lambda: contract_service.generate_code(
        source_id, version_id, body.get("target"), base_url=body.get("base_url"), search=body.get("search")))


@router.get("/sources/{source_id}/revisions/{revision_id}", summary="The contract a back-end revision carried")
def read_revision_version(source_id: str, revision_id: str, x_internal_secret: Secret = None) -> dict[str, Any]:
    _authorize(x_internal_secret)
    _guard(lambda: contract_service.store.get_source(source_id))
    return _guard(lambda: contract_service.version_for_revision(source_id, revision_id))


# ── change events (@.archcore/frontend-assist/contract-change-notifications.spec.md) ──
# The web server checks that the caller may read every project named here.

_MAX_STREAM_PROJECTS = 20


@router.get("/events/stream", summary="Live contract change events for consumer projects (server-sent events)")
async def stream_events(projects: str = "", x_internal_secret: Secret = None) -> StreamingResponse:
    _authorize(x_internal_secret)
    from api_contracts.events import change_events

    wanted = sorted({_project(part.strip()) for part in projects.split(",") if part.strip()})
    if not wanted or len(wanted) > _MAX_STREAM_PROJECTS:
        raise HTTPException(400, f"Name between 1 and {_MAX_STREAM_PROJECTS} projects.")
    return StreamingResponse(change_events(wanted), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/projects/{project_id}/events", summary="Change events of the contracts a project consumes, newest first")
def list_events(project_id: str, after: int = 0, limit: int = 50, user_id: str | None = None,
                x_internal_secret: Secret = None) -> dict[str, Any]:
    _authorize(x_internal_secret)
    return _guard(lambda: contract_service.events_for_project(_project(project_id), user_id, after_seq=max(0, after),
                                                              limit=limit))


def _project_event(project_id: str, event_id: str) -> dict[str, Any]:
    event = _guard(lambda: contract_service.store.get_event(event_id))
    if event["source_id"] not in contract_service.consumer_sources(project_id):
        raise HTTPException(404, "No such contract change for this project.")
    return event


@router.get("/projects/{project_id}/events/{event_id}", summary="One change event and what it touches in the project")
def read_event(project_id: str, event_id: str, x_internal_secret: Secret = None) -> dict[str, Any]:
    _authorize(x_internal_secret)
    _project_event(_project(project_id), event_id)
    return _guard(lambda: contract_service.event_detail(event_id, project_id))


@router.post("/projects/{project_id}/events/{event_id}/ack", summary="Mark a change event as seen by a user")
def acknowledge_event(project_id: str, event_id: str, body: dict, x_internal_secret: Secret = None) -> dict[str, Any]:
    _authorize(x_internal_secret)
    _project_event(_project(project_id), event_id)
    user = body.get("user_id")
    if not isinstance(user, str) or not user or len(user) > 200:
        raise HTTPException(400, "user_id is required.")
    _guard(lambda: contract_service.store.acknowledge(event_id, user))
    return {"acknowledged": True}


# ── one project's contracts ──────────────────────────────────────────────────

@router.get("/projects/{project_id}", summary="A project's contract sources and attachments")
def project_overview(project_id: str, x_internal_secret: Secret = None) -> dict[str, Any]:
    _authorize(x_internal_secret)
    return _guard(lambda: contract_service.project_overview(_project(project_id)))


@router.post("/projects/{project_id}/sources", summary="Create a contract source")
def create_source(project_id: str, body: dict, x_internal_secret: Secret = None) -> dict[str, Any]:
    _authorize(x_internal_secret)
    _project(project_id)

    def create() -> dict[str, Any]:
        from api_contracts.service import clean_source_input
        kind, name, config = clean_source_input(body.get("kind"), body.get("name"), body)
        user = body.get("user_id") if isinstance(body.get("user_id"), str) else None
        return contract_service.describe_source(contract_service.store.create_source(project_id, kind, name, config, user))

    return {"source": _guard(create)}


@router.delete("/projects/{project_id}/sources/{source_id}", summary="Remove a contract source and its versions")
def delete_source(project_id: str, source_id: str, x_internal_secret: Secret = None) -> dict[str, Any]:
    _authorize(x_internal_secret)
    _owned_source(project_id, source_id)
    _guard(lambda: contract_service.store.delete_source(source_id))
    return {"deleted": True}


@router.post("/projects/{project_id}/sources/{source_id}/document", summary="Upload a contract document")
async def upload_document(project_id: str, source_id: str, request: Request,
                          x_internal_secret: Secret = None) -> dict[str, Any]:
    _authorize(x_internal_secret)
    _owned_source(project_id, source_id)
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_DOCUMENT_BYTES:
        raise HTTPException(413, "The document exceeds the 10 MB limit.")
    data = await request.body()
    return await run_in_threadpool(lambda: _guard(lambda: contract_service.ingest_upload(source_id, data)))


@router.post("/projects/{project_id}/sources/{source_id}/refresh", summary="Check a URL or repository-path source now")
def refresh_source(project_id: str, source_id: str, x_internal_secret: Secret = None) -> dict[str, Any]:
    _authorize(x_internal_secret)
    _owned_source(project_id, source_id)
    return _guard(lambda: contract_service.refresh(source_id))


@router.delete("/projects/{project_id}/sources/{source_id}/versions/{version_id}", summary="Remove one version")
def delete_version(project_id: str, source_id: str, version_id: str, x_internal_secret: Secret = None) -> dict[str, Any]:
    _authorize(x_internal_secret)
    _owned_source(project_id, source_id)
    _guard(lambda: contract_service.store.delete_version(source_id, version_id))
    return {"deleted": True}


@router.post("/projects/{project_id}/attachments", summary="Attach a contract source to this project")
def create_attachment(project_id: str, body: dict, x_internal_secret: Secret = None) -> dict[str, Any]:
    _authorize(x_internal_secret)
    _project(project_id)
    source_id = body.get("source_id")
    if not isinstance(source_id, str):
        raise HTTPException(400, "source_id is required.")
    user = body.get("user_id") if isinstance(body.get("user_id"), str) else None
    attachment = _guard(lambda: contract_service.store.attach(
        source_id, project_id, body.get("role"), body.get("mode") or "follow_latest", body.get("pinned_version_id"), user))
    return {"attachment": contract_service.describe_attachment(attachment)}


@router.get("/projects/{project_id}/attachments/{attachment_id}/conformance",
            summary="Check the project's requests and models against the attached contract")
def read_conformance(project_id: str, attachment_id: str, revision_id: str | None = None,
                     x_internal_secret: Secret = None) -> dict[str, Any]:
    _authorize(x_internal_secret)
    _owned_attachment(project_id, attachment_id)
    return _guard(lambda: contract_service.conformance(attachment_id, revision_id or None))


@router.get("/projects/{project_id}/attachments/{attachment_id}/runtime",
            summary="Compare the provider's live traffic (routes and status codes) with the contract")
def read_runtime_conformance(project_id: str, attachment_id: str, revision_id: str | None = None,
                             x_internal_secret: Secret = None) -> dict[str, Any]:
    _authorize(x_internal_secret)
    _owned_attachment(project_id, attachment_id)
    return _guard(lambda: contract_service.runtime_conformance(attachment_id, revision_id or None))


@router.get("/projects/{project_id}/attachments/{attachment_id}/impact",
            summary="What a contract change touches in the project")
def read_impact(project_id: str, attachment_id: str, from_version: str | None = None, to_version: str | None = None,
                revision_id: str | None = None, x_internal_secret: Secret = None) -> dict[str, Any]:
    _authorize(x_internal_secret)
    _owned_attachment(project_id, attachment_id)
    return _guard(lambda: contract_service.impact(attachment_id, from_version or None, to_version or None, revision_id or None))


@router.put("/projects/{project_id}/attachments/{attachment_id}", summary="Follow the latest version or pin one")
def update_attachment(project_id: str, attachment_id: str, body: dict, x_internal_secret: Secret = None) -> dict[str, Any]:
    _authorize(x_internal_secret)
    _owned_attachment(project_id, attachment_id)
    attachment = _guard(lambda: contract_service.store.update_attachment(
        attachment_id, body.get("mode"), body.get("pinned_version_id")))
    return {"attachment": contract_service.describe_attachment(attachment)}


@router.delete("/projects/{project_id}/attachments/{attachment_id}", summary="Detach a contract from this project")
def delete_attachment(project_id: str, attachment_id: str, x_internal_secret: Secret = None) -> dict[str, Any]:
    _authorize(x_internal_secret)
    _owned_attachment(project_id, attachment_id)
    _guard(lambda: contract_service.store.detach(attachment_id))
    return {"deleted": True}
