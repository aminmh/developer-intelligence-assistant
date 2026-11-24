"""Authenticated API surface for saved code quality assessments."""
from __future__ import annotations

import hmac
import json
import logging
import os

from fastapi import APIRouter, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from code_quality.service import (
    AssessmentError,
    AssessmentModelUnavailable,
    AssessmentReportStoreUnavailable,
    ReportStore,
    assess_revision,
    model_failure_diagnostic,
)
from code_quality.performance import (
    PerformanceBusy,
    PerformanceError,
    PerformanceStore,
    benchmark_traces,
    cancel_job,
    compare_runs,
    export_script,
    live_progress,
    start_run,
)
from uuid import UUID
from code_quality.rules import CustomRuleStore, MAX_RULE_BYTES
from code_quality.analyzers import validate_semgrep_yaml
from code_quality.service import preview_custom_rule
from code_quality.source_view import (
    SourceViewError,
    list_indexed_source_directories,
    list_source_directories,
    MAX_DIRECTORY_ARCHIVE_BYTES,
    read_source_window,
)
from code_quality.business_rules import BusinessRuleStore
from code_quality.outline import OutlineConnectionStore, OutlineError, discover_collections, fetch_business_rule_documents
from code_quality.external_analyzers import (
    DeveloperModeStore,
    ExternalAnalyzerError,
    ExternalAnalyzerSettingsStore,
    capability_snapshot,
    developer_mode_enabled,
    deployment_mode,
    effective_worker_target,
    normalize_settings,
)
from code_quality.remote_worker import (
    RemoteWorkerError,
    check_sonarqube_access as check_worker_sonarqube_access,
    check_worker,
    get_worker_fingerprint,
)
from telemetry.snapshot import source_reference, validate_project_id, validate_revision_id
from code_quality.revision_deletion import RevisionBusy, RevisionStorageUnavailable, remove_revision

router = APIRouter(prefix="/code-quality", tags=["Code quality assessment"])
MAX_PERFORMANCE_REQUEST_BYTES = 100_000
MAX_EXCLUSION_REQUEST_BYTES = 32_000
logger = logging.getLogger(__name__)
MAX_RULE_REQUEST_BYTES = MAX_RULE_BYTES + 2_000
MAX_BUSINESS_RULE_REQUEST_BYTES = 4_000
MAX_OUTLINE_REQUEST_BYTES = 12_000
MAX_EXTERNAL_ANALYZER_REQUEST_BYTES = 8_000
MAX_DEVELOPER_MODE_REQUEST_BYTES = 2_000


def _authorize(request: Request) -> str:
    expected = os.environ.get("RAG_CALLBACK_SECRET", "")
    supplied = request.headers.get("x-internal-secret", "")
    if (not expected or len(expected) < 32
            or not hmac.compare_digest(supplied.encode(), expected.encode())):
        raise HTTPException(401, "Internal service credentials required")
    user_id = request.headers.get("x-user-id", "")
    if not user_id or len(user_id) > 512:
        raise HTTPException(401, "Authenticated user identity required")
    return user_id


async def _read_business_rule_payload(request: Request) -> dict:
    raw = await request.body()
    if len(raw) > MAX_BUSINESS_RULE_REQUEST_BYTES:
        raise HTTPException(413, "Business rule request exceeds the size limit.")
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, "Business rule request must be valid JSON.") from exc
    if not isinstance(payload, dict):
        raise HTTPException(422, "Business rule settings are invalid.")
    return payload


async def _read_outline_payload(request: Request) -> dict:
    raw = await request.body()
    if not raw:
        return {}
    if len(raw) > MAX_OUTLINE_REQUEST_BYTES:
        raise HTTPException(413, "Outline request exceeds the size limit.")
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, "Outline request must be valid JSON.") from exc
    if not isinstance(payload, dict):
        raise HTTPException(422, "Outline settings are invalid.")
    return payload


def _developer_mode_snapshot() -> dict:
    mode = deployment_mode()
    enabled = developer_mode_enabled()
    if mode == "production":
        reason = "Production Mode is locked to its deployment-installed local analyzer worker."
    elif mode != "developer":
        reason = "APP_DEPLOYMENT_MODE must be developer or production."
    elif not enabled:
        reason = "Developer Mode remote analyzers are disabled by the global administrator."
    else:
        reason = None
    return {"deployment_mode": mode, "enabled": enabled,
            "locked": mode != "developer", "reason": reason}


@router.get("/projects/{project_id}/outline")
def get_outline_connection(project_id: str, request: Request) -> dict:
    _authorize(request)
    try:
        validate_project_id(project_id)
    except ValueError as exc:
        raise HTTPException(422, "Invalid project identity.") from exc
    connection = OutlineConnectionStore().get(project_id)
    return {"connection": connection}


@router.put("/projects/{project_id}/outline")
async def put_outline_connection(project_id: str, request: Request) -> dict:
    _authorize(request)
    try:
        validate_project_id(project_id)
        payload = await _read_outline_payload(request)
        connection = OutlineConnectionStore().save(project_id, payload)
        return {"connection": connection}
    except HTTPException:
        raise
    except OutlineError as exc:
        raise HTTPException(422, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, "Invalid project identity.") from exc


@router.delete("/projects/{project_id}/outline")
def delete_outline_connection(project_id: str, request: Request) -> dict:
    _authorize(request)
    try:
        validate_project_id(project_id)
    except ValueError as exc:
        raise HTTPException(422, "Invalid project identity.") from exc
    return {"connection": OutlineConnectionStore().delete(project_id)}


@router.get("/projects/{project_id}/external-analyzers")
def get_external_analyzer_settings(project_id: str, request: Request) -> dict:
    _authorize(request)
    try:
        validate_project_id(project_id)
    except ValueError as exc:
        raise HTTPException(422, "Invalid project identity.") from exc
    settings = ExternalAnalyzerSettingsStore().get(project_id)
    return {"settings": settings, "deployment_mode": deployment_mode(),
            "capabilities": capability_snapshot(settings)}


@router.get("/developer-mode")
def get_developer_mode(request: Request) -> dict:
    _authorize(request)
    return _developer_mode_snapshot()


@router.put("/developer-mode")
async def put_developer_mode(request: Request) -> dict:
    _authorize(request)
    raw = await request.body()
    if len(raw) > MAX_DEVELOPER_MODE_REQUEST_BYTES:
        raise HTTPException(413, "Developer Mode settings exceed the size limit.")
    try:
        payload = json.loads(raw or b"{}")
        if not isinstance(payload, dict) or set(payload) != {"enabled"}:
            raise ExternalAnalyzerError("Developer Mode settings must contain only an enabled boolean.")
        DeveloperModeStore().set_enabled(payload["enabled"])
        return _developer_mode_snapshot()
    except ExternalAnalyzerError as exc:
        status_code = 409 if deployment_mode() == "production" else 422
        raise HTTPException(status_code, str(exc)) from exc
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(422, "Developer Mode settings are invalid.") from exc


@router.post("/projects/{project_id}/external-analyzers/worker/check")
async def check_external_analyzer_worker(project_id: str, request: Request) -> dict:
    _authorize(request)
    try:
        validate_project_id(project_id)
        if deployment_mode() == "invalid":
            raise HTTPException(503, "APP_DEPLOYMENT_MODE must be developer or production.")
        if deployment_mode() == "developer" and not developer_mode_enabled():
            raise HTTPException(409, "Developer Mode remote analyzers are disabled by the global administrator.")
        raw = await request.body()
        if len(raw) > 4_000:
            raise HTTPException(413, "Worker settings exceed the size limit.")
        payload = json.loads(raw or b"{}")
        if not isinstance(payload, dict):
            raise HTTPException(422, "Worker settings are invalid.")
        if deployment_mode() == "production":
            target = effective_worker_target({})
            if target is None:
                raise HTTPException(503, "The production analyzer worker is not installed or its TLS identity is missing.")
        else:
            submitted_worker = payload.get("worker", {})
            if not isinstance(submitted_worker, dict):
                raise ExternalAnalyzerError("Worker settings must be an object.")
            # This endpoint checks capabilities only and never transfers source.
            # Keep it independent from the project's separate source-consent gate.
            normalized = normalize_settings({"worker": {
                **submitted_worker,
                "enabled": False,
                "source_transfer_consent": False,
            }})
            worker = normalized["worker"]
            if not worker["endpoint"] or not worker["certificate_sha256"]:
                raise ExternalAnalyzerError("Enter the worker HTTPS address and certificate fingerprint before testing.")
            target = {"enabled": True, "endpoint": worker["endpoint"],
                      "certificate_sha256": worker["certificate_sha256"],
                      "source_transfer_consent": False,
                      "deployment_owned": False}
        return check_worker(target)
    except HTTPException:
        raise
    except (ExternalAnalyzerError, RemoteWorkerError) as exc:
        raise HTTPException(422, str(exc)) from exc
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(422, "Worker settings are invalid.") from exc


@router.post("/projects/{project_id}/external-analyzers/worker/fingerprint")
async def get_external_analyzer_worker_fingerprint(project_id: str, request: Request) -> dict:
    _authorize(request)
    try:
        validate_project_id(project_id)
        mode = deployment_mode()
        if mode == "invalid":
            raise HTTPException(503, "APP_DEPLOYMENT_MODE must be developer or production.")
        if mode != "developer":
            raise HTTPException(409, "Remote worker fingerprint discovery is available only in Developer Mode.")
        if not developer_mode_enabled():
            raise HTTPException(409, "Developer Mode remote analyzers are disabled by the global administrator.")
        raw = await request.body()
        if len(raw) > 4_000:
            raise HTTPException(413, "Worker settings exceed the size limit.")
        payload = json.loads(raw or b"{}")
        if not isinstance(payload, dict) or set(payload) != {"endpoint"}:
            raise HTTPException(422, "Provide only the analyzer worker HTTPS endpoint.")
        normalized = normalize_settings({"worker": {
            "endpoint": payload.get("endpoint"),
            "enabled": False,
            "source_transfer_consent": False,
        }})["worker"]
        if not normalized["endpoint"]:
            raise ExternalAnalyzerError("Enter the worker HTTPS address before fetching its fingerprint.")
        return await run_in_threadpool(get_worker_fingerprint, normalized["endpoint"])
    except HTTPException:
        raise
    except (ExternalAnalyzerError, RemoteWorkerError) as exc:
        raise HTTPException(422, str(exc)) from exc
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(422, "Worker endpoint is invalid.") from exc


@router.post("/projects/{project_id}/external-analyzers/sonarqube/check")
async def check_external_sonarqube_access(project_id: str, request: Request) -> dict:
    _authorize(request)
    try:
        validate_project_id(project_id)
        if deployment_mode() == "invalid":
            raise HTTPException(503, "APP_DEPLOYMENT_MODE must be developer or production.")
        if deployment_mode() == "developer" and not developer_mode_enabled():
            raise HTTPException(409, "Developer Mode remote analyzers are disabled by the global administrator.")
        raw = await request.body()
        if len(raw) > 16_000:
            raise HTTPException(413, "SonarQube access-check request exceeds the size limit.")
        payload = json.loads(raw or b"{}")
        if (not isinstance(payload, dict)
                or set(payload) - {"server_url", "project_key", "sonarqube_token", "worker"}):
            raise HTTPException(422, "SonarQube access-check settings are invalid.")
        token = payload.get("sonarqube_token")
        if not isinstance(token, str) or not token.strip() or len(token) > 8192:
            raise HTTPException(422, "Save a valid SonarQube token before testing access.")

        store = ExternalAnalyzerSettingsStore()
        saved = store.get(project_id)
        sonar = normalize_settings({"sonarqube": {
            "server_url": payload.get("server_url"),
            "project_key": payload.get("project_key"),
            "enabled": False,
            "source_upload_consent": False,
        }}, saved)["sonarqube"]

        if deployment_mode() == "production":
            target = effective_worker_target(saved)
            if target is None:
                raise HTTPException(503, "The production analyzer worker is not installed or its TLS identity is missing.")
        else:
            submitted_worker = payload.get("worker", saved.get("worker", {}))
            if not isinstance(submitted_worker, dict):
                raise ExternalAnalyzerError("Worker settings must be an object.")
            normalized_worker = normalize_settings({"worker": {
                **submitted_worker,
                "enabled": False,
                "source_transfer_consent": False,
            }}, saved)["worker"]
            if not normalized_worker["endpoint"] or not normalized_worker["certificate_sha256"]:
                raise ExternalAnalyzerError("Configure the worker HTTPS address and certificate fingerprint before testing SonarQube access.")
            target = {"enabled": True, "endpoint": normalized_worker["endpoint"],
                      "certificate_sha256": normalized_worker["certificate_sha256"],
                      "source_transfer_consent": False, "deployment_owned": False}

        return await run_in_threadpool(
            check_worker_sonarqube_access, target, sonar["server_url"], sonar["project_key"], token,
        )
    except HTTPException:
        raise
    except (ExternalAnalyzerError, RemoteWorkerError) as exc:
        raise HTTPException(422, str(exc)) from exc
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(422, "SonarQube access-check settings are invalid.") from exc


@router.put("/projects/{project_id}/external-analyzers")
async def put_external_analyzer_settings(project_id: str, request: Request) -> dict:
    _authorize(request)
    try:
        validate_project_id(project_id)
        raw = await request.body()
        if len(raw) > MAX_EXTERNAL_ANALYZER_REQUEST_BYTES:
            raise HTTPException(413, "External analyzer settings exceed the size limit.")
        payload = json.loads(raw)
        store = ExternalAnalyzerSettingsStore()
        settings = normalize_settings(payload, store.get(project_id))
        target = effective_worker_target(settings)
        if (deployment_mode() == "developer" and developer_mode_enabled()
                and settings.get("worker", {}).get("enabled")):
            try:
                check_worker(target)
            except RemoteWorkerError as exc:
                raise HTTPException(422, str(exc)) from exc
        settings = store.save(project_id, settings)
        return {"settings": settings, "capabilities": capability_snapshot(settings)}
    except HTTPException:
        raise
    except ExternalAnalyzerError as exc:
        raise HTTPException(422, str(exc)) from exc
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(422, "External analyzer settings are invalid.") from exc


@router.post("/projects/{project_id}/outline/collections")
async def get_outline_collections(project_id: str, request: Request) -> dict:
    _authorize(request)
    try:
        validate_project_id(project_id)
        payload = await _read_outline_payload(request)
        collections = await run_in_threadpool(
            discover_collections, payload.get("instance_url"), payload.get("token"),
        )
        return {"collections": collections}
    except HTTPException:
        raise
    except OutlineError as exc:
        raise HTTPException(422, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, "Invalid project identity.") from exc
    except Exception as exc:
        logger.warning("code_quality_outline_collection_list_failed project=%s error_type=%s",
                       project_id, type(exc).__name__)
        raise HTTPException(503, "Outline collections could not be loaded. Check the connection and retry.") from exc


@router.post("/projects/{project_id}/outline/documents")
async def get_outline_documents(project_id: str, request: Request) -> dict:
    _authorize(request)
    try:
        validate_project_id(project_id)
        payload = await _read_outline_payload(request)
        connection = OutlineConnectionStore().get(project_id)
        if not connection:
            raise HTTPException(404, "Connect an Outline collection before loading business rules.")
        documents = await run_in_threadpool(
            fetch_business_rule_documents, connection, payload.get("token"),
        )
        return {"documents": documents}
    except HTTPException:
        raise
    except OutlineError as exc:
        raise HTTPException(422, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, "Invalid project identity.") from exc
    except Exception as exc:
        logger.warning("code_quality_outline_document_list_failed project=%s error_type=%s",
                       project_id, type(exc).__name__)
        raise HTTPException(503, "Outline documents could not be loaded. Check the connection and retry.") from exc


@router.get("/projects/{project_id}/business-rules")
def list_business_rules(project_id: str, request: Request) -> dict:
    _authorize(request)
    try:
        validate_project_id(project_id)
    except ValueError as exc:
        raise HTTPException(422, "Invalid project identity.") from exc
    return {"rules": BusinessRuleStore().list(project_id)}


@router.post("/projects/{project_id}/business-rules")
async def create_business_rule(project_id: str, request: Request) -> dict:
    _authorize(request)
    try:
        validate_project_id(project_id)
        payload = await _read_business_rule_payload(request)
        return {"rule": BusinessRuleStore().save(project_id, payload)}
    except HTTPException:
        raise
    except AssessmentError as exc:
        raise HTTPException(422, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, "Invalid project identity.") from exc


@router.put("/projects/{project_id}/business-rules/{rule_id}")
async def update_business_rule(project_id: str, rule_id: str, request: Request) -> dict:
    _authorize(request)
    try:
        validate_project_id(project_id)
        payload = await _read_business_rule_payload(request)
        rule = BusinessRuleStore().save(project_id, payload, rule_id)
    except HTTPException:
        raise
    except AssessmentError as exc:
        raise HTTPException(422, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, "Invalid project identity.") from exc
    if rule is None:
        raise HTTPException(404, "Business rule was not found.")
    return {"rule": rule}


@router.delete("/projects/{project_id}/business-rules/{rule_id}")
def delete_business_rule(project_id: str, rule_id: str, request: Request) -> dict:
    _authorize(request)
    try:
        validate_project_id(project_id)
    except ValueError as exc:
        raise HTTPException(422, "Invalid project identity.") from exc
    if not BusinessRuleStore().delete(project_id, rule_id):
        raise HTTPException(404, "Business rule was not found.")
    return {"deleted": True}


# ----- Business rule knowledge: synced Outline index, rule <-> code links and developer aids -----

MAX_RULE_KNOWLEDGE_REQUEST_BYTES = 8_000
MAX_RULE_IMPACT_REQUEST_BYTES = 220_000


async def _read_json(request: Request, limit: int) -> dict:
    raw = await request.body()
    if not raw:
        return {}
    if len(raw) > limit:
        raise HTTPException(413, "The request exceeds the size limit.")
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, "The request must be valid JSON.") from exc
    if not isinstance(payload, dict):
        raise HTTPException(422, "The request body must be an object.")
    return payload


def _rule_revision(project_id: str, revision_id) -> "object":
    from code_quality.rule_code import load_revision
    validate_revision_id(revision_id)
    return load_revision(project_id, revision_id)


def _rule_error(exc: Exception, action: str, project_id: str) -> HTTPException:
    from code_quality.rule_code import RevisionUnavailable
    from code_quality.rule_index import RuleIndexError
    if isinstance(exc, HTTPException):
        return exc
    from code_quality.rule_assist import RuleModelUnavailable
    if isinstance(exc, RevisionUnavailable):
        return HTTPException(409, str(exc))
    if isinstance(exc, RuleModelUnavailable):
        return HTTPException(503, str(exc))
    if isinstance(exc, (RuleIndexError, OutlineError)):
        return HTTPException(422, str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(422, "Invalid project or revision identity.")
    logger.warning("business_rule_%s_failed project=%s error_type=%s", action, project_id, type(exc).__name__)
    return HTTPException(503, f"Business rule {action.replace('_', ' ')} failed. Check the API logs and retry.")


@router.get("/outline/connections")
def list_outline_connections(request: Request) -> dict:
    """Projects with an Outline collection, for the Node scheduler that keeps the rule index fresh."""
    _authorize(request)
    return {"connections": [{"project_id": item["project_id"], "credential_id": item["credential_id"]}
                            for item in OutlineConnectionStore().list()]}


@router.post("/projects/{project_id}/outline/sync")
async def sync_outline_rules(project_id: str, request: Request) -> dict:
    _authorize(request)
    from code_quality.outline import fetch_business_rules
    from code_quality.rule_index import RuleIndex
    try:
        validate_project_id(project_id)
        payload = await _read_outline_payload(request)
        connection = OutlineConnectionStore().get(project_id)
        if not connection:
            raise HTTPException(404, "Connect an Outline collection before syncing business rules.")
        index = RuleIndex()
        try:
            rules, summary = await run_in_threadpool(fetch_business_rules, connection, payload.get("token"))
        except OutlineError as exc:
            index.record_failure(project_id, str(exc))
            raise
        result = await run_in_threadpool(index.sync, project_id, rules, summary, connection.get("instance_url"))
        changed = [event["doc_id"] for event in result["events"]]
        if changed:
            from code_quality.rule_search import refresh_rule_vectors
            await run_in_threadpool(refresh_rule_vectors, project_id, changed)
        return {"sync": index.status(project_id), "events": result["events"]}
    except Exception as exc:
        raise _rule_error(exc, "sync", project_id) from exc


@router.get("/projects/{project_id}/rule-knowledge")
def get_rule_knowledge(project_id: str, request: Request) -> dict:
    _authorize(request)
    from code_quality.rule_index import RuleIndex
    try:
        validate_project_id(project_id)
        index = RuleIndex()
        return {"sync": index.status(project_id), "documents": index.documents(project_id),
                "instance_url": index.instance_url(project_id),
                "settings": {"allow_remote": index.allow_remote(project_id)}}
    except Exception as exc:
        raise _rule_error(exc, "index", project_id) from exc


@router.put("/projects/{project_id}/rule-knowledge/settings")
async def put_rule_knowledge_settings(project_id: str, request: Request) -> dict:
    _authorize(request)
    from code_quality.rule_index import RuleIndex
    try:
        validate_project_id(project_id)
        payload = await _read_json(request, MAX_RULE_KNOWLEDGE_REQUEST_BYTES)
        return {"settings": {"allow_remote": RuleIndex().set_allow_remote(project_id, payload.get("allow_remote"))}}
    except Exception as exc:
        raise _rule_error(exc, "settings", project_id) from exc


@router.get("/projects/{project_id}/rule-knowledge/links")
def get_rule_links(project_id: str, request: Request, doc_id: list[str] | None = Query(None),
                   symbol_id: list[str] | None = Query(None), file: str | None = Query(None, max_length=300)) -> dict:
    _authorize(request)
    from code_quality.rule_index import RuleIndex, validate_doc_id, validate_symbol_id
    try:
        validate_project_id(project_id)
        doc_ids = [validate_doc_id(value) for value in doc_id[:100]] if doc_id else None
        symbol_ids = [validate_symbol_id(value) for value in symbol_id[:200]] if symbol_id else None
        index = RuleIndex()
        links = index.links(project_id, doc_ids=doc_ids, symbol_ids=symbol_ids, files=[file] if file else None)
        documents = {item["doc_id"]: item for item in index.documents(project_id)}
        return {"links": [{**link, "title": documents[link["doc_id"]]["title"], "url": documents[link["doc_id"]]["url"],
                           "synced_at": documents[link["doc_id"]]["synced_at"]} for link in links
                          if link["doc_id"] in documents],
                "instance_url": index.instance_url(project_id)}
    except Exception as exc:
        raise _rule_error(exc, "links", project_id) from exc


@router.post("/projects/{project_id}/rule-knowledge/links/suggest")
async def suggest_rule_links(project_id: str, request: Request) -> dict:
    _authorize(request)
    from code_quality.rule_code import suggest_links
    from code_quality.rule_index import validate_doc_id
    try:
        validate_project_id(project_id)
        payload = await _read_json(request, MAX_RULE_KNOWLEDGE_REQUEST_BYTES)
        doc_ids = payload.get("doc_ids")
        if doc_ids is not None:
            if not isinstance(doc_ids, list):
                raise HTTPException(422, "doc_ids must be a list.")
            doc_ids = [validate_doc_id(value) for value in doc_ids[:50]]
        revision = await run_in_threadpool(_rule_revision, project_id, payload.get("revision_id"))
        return await run_in_threadpool(suggest_links, project_id, revision, doc_ids)
    except Exception as exc:
        raise _rule_error(exc, "link_suggestion", project_id) from exc


@router.put("/projects/{project_id}/rule-knowledge/links")
async def put_rule_link(project_id: str, request: Request) -> dict:
    user_id = _authorize(request)
    from code_quality.rule_code import symbol_hashes
    from code_quality.rule_index import RuleIndex
    try:
        validate_project_id(project_id)
        payload = await _read_json(request, MAX_RULE_KNOWLEDGE_REQUEST_BYTES)
        code_hash = file = line = revision_id = None
        if payload.get("revision_id") is not None and payload.get("state") in {"confirmed", "manual"}:
            revision = await run_in_threadpool(_rule_revision, project_id, payload["revision_id"])
            definition = revision.definitions.get(payload.get("symbol_id"))
            if definition:
                hashes = await run_in_threadpool(symbol_hashes, revision.archive_loader(), revision.definitions,
                                                 [payload["symbol_id"]])
                code_hash, file, line = hashes.get(payload["symbol_id"]), definition["file"], definition["line"]
            revision_id = revision.revision_id
        link = RuleIndex().set_link_state(project_id, payload.get("doc_id"), payload.get("symbol_id"),
                                          payload.get("state"), user_id, code_hash=code_hash, file=file,
                                          line=line, revision_id=revision_id)
        return {"link": link}
    except Exception as exc:
        raise _rule_error(exc, "link_update", project_id) from exc


@router.get("/projects/{project_id}/rule-knowledge/coverage")
async def get_rule_coverage(project_id: str, request: Request, revision_id: str = Query(...)) -> dict:
    """Refresh code-drift hashes for reviewed links and report which rules have tagged tests."""
    _authorize(request)
    from code_quality.rule_assist import has_rule_tests
    from code_quality.rule_code import refresh_code_hashes
    from code_quality.rule_index import RuleIndex
    try:
        validate_project_id(project_id)
        index = RuleIndex()
        revision = await run_in_threadpool(_rule_revision, project_id, revision_id)
        refreshed = await run_in_threadpool(refresh_code_hashes, project_id, revision, index)
        archive = await run_in_threadpool(revision.archive_loader)
        tested = await run_in_threadpool(has_rule_tests, archive, [item["doc_id"] for item in index.documents(project_id)])
        return {"revision_id": revision_id, "tested": tested, "links_refreshed": refreshed}
    except Exception as exc:
        raise _rule_error(exc, "coverage", project_id) from exc


@router.post("/projects/{project_id}/rule-knowledge/impact")
async def check_rule_impact(project_id: str, request: Request) -> dict:
    _authorize(request)
    from code_quality.rule_assist import check_impact
    try:
        validate_project_id(project_id)
        payload = await _read_json(request, MAX_RULE_IMPACT_REQUEST_BYTES)
        revision = await run_in_threadpool(_rule_revision, project_id, payload.get("revision_id"))
        return await run_in_threadpool(check_impact, project_id, revision, payload.get("diff"))
    except Exception as exc:
        raise _rule_error(exc, "impact_check", project_id) from exc


@router.get("/projects/{project_id}/rule-knowledge/runtime")
async def get_rule_runtime(project_id: str, request: Request, revision_id: str = Query(...)) -> dict:
    _authorize(request)
    from code_quality.rule_assist import runtime_health
    try:
        validate_project_id(project_id)
        validate_revision_id(revision_id)
        from telemetry.registry import default_registry
        from telemetry.runtime import project_runtime_by_revision
        try:
            runtime = await run_in_threadpool(project_runtime_by_revision, project_id, revision_id, default_registry())
        except KeyError as exc:
            raise HTTPException(404, "The selected revision is not indexed.") from exc
        return {"revision_id": revision_id, **runtime_health(project_id, runtime.get("telemetry") or runtime)}
    except Exception as exc:
        raise _rule_error(exc, "runtime", project_id) from exc


@router.post("/projects/{project_id}/rule-knowledge/{doc_id}/tests")
async def generate_rule_tests(project_id: str, doc_id: str, request: Request) -> dict:
    _authorize(request)
    from code_quality.rule_assist import generate_tests
    from code_quality.rule_index import validate_doc_id
    try:
        validate_project_id(project_id)
        validate_doc_id(doc_id)
        payload = await _read_json(request, MAX_RULE_KNOWLEDGE_REQUEST_BYTES)
        revision = await run_in_threadpool(_rule_revision, project_id, payload.get("revision_id"))
        return await run_in_threadpool(generate_tests, project_id, revision, doc_id)
    except Exception as exc:
        raise _rule_error(exc, "test_generation", project_id) from exc


@router.get("/rules")
def list_custom_rules(request: Request) -> dict:
    user_id = _authorize(request)
    return {"rules": CustomRuleStore().list(user_id)}


async def _read_rule_payload(request: Request) -> dict:
    raw = await request.body()
    if len(raw) > MAX_RULE_REQUEST_BYTES:
        raise HTTPException(413, "Custom rule request exceeds the size limit.")
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, "Custom rule request must be valid JSON.") from exc
    if not isinstance(payload, dict):
        raise HTTPException(422, "Custom rule settings are invalid.")
    return payload


@router.post("/rules")
async def create_custom_rule(request: Request) -> dict:
    user_id = _authorize(request)
    payload = await _read_rule_payload(request)
    try:
        await run_in_threadpool(validate_semgrep_yaml, payload.get("yaml"))
        return {"rule": CustomRuleStore().save(user_id, payload.get("title"), payload.get("yaml"))}
    except AssessmentError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.put("/rules/{rule_id}")
async def update_custom_rule(rule_id: str, request: Request) -> dict:
    user_id = _authorize(request)
    payload = await _read_rule_payload(request)
    try:
        await run_in_threadpool(validate_semgrep_yaml, payload.get("yaml"))
        rule = CustomRuleStore().save(user_id, payload.get("title"), payload.get("yaml"), rule_id)
    except AssessmentError as exc:
        raise HTTPException(422, str(exc)) from exc
    if rule is None:
        raise HTTPException(404, "Custom rule set was not found.")
    return {"rule": rule}


@router.patch("/rules/{rule_id}/enabled")
async def set_custom_rule_enabled(rule_id: str, request: Request) -> dict:
    user_id = _authorize(request)
    payload = await _read_rule_payload(request)
    if type(payload.get("enabled")) is not bool:
        raise HTTPException(422, "Enabled must be true or false.")
    rule = CustomRuleStore().set_enabled(user_id, rule_id, payload["enabled"])
    if rule is None:
        raise HTTPException(404, "Custom rule set was not found.")
    return {"rule": rule}


@router.delete("/rules/{rule_id}")
def delete_custom_rule(rule_id: str, request: Request) -> dict:
    user_id = _authorize(request)
    if not CustomRuleStore().delete(user_id, rule_id):
        raise HTTPException(404, "Custom rule set was not found.")
    return {"deleted": True}


@router.post("/projects/{project_id}/revisions/{revision_id}/rules/preview")
async def preview_rule(project_id: str, revision_id: str, request: Request) -> dict:
    user_id = _authorize(request)
    try:
        validate_project_id(project_id)
        validate_revision_id(revision_id)
        payload = await _read_rule_payload(request)
        document = payload.get("yaml")
        await run_in_threadpool(validate_semgrep_yaml, document)
        from hashlib import sha256
        from telemetry.registry import default_registry
        registry = default_registry()
        snapshot = registry.get_by_revision_id(project_id, revision_id)
        if snapshot is None or snapshot.get("state") != "ready":
            raise HTTPException(404, "The selected indexed revision is unavailable.")
        archive_key = registry.archive_key(project_id, source_reference(snapshot))
        if not archive_key:
            raise HTTPException(409, "The source archive for this revision is no longer available.")
        from index.storage import storage
        archive = await run_in_threadpool(storage.get_snapshot_archive, archive_key)
        user_rule = {"rule_id": "preview", "title": "Preview", "yaml": document,
                     "content_hash": sha256(document.encode("utf-8")).hexdigest()}
        exclusion_patterns = ReportStore().exclusions(project_id)
        return await run_in_threadpool(preview_custom_rule, project_id, revision_id, snapshot, archive,
                                       user_rule, exclusion_patterns)
    except HTTPException:
        raise
    except AssessmentError as exc:
        raise HTTPException(422, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, "Invalid project revision identity.") from exc
    except Exception as exc:
        logger.warning("code_quality_rule_preview_failed project=%s revision=%s user=%s error_type=%s",
                       project_id, revision_id, user_id[:24], type(exc).__name__)
        raise HTTPException(503, "Rule preview could not complete. Check the selected revision and retry.") from exc


@router.get("/preferences")
def get_preferences(request: Request) -> dict:
    user_id = _authorize(request)
    return {"weights": ReportStore().weights(user_id)}


@router.put("/preferences")
async def put_preferences(request: Request) -> dict:
    user_id = _authorize(request)
    try:
        payload = await request.json()
        weights = ReportStore().save_weights(user_id, payload.get("weights") if isinstance(payload, dict) else None)
        return {"weights": weights}
    except AssessmentError as exc:
        raise HTTPException(422, str(exc)) from exc
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, "Invalid assessment preferences.") from exc


@router.get("/projects/{project_id}/exclusions")
def get_project_exclusions(project_id: str, request: Request) -> dict:
    _authorize(request)
    try:
        validate_project_id(project_id)
    except ValueError as exc:
        raise HTTPException(422, "Invalid project identity.") from exc
    return {"patterns": ReportStore().exclusions(project_id)}


@router.put("/projects/{project_id}/exclusions")
async def put_project_exclusions(project_id: str, request: Request) -> dict:
    _authorize(request)
    try:
        validate_project_id(project_id)
        raw = await request.body()
        if len(raw) > MAX_EXCLUSION_REQUEST_BYTES:
            raise HTTPException(413, "Assessment exclusion settings exceed the size limit.")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("Assessment exclusion settings must be an object.")
        patterns = ReportStore().save_exclusions(project_id, payload.get("patterns"))
        return {"patterns": patterns}
    except HTTPException:
        raise
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(422, str(exc) or "Assessment exclusion settings are invalid.") from exc


@router.get("/projects/{project_id}/public-routes")
def get_project_public_routes(project_id: str, request: Request) -> dict:
    """Routes the project declares intentionally public (code_quality/public_routes.py)."""
    _authorize(request)
    try:
        validate_project_id(project_id)
    except ValueError as exc:
        raise HTTPException(422, "Invalid project identity.") from exc
    from code_quality.public_routes import PublicRouteStore

    return {"patterns": PublicRouteStore().patterns(project_id)}


@router.put("/projects/{project_id}/public-routes")
async def put_project_public_routes(project_id: str, request: Request) -> dict:
    _authorize(request)
    from code_quality.public_routes import PublicRouteStore

    try:
        validate_project_id(project_id)
        raw = await request.body()
        if len(raw) > MAX_EXCLUSION_REQUEST_BYTES:
            raise HTTPException(413, "Public route settings exceed the size limit.")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("Public route settings must be an object.")
        return {"patterns": PublicRouteStore().save(project_id, payload.get("patterns"))}
    except HTTPException:
        raise
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(422, str(exc) or "Public route settings are invalid.") from exc


@router.get("/projects/{project_id}/reports")
def list_reports(project_id: str, request: Request) -> dict:
    _authorize(request)
    try:
        validate_project_id(project_id)
    except ValueError as exc:
        raise HTTPException(422, "Invalid project identity.") from exc
    return {"reports": ReportStore().history(project_id)}


@router.get("/projects/{project_id}/reports/{report_id}")
def get_report(project_id: str, report_id: str, request: Request) -> dict:
    _authorize(request)
    try:
        validate_project_id(project_id)
    except ValueError as exc:
        raise HTTPException(422, "Invalid project identity.") from exc
    report = ReportStore().get_report(project_id, report_id)
    if report is None:
        raise HTTPException(404, "Assessment report was not found.")
    return report


@router.delete("/projects/{project_id}/reports/{report_id}")
def delete_report(project_id: str, report_id: str, request: Request) -> dict:
    _authorize(request)
    try:
        validate_project_id(project_id)
    except ValueError as exc:
        raise HTTPException(422, "Invalid project identity.") from exc
    if not ReportStore().delete_report(project_id, report_id):
        raise HTTPException(404, "Assessment report was not found.")
    return {"deleted": True}


def _performance_uuid(value, label: str) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(422, f"Invalid performance {label} identity.") from exc


async def _performance_payload(request: Request) -> dict:
    raw = await request.body()
    if len(raw) > MAX_PERFORMANCE_REQUEST_BYTES:
        raise HTTPException(413, "Performance request exceeds the size limit.")
    try:
        payload = json.loads(raw or b"{}")
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, "Performance request must be valid JSON.") from exc
    if not isinstance(payload, dict):
        raise HTTPException(422, "Performance request settings are invalid.")
    return payload


def _performance_project(project_id: str) -> None:
    try:
        validate_project_id(project_id)
    except ValueError as exc:
        raise HTTPException(422, "Invalid project identity.") from exc


@router.get("/projects/{project_id}/performance/targets")
def list_performance_targets(project_id: str, request: Request) -> dict:
    user_id = _authorize(request)
    _performance_project(project_id)
    return {"targets": PerformanceStore().targets(user_id, project_id)}


@router.post("/projects/{project_id}/performance/targets")
async def save_performance_target(project_id: str, request: Request) -> dict:
    user_id = _authorize(request)
    _performance_project(project_id)
    payload = await _performance_payload(request)
    if payload.get("target_id") is not None:
        payload["target_id"] = _performance_uuid(payload["target_id"], "target")
    try:
        return {"target": PerformanceStore().save_target(user_id, project_id, payload)}
    except PerformanceError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/projects/{project_id}/performance/targets/{target_id}")
def get_performance_target(project_id: str, target_id: str, request: Request) -> dict:
    user_id = _authorize(request)
    _performance_project(project_id)
    target = PerformanceStore().get_target(user_id, project_id, _performance_uuid(target_id, "target"))
    if target is None:
        raise HTTPException(404, "Performance target was not found.")
    return {"target": target}


@router.delete("/projects/{project_id}/performance/targets/{target_id}")
def delete_performance_target(project_id: str, target_id: str, request: Request) -> dict:
    user_id = _authorize(request)
    _performance_project(project_id)
    if not PerformanceStore().delete_target(user_id, project_id, _performance_uuid(target_id, "target")):
        raise HTTPException(404, "Performance target was not found.")
    return {"deleted": True}


@router.get("/projects/{project_id}/revisions/{revision_id}/performance/routes")
def performance_routes(project_id: str, revision_id: str, request: Request) -> dict:
    """GET routes seen for this revision in the Runtime window, with production volume and latency."""
    _authorize(request)
    try:
        validate_project_id(project_id)
        validate_revision_id(revision_id)
        from telemetry.registry import default_registry
        if default_registry().get_by_revision_id(project_id, revision_id) is None:
            raise HTTPException(404, "Indexed revision was not found.")
        from code_quality.performance import route_parameters
        from telemetry.runtime import ClickHouseRuntime
        summary = ClickHouseRuntime().summary(project_id, revision_id, set())
        routes = []
        for item in summary.get("routes", []):
            route = item.get("route")
            if str(item.get("method", "")).upper() not in {"GET", "HEAD"}:
                continue
            if not isinstance(route, str) or not route.startswith("/") or route.startswith("//") or "?" in route:
                continue
            routes.append({"method": "GET", "route": route, "parameters": route_parameters(route),
                           "requests": int(item.get("requests") or 0), "failures": int(item.get("failures") or 0),
                           "error_rate": item.get("error_rate"), "p95_ms": item.get("p95_ms"),
                           "last_seen": item.get("last_seen")})
        merged: dict[str, dict] = {}
        for item in routes:  # GET and HEAD of one route are one benchmark choice
            current = merged.get(item["route"])
            if current is None or item["requests"] > current["requests"]:
                merged[item["route"]] = item
        return {"available": bool(summary.get("available")), "reason": summary.get("reason"),
                "window_hours": summary.get("aggregation_window_hours"),
                "routes": sorted(merged.values(), key=lambda item: (-item["requests"], item["route"]))}
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(422, "Invalid project revision identity.") from exc
    except Exception as exc:
        logger.warning("code_quality_performance_routes_failed project=%s error_type=%s", project_id, type(exc).__name__)
        raise HTTPException(503, "Sampled Runtime routes are unavailable.") from exc


@router.post("/projects/{project_id}/performance/runs", status_code=202)
async def create_performance_run(project_id: str, request: Request):
    """Start a background benchmark; poll GET .../runs/{run_id} for progress and the result."""
    user_id = _authorize(request)
    _performance_project(project_id)
    payload = await _performance_payload(request)
    store = PerformanceStore()
    target = store.get_target(user_id, project_id, _performance_uuid(payload.get("target_id"), "target"))
    if target is None:
        raise HTTPException(404, "Performance target was not found.")
    if target.get("unassigned"):
        raise HTTPException(409, "Save this target under the current project before running it.")
    revision = None
    if payload.get("revision_id"):
        try:
            validate_revision_id(payload["revision_id"])
        except ValueError as exc:
            raise HTTPException(422, "Invalid project revision identity.") from exc
        from telemetry.registry import default_registry
        revision = default_registry().get_by_revision_id(project_id, payload["revision_id"])
        if revision is None:
            raise HTTPException(404, "Indexed revision was not found.")
    try:
        job = await run_in_threadpool(start_run, store, user_id, project_id, target, payload.get("routes"), revision,
                                      payload.get("bearer_token"), payload.get("login_credentials"))
    except PerformanceBusy as exc:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=409, content={"detail": str(exc), "active_run_id": exc.active_run_id})
    except PerformanceError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {**job, "progress": live_progress(job)}


@router.get("/projects/{project_id}/performance/runs")
def list_performance_runs(project_id: str, request: Request) -> dict:
    user_id = _authorize(request)
    _performance_project(project_id)
    store = PerformanceStore()
    jobs = store.jobs(user_id, project_id)
    return {"runs": store.history(user_id, project_id),
            "active": [{**job, "progress": live_progress(job)} for job in jobs if job["state"] == "running"],
            "failed": [job for job in jobs if job["state"] == "failed"][:5]}


@router.get("/projects/{project_id}/performance/runs/{run_id}")
def get_performance_run(project_id: str, run_id: str, request: Request) -> dict:
    user_id = _authorize(request)
    _performance_project(project_id)
    run_id = _performance_uuid(run_id, "run")
    store = PerformanceStore()
    result = store.get_run(user_id, project_id, run_id)
    if result is not None:
        return result
    job = store.job(user_id, project_id, run_id)
    if job is None:
        raise HTTPException(404, "Performance run was not found.")
    return {**job, "progress": live_progress(job)}


@router.delete("/projects/{project_id}/performance/runs/{run_id}")
def delete_or_cancel_performance_run(project_id: str, run_id: str, request: Request) -> dict:
    """Cancel a running benchmark (its partial result is saved) or delete a saved run."""
    user_id = _authorize(request)
    _performance_project(project_id)
    run_id = _performance_uuid(run_id, "run")
    store = PerformanceStore()
    job = store.job(user_id, project_id, run_id)
    if job is not None and job["state"] == "running":
        return {"cancelling": cancel_job(job)}
    if not store.delete_run(user_id, project_id, run_id) and job is None:
        raise HTTPException(404, "Performance run was not found.")
    return {"deleted": True}


@router.get("/projects/{project_id}/performance/runs/{run_id}/script")
def export_performance_script(project_id: str, run_id: str, request: Request):
    user_id = _authorize(request)
    _performance_project(project_id)
    result = PerformanceStore().get_run(user_id, project_id, _performance_uuid(run_id, "run"))
    if result is None:
        raise HTTPException(404, "Performance run was not found.")
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(export_script(result), media_type="text/javascript")


@router.get("/projects/{project_id}/performance/runs/{run_id}/traces")
async def performance_run_traces(project_id: str, run_id: str, request: Request) -> dict:
    user_id = _authorize(request)
    _performance_project(project_id)
    result = PerformanceStore().get_run(user_id, project_id, _performance_uuid(run_id, "run"))
    if result is None:
        raise HTTPException(404, "Performance run was not found.")
    return await run_in_threadpool(benchmark_traces, project_id, result.get("revision_id"), result["run_id"],
                                  route_results=result.get("route_results"))


@router.post("/projects/{project_id}/performance/compare")
async def compare_performance_runs(project_id: str, request: Request) -> dict:
    user_id = _authorize(request)
    _performance_project(project_id)
    payload = await _performance_payload(request)
    store = PerformanceStore()
    left = store.get_run(user_id, project_id, _performance_uuid(payload.get("left_run_id"), "run"))
    right = store.get_run(user_id, project_id, _performance_uuid(payload.get("right_run_id"), "run"))
    if not left or not right:
        raise HTTPException(404, "One or both performance runs were not found.")
    return {"comparison": compare_runs(left, right), "runs": [left, right]}


@router.get("/projects/{project_id}/revisions/{revision_id}/precheck")
async def assessment_precheck(project_id: str, revision_id: str, request: Request) -> dict:
    _authorize(request)
    try:
        validate_project_id(project_id)
        validate_revision_id(revision_id)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, "Invalid project revision identity.") from exc
    from code_quality.index_readiness import assessment_index_readiness
    from telemetry.registry import default_registry
    return await run_in_threadpool(assessment_index_readiness, project_id, revision_id, default_registry())


@router.delete("/projects/{project_id}/revisions/{revision_id}")
async def delete_revision(project_id: str, revision_id: str, request: Request) -> dict:
    _authorize(request)
    try:
        validate_project_id(project_id)
        validate_revision_id(revision_id)
        if not await run_in_threadpool(remove_revision, project_id, revision_id):
            raise HTTPException(404, "Project revision was not found.")
        return {"deleted": True}
    except HTTPException:
        raise
    except RevisionBusy as exc:
        raise HTTPException(409, str(exc)) from exc
    except RevisionStorageUnavailable as exc:
        logger.warning("revision_removal_storage_unavailable project=%s revision=%s error_type=%s", project_id, revision_id, type(exc.__cause__).__name__)
        raise HTTPException(503, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, "Invalid project revision identity.") from exc
    except Exception as exc:
        logger.exception("revision_removal_failed project=%s revision=%s", project_id, revision_id)
        raise HTTPException(503, "Revision removal is incomplete. Check storage availability and retry removal.") from exc


@router.post("/projects/{project_id}/revisions/{revision_id}/reindex")
async def reindex_revision(project_id: str, revision_id: str, request: Request) -> dict:
    """Rebuild a Git revision's vectors and graph from its stored source archive."""
    from telemetry.registry import default_registry

    _authorize(request)
    try:
        validate_project_id(project_id)
        validate_revision_id(revision_id)
    except ValueError as exc:
        raise HTTPException(422, "Invalid project revision identity.") from exc
    registry = default_registry()
    item = await run_in_threadpool(registry.get_by_revision_id, project_id, revision_id)
    if item is None:
        raise HTTPException(404, "Project revision was not found.")
    outcome = await run_in_threadpool(registry.queue_reindex, project_id, source_reference(item))
    if outcome == "missing":
        raise HTTPException(409, "Only a Git revision with a stored source archive can be re-indexed.")
    if outcome == "busy":
        raise HTTPException(409, "This revision is already being indexed.")
    return {"queued": True, "revision_id": revision_id}


@router.get("/projects/{project_id}/revisions/{revision_id}/index-progress")
async def revision_index_progress(project_id: str, revision_id: str, request: Request) -> dict:
    """Stage and overall percent of a revision's running index job, for the progress bar."""
    from telemetry.registry import default_registry

    _authorize(request)
    try:
        validate_project_id(project_id)
        validate_revision_id(revision_id)
    except ValueError as exc:
        raise HTTPException(422, "Invalid project revision identity.") from exc
    registry = default_registry()
    item = await run_in_threadpool(registry.get_by_revision_id, project_id, revision_id)
    if item is None:
        raise HTTPException(404, "Project revision was not found.")
    return await run_in_threadpool(registry.index_progress, project_id, source_reference(item))


def _assess_locked(registry, project_id, revision_id, snapshot, archive, user_id,
                   outline_token, outline_credential_error, sonarqube_token):
    with registry.lock(project_id, source_reference(snapshot)):
        current = registry.get_by_revision_id(project_id, revision_id)
        if not current or current.get("state") != "ready":
            raise AssessmentError("The selected revision is no longer available for assessment.")
        return assess_revision(project_id, revision_id, snapshot, archive, user_id,
                               None, outline_token, outline_credential_error, sonarqube_token)


@router.post("/projects/{project_id}/revisions/{revision_id}/assess")
async def assess(project_id: str, revision_id: str, request: Request) -> dict:
    user_id = _authorize(request)
    try:
        validate_project_id(project_id)
        validate_revision_id(revision_id)
        assessment_payload = await _read_outline_payload(request)
        outline_token = assessment_payload.get("outline_token")
        if outline_token is not None and (not isinstance(outline_token, str) or len(outline_token) > 8192):
            raise HTTPException(422, "Outline credential is invalid.")
        sonarqube_token = assessment_payload.get("sonarqube_token")
        if sonarqube_token is not None and (not isinstance(sonarqube_token, str) or len(sonarqube_token) > 8192):
            raise HTTPException(422, "SonarQube credential is invalid.")
        outline_credential_error = assessment_payload.get("outline_credential_error")
        if outline_credential_error is not None and not isinstance(outline_credential_error, str):
            raise HTTPException(422, "Outline credential status is invalid.")
        from telemetry.registry import default_registry
        registry = default_registry()
        from code_quality.index_readiness import assessment_index_readiness
        readiness = await run_in_threadpool(assessment_index_readiness, project_id, revision_id, registry)
        if not readiness["ready"]:
            raise HTTPException(503 if readiness["status"] == "index_unavailable" else 409, readiness["reason"])
        snapshot = registry.get_by_revision_id(project_id, revision_id)
        if snapshot is None or snapshot.get("state") != "ready":
            raise HTTPException(404, "The selected indexed revision is unavailable.")
        archive_key = registry.archive_key(project_id, source_reference(snapshot))
        if not archive_key:
            raise HTTPException(409, "The source archive for this revision is no longer available for assessment.")
        from index.storage import storage
        try:
            archive = await run_in_threadpool(storage.get_snapshot_archive, archive_key)
        except Exception as exc:
            logger.warning(
                "code_quality_source_archive_unavailable project=%s revision=%s error_type=%s",
                project_id,
                revision_id,
                type(exc).__name__,
            )
            raise HTTPException(
                503,
                "The selected revision's source archive could not be retrieved. Retry or re-index the project.",
            ) from exc
        return await run_in_threadpool(_assess_locked, registry, project_id, revision_id, snapshot, archive, user_id,
                                       outline_token, outline_credential_error, sonarqube_token)
    except HTTPException:
        raise
    except AssessmentError as exc:
        raise HTTPException(422, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, "Invalid indexed revision identity.") from exc
    except AssessmentModelUnavailable as exc:
        category, provider_status, detail = model_failure_diagnostic(exc.__cause__)
        logger.warning(
            "code_quality_model_unavailable project=%s revision=%s category=%s provider_status=%s error_type=%s",
            project_id,
            revision_id,
            category,
            provider_status,
            type(exc.__cause__).__name__ if exc.__cause__ else type(exc).__name__,
        )
        raise HTTPException(503, detail) from exc
    except AssessmentReportStoreUnavailable as exc:
        logger.error(
            "code_quality_report_store_unavailable project=%s revision=%s error_type=%s",
            project_id,
            revision_id,
            type(exc.__cause__).__name__ if exc.__cause__ else type(exc).__name__,
        )
        raise HTTPException(
            503,
            "The assessment completed but its report could not be saved. Check API storage and retry.",
        ) from exc
    except Exception as exc:
        logger.error(
            "code_quality_unexpected_failure project=%s revision=%s error_type=%s",
            project_id,
            revision_id,
            type(exc).__name__,
        )
        raise HTTPException(
            503,
            "The assessment failed during internal processing. Check API logs and retry.",
        ) from exc


@router.get("/projects/{project_id}/revisions/{revision_id}/source")
async def source_window(
    project_id: str,
    revision_id: str,
    request: Request,
    file_path: str = Query(..., min_length=1, max_length=1024),
    line: int = Query(1, ge=1),
    start_line: int | None = Query(None, ge=1),
) -> dict:
    """Read a bounded source window from one authorized immutable revision."""
    _authorize(request)
    try:
        validate_project_id(project_id)
        validate_revision_id(revision_id)
        from telemetry.registry import default_registry
        registry = default_registry()
        snapshot = registry.get_by_revision_id(project_id, revision_id)
        if snapshot is None or snapshot.get("state") != "ready":
            raise HTTPException(404, "The selected indexed revision is unavailable.")
        archive_key = registry.archive_key(project_id, source_reference(snapshot))
        if not archive_key:
            raise HTTPException(409, "The source archive for this revision is no longer available.")
        from index.storage import storage
        archive = await run_in_threadpool(storage.get_snapshot_archive, archive_key)
        return await run_in_threadpool(read_source_window, archive, file_path, line, start_line)
    except HTTPException:
        raise
    except SourceViewError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, "Invalid indexed revision or source location.") from exc
    except Exception as exc:
        logger.warning(
            "code_quality_source_view_failed project=%s revision=%s error_type=%s",
            project_id,
            revision_id,
            type(exc).__name__,
        )
        raise HTTPException(503, "Source for this revision could not be retrieved. Retry or re-index the project.") from exc


@router.get("/projects/{project_id}/revisions/{revision_id}/directories")
async def source_directories(project_id: str, revision_id: str, request: Request) -> dict:
    """Return directory paths, without file names, for the selected immutable revision."""
    _authorize(request)
    try:
        validate_project_id(project_id)
        validate_revision_id(revision_id)
        from telemetry.registry import default_registry
        registry = default_registry()
        snapshot = registry.get_by_revision_id(project_id, revision_id)
        if snapshot is None or snapshot.get("state") != "ready":
            raise HTTPException(404, "The selected indexed revision is unavailable.")
        archive_key = registry.archive_key(project_id, source_reference(snapshot))
        if not archive_key:
            raise HTTPException(409, "The source archive for this revision is no longer available.")
        from index.storage import storage
        archive = await run_in_threadpool(storage.get_snapshot_archive, archive_key)
        if archive and len(archive) > MAX_DIRECTORY_ARCHIVE_BYTES:
            directories = await run_in_threadpool(
                list_indexed_source_directories, project_id, revision_id, snapshot,
            )
        else:
            directories = await run_in_threadpool(list_source_directories, archive)
        return {"directories": directories}
    except HTTPException:
        raise
    except SourceViewError as exc:
        raise HTTPException(422, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, "Invalid indexed revision identity.") from exc
    except Exception as exc:
        logger.warning("code_quality_directory_tree_failed project=%s revision=%s error_type=%s",
                       project_id, revision_id, type(exc).__name__)
        raise HTTPException(503, "Directories for this revision could not be retrieved. Retry or re-index the project.") from exc
