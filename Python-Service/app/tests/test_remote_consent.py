"""Tests for per-project consent to send project source to the external model."""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import infra.remote_consent as consent
from infra.remote_consent import InvalidProject, get_consent, remote_source_allowed, set_consent
from rag.answer_service import AnswerService


class _MemoryStore:
    def __init__(self):
        self.values = {}

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value


@pytest.fixture
def store(monkeypatch):
    memory = _MemoryStore()
    monkeypatch.setattr(consent, "_store", lambda store=None: memory)
    monkeypatch.setattr(consent, "existing_project_ids", lambda: ["pizza", "armaiti"])
    return memory


def test_existing_projects_start_allowed_and_new_projects_start_blocked(store):
    seed = consent.existing_project_ids

    assert get_consent("pizza", seed=seed)["allowed"] is True
    assert get_consent("pizza", seed=seed)["source"] == "existing-project"
    assert get_consent("brand-new", seed=seed) == {"project_id": "brand-new", "allowed": False,
                                                   "updated_at": None, "source": "default"}


def test_seeding_happens_once(store):
    seeds = []

    def seed():
        seeds.append(1)
        return ["pizza"]

    get_consent("pizza", seed=seed)
    get_consent("other", seed=seed)
    set_consent("other", True, seed=seed)

    assert len(seeds) == 1
    assert get_consent("other", seed=seed)["allowed"] is True


def test_seed_failure_blocks_everything_instead_of_allowing(store):
    def broken():
        raise OSError("registry unreadable")

    assert get_consent("pizza", seed=broken)["allowed"] is False


def test_admin_can_turn_consent_off_and_on(store):
    seed = consent.existing_project_ids

    assert set_consent("pizza", False, seed=seed)["allowed"] is False
    assert remote_source_allowed("pizza", seed=seed) is False
    assert set_consent("pizza", True, seed=seed)["source"] == "admin"
    assert remote_source_allowed("pizza", seed=seed) is True


@pytest.mark.parametrize("value", ["true", 1, None])
def test_consent_value_must_be_boolean(store, value):
    with pytest.raises(InvalidProject):
        set_consent("pizza", value)


@pytest.mark.parametrize("project_id", ["", "../x", "a b", "x" * 200])
def test_invalid_project_ids_are_rejected_and_never_allowed(store, project_id):
    with pytest.raises(InvalidProject):
        get_consent(project_id)
    assert remote_source_allowed(project_id or "bad id") is False


def test_requests_without_a_project_are_not_gated(store):
    assert remote_source_allowed(None) is True
    assert remote_source_allowed("") is True


# --- enforcement in the answer service --------------------------------------------------

def _service(calls):
    def answer_context(body, query, trace):
        calls.append("retrieval")
        return [], [], None

    return AnswerService(
        answer_context=answer_context, resolve_profile=lambda b, q: None,
        inspect_context=lambda r, b: None, domain_context=lambda r, b: None,
        resolve_preview_mode=lambda b: "off", report_stream_error=lambda exc: None,
    )


def test_blocked_project_gets_no_retrieval_and_no_model_call(store, monkeypatch):
    import rag.answer as answer_module

    def must_not_run(*args, **kwargs):
        raise AssertionError("the model must not be called")

    monkeypatch.setattr(answer_module, "generate_answer", must_not_run)
    monkeypatch.setattr(answer_module, "generate_answer_stream", must_not_run)
    calls = []
    service = _service(calls)

    result = service.answer({"query": "q", "project_id": "brand-new"})
    events = [json.loads(e[len("data: "):]) for e in service.stream({"query": "q", "project_id": "brand-new"})]

    assert calls == []
    assert result["remote_source_blocked"] is True and "Project access" in result["answer"]
    assert result["llmUsed"] is None and result["tools"]["code"] == "consent"
    assert [e["type"] for e in events] == ["done"] and events[0]["remote_source_blocked"] is True
    assert events[0]["matches"] == [] and events[0]["tool_calls"] == []


def test_allowed_project_and_local_requests_answer_normally(store, monkeypatch):
    import rag.answer as answer_module

    monkeypatch.setattr(answer_module, "generate_answer",
                        lambda *a, **k: {"answer": "ok", "provider": "remote", "model": "m", "duration_ms": 1})
    calls = []
    service = _service(calls)

    assert service.answer({"query": "q", "project_id": "pizza"})["answer"] == "ok"
    assert service.answer({"query": "q", "project_id": "brand-new", "use_remote": False})["answer"] == "ok"
    assert calls == ["retrieval", "retrieval"]


def test_unreadable_consent_store_fails_closed(monkeypatch):
    def broken(project_id, **kwargs):
        raise RuntimeError("settings db locked")

    monkeypatch.setattr(consent, "remote_source_allowed", broken)
    result = _service([]).answer({"query": "q", "project_id": "pizza"})

    assert result["remote_source_blocked"] is True


# --- internal API ------------------------------------------------------------------------

def _client(monkeypatch):
    import routers.remote_consent as route_module

    monkeypatch.setattr(route_module.settings._env, "internal_secret", "s3cret")
    app = FastAPI()
    app.include_router(route_module.router)
    return TestClient(app)


def test_api_requires_internal_secret(store, monkeypatch):
    client = _client(monkeypatch)

    assert client.get("/projects/pizza/remote-source").status_code == 401
    assert client.get("/projects/pizza/remote-source", headers={"X-Internal-Secret": "wrong"}).status_code == 401


def test_api_reads_and_updates_consent(store, monkeypatch):
    client = _client(monkeypatch)
    headers = {"X-Internal-Secret": "s3cret"}

    assert client.get("/projects/pizza/remote-source", headers=headers).json()["allowed"] is True
    updated = client.put("/projects/pizza/remote-source", headers=headers, json={"allowed": False})
    assert updated.status_code == 200 and updated.json()["allowed"] is False
    assert client.put("/projects/pizza/remote-source", headers=headers, json={"allowed": "no"}).status_code == 400
    assert client.get("/projects/bad%20id/remote-source", headers=headers).status_code == 400


# --- other features that send source to the remote model ---------------------------------

def _block(monkeypatch, allowed):
    monkeypatch.setattr(consent, "remote_source_allowed", lambda project_id, **kwargs: allowed)


def test_code_quality_assessment_stops_before_reading_anything(monkeypatch):
    import code_quality.service as cq
    import runtime.llm as llm

    _block(monkeypatch, False)
    monkeypatch.setattr(llm, "remote_configured", lambda: True)

    class Store:
        def weights(self, user_id):
            raise AssertionError("assessment must stop before touching the report store")

    with pytest.raises(cq.AssessmentError, match="Project access"):
        cq.assess_revision("pizza", "rev_1", {}, b"", "user", store=Store())


def test_code_quality_assessment_is_not_gated_without_a_remote_model(monkeypatch):
    import code_quality.service as cq
    import runtime.llm as llm

    _block(monkeypatch, False)
    monkeypatch.setattr(llm, "remote_configured", lambda: False)

    class Store:
        def weights(self, user_id):
            raise RuntimeError("reached the store")

    with pytest.raises(cq.AssessmentReportStoreUnavailable):
        cq.assess_revision("pizza", "rev_1", {}, b"", "user", store=Store())


def test_rule_assist_blocks_remote_calls_without_project_consent(monkeypatch):
    import code_quality.rule_assist as assist

    _block(monkeypatch, False)
    monkeypatch.setattr(assist, "model_provider", lambda project_id, index=None: "remote")

    def must_not_run(*args, **kwargs):
        raise AssertionError("the model must not be called")

    with pytest.raises(assist.RuleAssistError, match="Project access"):
        assist._call_model("pizza", {"task": "x"}, "system", 100, must_not_run, index=None)

    monkeypatch.setattr(assist, "model_provider", lambda project_id, index=None: "local")
    result, model, used = assist._call_model("pizza", {"t": 1}, "s", 10, lambda *a, **k: ('{"ok": true}', "m", "local"),
                                             index=None)
    assert result == {"ok": True} and used == "local"


def test_runtime_analysis_blocks_only_the_remote_provider(monkeypatch):
    import telemetry.analysis as analysis

    _block(monkeypatch, False)
    with pytest.raises(analysis.AnalysisError, match="Project access"):
        analysis.analyze_request("pizza", {}, {}, "remote", definitions_loader=None, archive_loader=None)
    with pytest.raises(analysis.AnalysisError, match="Request telemetry is unavailable"):
        analysis.analyze_request("pizza", {}, {}, "local", definitions_loader=None, archive_loader=None)


def test_runtime_diff_preview_checks_consent_before_the_model(monkeypatch):
    import telemetry.analysis as analysis

    _block(monkeypatch, False)
    with pytest.raises(analysis.AnalysisError):
        analysis._require_remote_consent("pizza", "remote")
    analysis._require_remote_consent("pizza", "local")
    _block(monkeypatch, True)
    analysis._require_remote_consent("pizza", "remote")


def test_guard_fails_closed(monkeypatch):
    def broken(project_id, **kwargs):
        raise RuntimeError("store locked")

    monkeypatch.setattr(consent, "remote_source_allowed", broken)
    with pytest.raises(consent.RemoteSourceNotAllowed):
        consent.require_remote_source_allowed("pizza")
